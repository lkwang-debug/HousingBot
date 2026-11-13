"""Versioned official review lists. Public aggregates; identifier-only private cache."""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import select

from app.housing.batches import batch_key
from app.housing.models import HousingAttachmentState, HousingMeta, HousingNotice, utcnow

PARSER_VERSION = 'qualification-10'
ATTACHMENT_PARSER_VERSION = 'qualification-10'
STATUSES = ('qualified', 'ineligible', 'withdrawn', 'not_reviewed')
LABELS = {'qualified': '公示合格', 'ineligible': '不合格', 'withdrawn': '撤销认租',
          'not_reviewed': '未纳入本轮审核', 'not_found': '未见于已核实名单'}
IDENTIFIER = r'[A-Z]{1,6}\d[A-Z0-9-]{5,63}'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cache_root():
    from app.config import PROJECT_ROOT
    return PROJECT_ROOT / 'data' / 'qualification-private'


def attachment_status(name):
    text = re.sub(r'\s+', '', name)
    if re.search(r'未纳入|不纳入|暂不审核|未审核', text): return 'not_reviewed'
    if '不合格' in text and re.search(r'撤销|撤回|放弃', text): return 'mixed_negative'
    if re.search(r'撤销|撤回|放弃认租', text): return 'withdrawn'
    if '不合格' in text or '未通过' in text: return 'ineligible'
    if '合格' in text or '审核通过' in text: return 'qualified'
    return ''


def review_files(notice):
    if '选房' in (notice.title or '') and not re.search(r'终审|资格核查结果|资格审核结果|资格审查结果',notice.title or ''):return []
    return [a for a in notice.attachments or [] if attachment_status(a.get('name', ''))
            and re.search(r'名单|名册|结果', a.get('name', ''))]


def band_key(text):
    """Only explicit area/layout evidence; never turn room counts into m²."""
    compact = re.sub(r'\s+', '', text)
    area = re.search(r'(?:建筑面积|认租面积|面积档位)?(\d+(?:\.\d+)?)(?:㎡|平方米|平米)(及以上|以上|左右|以下)?', compact)
    if area: return area[1] + ('+' if area[2] in ('及以上', '以上') else '')
    room = re.search(r'(单身公寓|单房|小三房|[一二两三四五六]房(?:[一二两三四]厅)?)', compact)
    return room[1].replace('二房', '两房') if room else 'all'


def declared_review_counts(text):
    return {band_key(m[0]):int(m[1]) for m in re.findall(
        r'(建筑面积\s*\d+(?:\.\d+)?\s*(?:㎡|平方米|平米)\s*(?:左右|及以上|以上)?)[^；。]{0,100}?审核(\d+)户',text or '')}


def _row(identifier, band, status, queue='', order=None, source=''):
    if not re.fullmatch(IDENTIFIER, identifier) or status not in STATUSES:
        raise ValueError('名单编号或审核状态未可靠识别')
    return {'apply_no': identifier, 'band': band, 'status': status, 'queue': queue,
            'order': order, 'source': source}


def project_intentions(text, catalog):
    """Return catalog names only: never persist raw columns with personal data."""
    compact=re.sub(r'\s+','',text)
    found=[]
    for name in catalog or []:
        aliases=[name,re.sub(r'[（(].*?[）)]','',name)]
        aliases+=re.findall(r'[（(]([^）)]+)[）)]',name)
        if any(len(a)>=3 and re.sub(r'\s+','',a) in compact for a in aliases):found.append(name)
    return sorted(set(found))


def parse_pdf(data, status, source='', label='', *, project_catalog=None):
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(data))
    if len(pdf.pages) > 1200: raise ValueError('审核附件超过安全页数')
    records = [];text_reasons={};prior_band=False;prior_queue=False;prior_rank=False
    for page in pdf.pages:
        text = page.extract_text() or ''
        # Some generators glue the first row ordinal to the preceding header.
        # It is only a boundary marker, never an inferred ranking column.
        anchors = list(re.finditer(r'(?<![A-Z0-9])(?:\d{1,7}\s+)?(' + IDENTIFIER + r')(?![A-Z0-9*＊])', text))
        ids = re.findall(r'(?<![A-Z0-9])' + IDENTIFIER + r'(?![A-Z0-9*＊])', text)
        # Identification must be complete, not merely a few successful rows.
        ids = [no for no in ids if re.search(r'\d', no)]
        if len(ids) != len(anchors): raise ValueError('PDF存在未完整识别的编号行')
        header=re.sub(r'\s+','',text[:anchors[0].start()] if anchors else text)
        has_band=prior_band or bool(re.search(r'意向分类|认租面积|认租户型|面积档位|户型档位',header))
        has_queue=prior_queue or '队列' in header
        has_rank=prior_rank or bool(re.search(r'选房(?:排位|排序|顺序)号?',header))
        prior_band,prior_queue,prior_rank=has_band,has_queue,has_rank
        for i, anchor in enumerate(anchors):
            chunk = text[anchor.end():anchors[i+1].start() if i+1 < len(anchors) else len(text)]
            chunk = re.split(r'\n\s*(?:附件\s*\d|20\d{2}年.{0,50}名单)', chunk)[0]
            # Strip masked certificate and name fields; neither is persisted.
            band = band_key(chunk) if has_band else band_key(label)
            queue = re.search(r'第[一二三四五六七八九十]+队列|(?:优先|其他|普通)[^\s；;，,]{0,6}队列', chunk) if has_queue else None
            outcome = status
            if status == 'mixed_negative':
                outcome = 'withdrawn' if re.search(r'撤销|撤回|主动放弃', chunk) else 'ineligible'
            order = None
            if outcome in {'qualified', 'not_reviewed'} and re.search(r'原.{0,10}摇号顺', re.sub(r'\s+', '', text[:350])):
                number = re.search(r'\s(\d{1,7})\s*$', chunk)
                if number: order = int(number[1])
            record=_row(anchor[1], band, outcome, queue[0] if queue else '', order, source)
            if has_rank:
                leading=anchor[0].split(anchor[1])[0].strip()
                if leading.isdigit():record['selection_rank']=int(leading)
            if status in {'ineligible','mixed_negative'}:
                from app.housing.review_details import reason_text,clean
                compact=clean(chunk)
                start=re.search(r'主申请人|共同申请人|申请人|配偶|父母|未按规定|成年子女|未成年子女|有效家庭人口|未提供|撤销认租',compact)
                if start:text_reasons[anchor[1]]=reason_text(compact[start.start():])
            if project_catalog is not None:
                intentions=project_intentions(chunk,project_catalog) if re.search(r'认租意向|认租项目|意向小区',header) else []
                record.update(intentions=intentions,intentions_verified=bool(intentions))
                from app.housing.review_details import concrete_intentions
                types=concrete_intentions(chunk,project_catalog) if intentions else []
                record.update(intention_types=types,types_verified=bool(types) and set(intentions)=={t['project'] for t in types})
            records.append(record)
    if not records: raise ValueError('审核附件没有可识别的编号记录')
    if status in {'ineligible','mixed_negative'}:
        from app.housing.review_details import pdf_reason_columns
        try:reasons=pdf_reason_columns(data)
        except Exception:reasons={}
        for r in records:
            column=reasons.get(r['apply_no'],'');plain=text_reasons.get(r['apply_no'],'')
            # The explicit reason column establishes the meaning. A ruled cell
            # may contain overflow from the previous row; the source text's
            # identifier boundaries retain the correct whole-row reading order.
            value=plain if column and plain else column
            r.update(reason=value,reason_verified=bool(value))
            if status=='mixed_negative' and value:r['status']='withdrawn' if re.search(r'撤销|撤回|主动放弃',value) else 'ineligible'
    return records


def parse_table(table, status, source='', label='', *, project_catalog=None):
    for start, row in enumerate(table[:20]):
        header = [re.sub(r'\s+', '', str(c or '')) for c in row]
        no = next((i for i, c in enumerate(header) if c in {'认租编号', '认租申请编号', '申请编号', '受理编号','备案回执号'}), None)
        if no is None: continue
        def column(terms): return next((i for i, c in enumerate(header) if c in terms), None)
        band = column({'意向分类', '认租档位', '认租面积档位', '认租户型', '户型', '面积档位'})
        queue = column({'队列分类', '队列', '选房队列'})
        result = column({'审核结果', '审核状态', '不合格原因', '原因', '备注'})
        order = column({'原意向摇号顺序号', '原摇号顺序号', '摇号顺序号'})
        intent = column({'认租意向','认租项目','意向小区','意向项目'})
        records = []
        for cells in table[start+1:]:
            def value(i): return str(cells[i] or '').strip() if i is not None and i < len(cells) else ''
            identifier = re.sub(r'\s+', '', value(no)).upper()
            if not identifier: continue
            if not re.fullmatch(IDENTIFIER, identifier): raise ValueError('表格存在无法识别的编号行')
            outcome = status
            if status == 'mixed_negative':
                if result is None: raise ValueError('不合格与撤销无法分开')
                outcome = 'withdrawn' if re.search(r'撤销|撤回|主动放弃', value(result)) else 'ineligible'
            record=_row(identifier, band_key(value(band) or label), outcome, value(queue),
                        int(value(order)) if value(order).isdigit() else None, source)
            if status in {'ineligible','mixed_negative'}:
                from app.housing.review_details import reason_text
                reason=reason_text(value(result))
                record.update(reason=reason,reason_verified=bool(reason))
            if project_catalog is not None:
                intentions=project_intentions(value(intent),project_catalog)
                record.update(intentions=intentions,intentions_verified=bool(intentions))
                from app.housing.review_details import concrete_intentions
                types=concrete_intentions(value(intent),project_catalog)
                record.update(intention_types=types,types_verified=bool(types) and set(intentions)=={t['project'] for t in types})
            records.append(record)
        if not records: raise ValueError('表格没有审核记录')
        return records
    raise ValueError('未找到明确的编号列')


def normalize_records(records):
    unique = {}
    for record in records:
        no = record['apply_no']
        if no in unique and unique[no] != record:
            # Exact mirror rows are okay; incompatible outcomes/scopes are not.
            ignored={'source','intentions','intentions_verified','intention_types','types_verified'}
            if {k:v for k,v in unique[no].items() if k not in ignored} != {k:v for k,v in record.items() if k not in ignored}:
                raise ValueError('同编号存在冲突审核记录，需核对轮次')
            if 'intentions' in record or 'intentions' in unique[no]:
                types={json.dumps(t,sort_keys=True):t for t in [*unique[no].get('intention_types',[]),*record.get('intention_types',[])]}
                record=dict(record,intentions=sorted(set(record.get('intentions',[]))|set(unique[no].get('intentions',[]))),
                            intentions_verified=record.get('intentions_verified',False) and unique[no].get('intentions_verified',False),
                            intention_types=list(types.values()),types_verified=record.get('types_verified',False) and unique[no].get('types_verified',False))
        unique[no] = record
    return unique


def summarize_records(records):
    counts = {}
    for r in records.values():
        band = counts.setdefault(r['band'], {s:0 for s in STATUSES} | {'queues': {}})
        band[r['status']] += 1
        if r['status'] == 'qualified' and r['queue']:
            band['queues'][r['queue']] = band['queues'].get(r['queue'], 0) + 1
    for band in counts.values():
        band['queue_complete'] = sum(band['queues'].values()) == band['qualified']
        total = band['qualified'] + band['ineligible']
        band['reviewed'] = total
        band['pass_rate'] = str((Decimal(band['qualified'])*100/Decimal(total)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if total else None
    return counts


def _metadata(session, notice_id):
    record = session.get(HousingMeta, f'qualification:{notice_id}')
    return record.value if record else None


def parse_attachment(data,kind,status,url,label,*,cache=True,project_catalog=None):
    """Identical official mirrors/rechecks reuse a complete private parse."""
    parser=ATTACHMENT_PARSER_VERSION if status in {'ineligible','mixed_negative'} else 'qualification-7'
    version=digest([parser,hashlib.sha256(data).hexdigest(),kind,status,band_key(label),project_catalog])
    path=cache_root()/'parsed'/(version+'.json')
    if cache and path.exists():
        return [dict(r,source=url) for r in json.loads(path.read_text(encoding='utf-8'))]
    kwargs={'project_catalog':project_catalog} if project_catalog else {}
    if kind=='pdf':rows=parse_pdf(data,status,url,label,**kwargs)
    elif kind in {'xls','xlsx','et'}:
        from app.housing.lottery import _read_table
        rows=parse_table(_read_table(data,'xls' if kind=='et' else kind),status,url,label,**kwargs)
    else:raise ValueError('不支持的核查名单附件')
    if cache:
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps([{k:v for k,v in r.items() if k!='source'} for r in rows],ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        temp.replace(path)
    return rows


async def ingest_review(session, client, notice, *, force=False):
    from app.housing.ingestion import _state, _manifest, _finish
    files = review_files(notice)
    key = batch_key(notice)
    if not key or not files: return None
    state = _state(session, notice, 'qualification')
    parser_changed=state.parser_version!=PARSER_VERSION or (_metadata(session,notice.id) or {}).get('parser_version')!=PARSER_VERSION
    from app.housing.inventory import batch_inventory
    inventory=batch_inventory(session,key) or {}
    catalog=sorted({p['name'] for p in inventory.get('projects',[]) if p.get('rows') and p.get('name') not in {'项目基本情况','房源概况'}})
    manifest = digest([_manifest(notice, 'qualification'), PARSER_VERSION,catalog])
    if not force and state.manifest_hash == manifest and state.parser_version == PARSER_VERSION and state.next_attempt_at and state.next_attempt_at > utcnow():
        return _metadata(session, notice.id)
    errors, hashes, rows = [], {}, []
    if len(files) > 24: errors.append('核查附件超过安全数量')
    for a in files[:24]:
        try:
            # No DB write transaction is held while waiting for the network.
            if not session.info.get('housing_dry_run'): session.commit()
            data = await client.get_bytes(a['url'], referer=notice.canonical_url)
            hashes[a['url']] = hashlib.sha256(data).hexdigest()
            kind = a.get('kind') or a['url'].rsplit('.',1)[-1].lower()
            status = attachment_status(a['name'])
            parsed=parse_attachment(data,kind,status,a['url'],a['name'],cache=not session.info.get('housing_dry_run'),project_catalog=catalog or None)
            rows.extend(parsed)
        except Exception as exc:
            errors.append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    records = {}
    if not errors:
        try: records = normalize_records(rows)
        except ValueError as exc: errors.append(str(exc))
    if not errors:
        # Check declared stage totals when provided. Only explicit counts are
        # constraints; absent data is not zero or an inferred failed household.
        body=re.sub(r'\s+','',notice.body_text or '')
        for status,pattern in [('qualified',r'(?<!不)(?:合格认租家庭|合格家庭)(\d+)户'),
                               ('ineligible',r'(?<!及)(?:不合格认租家庭|不合格家庭)(\d+)户')]:
            declared=re.search(pattern,body)
            if declared and sum(r['status']==status for r in records.values())!=int(declared[1]):
                errors.append('正文与名单人数不一致，未替换已核实版本')
        counts=summarize_records(records)
        for band,number in declared_review_counts(notice.body_text).items():
            if counts.get(band,{}).get('reviewed')!=number:
                errors.append('正文与对应档位已出审核结论户数不一致')
    _finish(state, manifest, hashes, errors, session=session)
    state.parser_version = PARSER_VERSION
    if errors: return _metadata(session, notice.id)
    phase = 'final' if '终审' in notice.title else 'publicity'
    counts = summarize_records(records)
    # The private object is content addressed and is never in lookup/static.
    version = digest([key, phase, records])
    metadata = {'schema':'qualification-v1','notice_id':notice.id,'batch_key':key,'version':version,
                'parser_version':PARSER_VERSION,
                'published_at':(notice.published_at or notice.first_seen_at).isoformat(), 'phase':phase,
                'url':notice.canonical_url,'title':notice.title,'counts':counts,'total':len(records),
                'scope_note':'本次公告及所附名单范围；未列出的家庭不据此认定不合格',
                'body_reviewed':declared_review_counts(notice.body_text),
                'private_file':version+'.json'}
    metadata['disclosed_statuses']=sorted({s for a in files for s in (
        ('ineligible','withdrawn') if attachment_status(a['name'])=='mixed_negative' else (attachment_status(a['name']),))})
    from app.housing.review_details import reason_summary
    metadata['reason_summary']=reason_summary(records) if 'ineligible' in metadata['disclosed_statuses'] else None
    metadata['count_unit']='家' if re.search(r'企业|单位',notice.title or '') and not re.search(r'个人|家庭',notice.title or '') else '户'
    metadata['count_subject']='企业／单位' if metadata['count_unit']=='家' else '申请家庭／个人'
    if not session.info.get('housing_dry_run'):
        root = cache_root();root.mkdir(parents=True, exist_ok=True)
        tmp = root/(version+'.tmp')
        tmp.write_text(json.dumps({'metadata':metadata,'records':records},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        tmp.replace(root/metadata['private_file'])
    record = session.get(HousingMeta, f'qualification:{notice.id}')
    previous=record.value if record else None
    if record is None: record = HousingMeta(key=f'qualification:{notice.id}');session.add(record)
    record.value = metadata
    # Seed backfills without replay. Subsequent semantic changes are genuine
    # business changes; PDF metadata/whitespace does not generate events.
    if previous and previous.get('version')!=version and not parser_changed:
        from app.housing.events import business_version,record_revision
        old_business=notice.business_version
        notice.attachment_version=digest(['qualification',notice.attachment_version,version])
        notice.business_version=business_version(notice)
        notice.event_at=utcnow()
        record_revision(session,notice,old_business,at=notice.event_at,reason='official-qualification-changed')
    state.manifest_hash=digest([_manifest(notice,'qualification'),PARSER_VERSION,catalog])
    session.flush()
    return metadata


def review_history(session, key, *, at=None):
    notices = list(session.scalars(select(HousingNotice).where(HousingNotice.batch_key_v2 == key))) if key else []
    result=[]
    for n in notices:
        date=n.published_at or n.first_seen_at
        if at and date.replace(tzinfo=None)>at.replace(tzinfo=None): continue
        if n.published_unverified: continue
        meta=_metadata(session,n.id)
        if meta:
            meta=dict(meta)
            state = session.scalar(select(HousingAttachmentState).where(
                HousingAttachmentState.notice_id==n.id,HousingAttachmentState.purpose=='qualification'))
            meta['pending'] = bool(state and state.status!='ok')
            result.append(meta)
        elif review_files(n):
            result.append({'notice_id':n.id,'published_at':date.isoformat(),'url':n.canonical_url,
                           'phase':'final' if '终审' in n.title else 'publicity','pending':True,'counts':{},'version':''})
    # Equal evidence from mirrors is one event. Distinct scopes/rounds remain.
    unique={}
    for m in sorted(result,key=lambda m:(m['published_at'],m['notice_id'])):
        unique[(m.get('version') or str(m['notice_id']))]=m
    return list(unique.values())


def public_review(session, key, supply=(), *, at=None):
    history=review_history(session,key,at=at)
    if not history:return None
    latest=history[-1]
    pending_notice=latest if latest.get('pending') else None
    if not latest.get('counts'):
        verified=[m for m in history if m.get('counts')]
        if verified: latest=verified[-1]
    result={k:v for k,v in latest.items() if k not in {'private_file','body_reviewed'}}
    if pending_notice:
        result['pending']=True
        result['pending_notice']={k:pending_notice.get(k) for k in ('notice_id','published_at','url')}
    catalog={i['category']:i for i in supply}
    result['bands']=[]
    for band,counts in latest['counts'].items():
        counts=dict(counts)
        disclosed=set(latest.get('disclosed_statuses',STATUSES))
        for status in STATUSES:
            if status not in disclosed:counts[status]=None
        if not {'qualified','ineligible'}<=disclosed:
            counts['reviewed']=None;counts['pass_rate']=None
        units=catalog.get(band,{}).get('initial_units')
        row={'band':band,'label':catalog.get(band,{}).get('label',band+'㎡' if band!='all' else '本批'),**counts,
             'initial_units':units,'qualified_ratio':None}
        if units and counts['qualified'] is not None and catalog[band].get('competition_ratio') is not None:
            row['qualified_ratio']=str((Decimal(counts['qualified'])/Decimal(units)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP))
        announced=latest.get('body_reviewed',{}).get(band)
        row['count_issue']='正文审核户数与已出结论户数不一致，需核对' if announced is not None and announced!=counts['reviewed'] else ''
        if row['count_issue']:row['qualified_ratio']=None;row['pass_rate']=None
        result['bands'].append(row)
    result['queue_note']='各队列合格户数：本次未公开' if any(not b['queue_complete'] for b in result['bands']) else ''
    from app.housing.daily_lifecycle import action_windows
    current_notice=session.get(HousingNotice,latest['notice_id'])
    result['handling']=action_windows(current_notice) if current_notice else []
    disclosed=set(latest.get('disclosed_statuses',STATUSES))
    result['overall']={s:sum(c[s] for c in latest['counts'].values()) if s in disclosed else None for s in STATUSES}
    q,i=result['overall']['qualified'],result['overall']['ineligible']
    result['overall']['reviewed']=q+i if q is not None and i is not None else None
    result['overall']['pass_rate']=str((Decimal(q)*100/Decimal(q+i)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if q is not None and i is not None and q+i else None
    return result


def attach_reviews(session, payload):
    """Bind individual updates to the original lottery identity, not row ordinal."""
    for index,batch in enumerate(payload['batches']):
        key=batch.get('batch_key')
        if not key: continue
        supply=[dict(c.get('competition') or {},category=c['key']) for c in batch['c']]
        summary=public_review(session,key,supply)
        if not summary:continue
        batch['qualification']=summary
        batch['handling']=summary.get('handling',[])
        merged={}
        for meta in review_history(session,key):
            filename=meta.get('private_file','')
            if not filename or Path(filename).name!=filename:continue
            path=cache_root()/filename
            if not path.exists():continue
            data=json.loads(path.read_text(encoding='utf-8'))
            for no,r in data['records'].items():
                # Later rounds replace only explicitly covered households.
                merged[no]={**r,'published_at':meta['published_at'],'phase':meta['phase'],
                            'notice_id':meta['notice_id'],'url':meta['url'],'version':meta['version'],
                            'label':('终审合格' if meta['phase']=='final' else '公示合格') if r['status']=='qualified' else LABELS[r['status']]}
        checked={}
        for no,record in merged.items():
            matches=[m for m in payload['rows'].get(no,[]) if m[0]==index]
            if len(matches)!=1:continue
            match=matches[0];band=batch['c'][match[1]]['key']
            if record['band']!=band or (record['order'] is not None and record['order']!=match[3]):
                checked[no]={'status':'conflict','label':'最新名单与原摇号记录存在差异，待核对','url':record['url'],
                             'published_at':record['published_at'],'queue':'','phase':record['phase']}
            else: checked[no]=record
        batch['review_rows']=checked
        from app.housing.review_statistics import statistics
        scope={no:r for no,r in checked.items() if r.get('notice_id')==summary['notice_id'] and r['status'] in STATUSES}
        batch['review_statistics']=statistics(payload,index,scope,batch.get('opportunity',{}).get('projects',[]),summary.get('disclosed_statuses',STATUSES))
    return payload
