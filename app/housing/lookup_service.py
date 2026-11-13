"""Stateless HTTP gateway service. No public bulk roster endpoint."""
from __future__ import annotations

import base64
import copy
import json
import re
from datetime import datetime, timezone,timedelta

try:
    from .lookup_tokens import AccessError, verify_token
    from .opportunity import query_summary, queue_reference, review_position, qualified_reference, filtered_reference, intention_context
except ImportError:  # Cloud function ZIP has these three stdlib-only modules at root.
    from lookup_tokens import AccessError, verify_token
    from opportunity import query_summary, queue_reference, review_position, qualified_reference, filtered_reference, intention_context


class LookupService:
    def __init__(self, data, metadata, secret, origin, clock=None):
        self.data, self.metadata, self.secret, self.origin = data, metadata, secret, origin
        # Exact origins only. During a domain migration, keep already-sent links
        # usable by listing both the old and new origins in the private config.
        self.origins = frozenset(value.strip() for value in origin.split(',') if value.strip())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def handle(self, event):
        headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
        response_headers = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
                            'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Vary': 'Origin',
                            'Referrer-Policy': 'no-referrer'}
        if headers.get('origin') in self.origins:
            response_headers['Access-Control-Allow-Origin'] = headers['origin']
        def response(status, payload):
            return {'statusCode': status, 'headers': response_headers, 'isBase64Encoded': False,
                    'body': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}
        if headers.get('origin') and headers['origin'] not in self.origins:
            return response(403, {'code': 'origin_denied', 'message': '不允许的页面来源'})
        method = event.get('httpMethod', '').upper()
        if method == 'OPTIONS':
            response_headers.update({'Access-Control-Allow-Methods': 'POST, OPTIONS',
                                     'Access-Control-Allow-Headers': 'Authorization, Content-Type'})
            return response(204, {})
        # Never accept bearer tokens from query parameters or request bodies.
        auth = headers.get('authorization', '')
        token = auth[7:] if isinstance(auth, str) and auth.startswith('Bearer ') else ''
        try:
            claims = verify_token(token, self.secret, self.clock())
        except AccessError as exc:
            return response(401, {'code': exc.code, 'message': str(exc)})
        except ValueError:
            return response(503, {'code': 'not_configured', 'message': '查询服务暂未配置完成，请稍后再试。'})
        if method != 'POST':
            return response(405, {'code': 'method_not_allowed', 'message': '请求方式不支持'})
        path = (event.get('path') or '').rstrip('/').split('/')[-1]
        now = self.clock().timestamp()
        common = {'server_time': now, 'expires_at': claims['exp'], 'version': self.metadata['version']}
        if path == 'session':
            return response(200, dict(common, regions=self.metadata['regions'],built_at=self.metadata.get('built_at')))
        if path != 'query':
            return response(404, {'code': 'not_found', 'message': '接口不存在'})
        raw = event.get('body') or '{}'
        try:
            if event.get('isBase64Encoded'):
                raw = base64.b64decode(raw, validate=True).decode('utf-8')
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 2048:
                raise ValueError()
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) != {'region', 'apply_no', 'queue', 'version'}:
                raise ValueError()
            region, number, queue = request['region'], request['apply_no'], request['queue']
            if not all(isinstance(v, str) for v in (region, number, queue)):
                raise ValueError()
            number = number.strip().upper()
            if not re.fullmatch(r'[A-Z0-9-]{6,64}', number) or len(queue) > 40 or region not in self.data:
                raise ValueError()
            if queue not in self.metadata['regions'][region]['queues']:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, KeyError):
            return response(400, {'code': 'invalid_request', 'message': '请核对地区、认租编号及本人队列。'})
        if request['version'] != self.metadata['version']:
            return response(409, {'code': 'version_changed', 'message': '查询数据已更新，请刷新页面后重新查询。'})
        bundle = self.data[region]
        found = bundle['rows'].get(number, [])
        limited = {'district': bundle['district'], 'batches': [], 'rows': {number: []}}
        for match in found:
            original = bundle['batches'][match[0]]
            batch = copy.deepcopy({k: v for k, v in original.items() if k not in {'R', 'final_rows', 'review_rows','queue_updates'}})
            batch['c'] = [copy.deepcopy(original['c'][match[1]])]
            batch['final_rows'] = {number: copy.deepcopy(original['final_rows'][number])} if number in original.get('final_rows', {}) else {}
            batch['review_rows'] = {number: copy.deepcopy(original['review_rows'][number])} if number in original.get('review_rows', {}) else {}
            batch['queue_updates']={number:copy.deepcopy(original['queue_updates'][number])} if number in original.get('queue_updates',{}) else {}
            batch['selection_status']='listed' if number in batch['final_rows'] else 'not_listed_in_verified_scope' if (original.get('final_notice') or {}).get('verified') else 'unverified'
            try:
                from .handling import guidance,state
            except ImportError:
                from handling import guidance,state
            now=datetime.fromtimestamp(self.clock().timestamp(),timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            selected_windows=[w for w in original.get('selection_handling',[]) if not w.get('bands') or original['c'][match[1]]['key'] in w['bands']]
            batch['handling']=[dict(w,current_state=state(w,now)) for w in [*original.get('handling',[]),*selected_windows]]
            own_review=batch['review_rows'].get(number,{})
            batch['handling_guidance']=guidance(own_review.get('status','not_found'),batch['handling'],now)
            if batch['final_rows'].get(number):batch['handling_guidance'][0]='正式选房记录已公布，请按本人官方排位核对场次、材料和安排；房源选完即止。'
            # The entire original reason-combination table is not needed to
            # answer one household; anonymous category totals remain available.
            if batch.get('qualification',{}).get('reason_summary'):
                batch['qualification']['reason_summary'].pop('exact',None)
            if batch.get('review_statistics',{}).get('reason_summary'):
                batch['review_statistics']['reason_summary'].pop('exact',None)
            returned = [len(limited['batches']), 0, *match[2:]]
            batch['access_context'] = {'match': returned, 'queue': queue,
                                      'summary': query_summary(bundle, match, queue),
                                      'review_position': review_position(bundle, match, number),
                                      'qualified_reference': qualified_reference(bundle,match,number),
                                      'filtered_reference': filtered_reference(bundle,match,number,queue),
                                      'intention_context': intention_context(bundle,match,number),
                                      'position': queue_reference(bundle, match, queue)}
            limited['batches'].append(batch)
            limited['rows'][number].append(returned)
        return response(200, dict(common, payload=limited))
