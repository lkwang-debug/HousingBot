"""Anonymous cross-tabs. Historical queue groupings never become current queues."""
from collections import Counter,defaultdict
from decimal import Decimal,ROUND_HALF_UP
from functools import lru_cache
import hashlib
import json
from pathlib import Path

STATUSES=('qualified','ineligible','withdrawn','not_reviewed')


def statistics(payload,index,records,projects=(),disclosed=STATUSES):
    batch=payload['batches'][index];cross=defaultdict(Counter);matched=0;conflicts=0
    project_sets=defaultdict(set);known=Counter();qualified=Counter()
    for number,r in records.items():
        if r['status']=='qualified':
            qualified[r['band']]+=1
            if r.get('intentions_verified'):
                known[r['band']]+=1
                for name in set(r.get('intentions',[])):project_sets[(r['band'],name)].add(number)
        ms=[m for m in payload.get('rows',{}).get(number,[]) if m[0]==index]
        if len(ms)!=1 or batch['c'][ms[0][1]]['key']!=r['band']:
            conflicts+=1;continue
        m=ms[0];matched+=1
        cross[(r['band'],batch['Q'][m[2]])][r['status']]+=1
    rows=[]
    for (band,queue),counts in cross.items():
        rows.append({'band':band,'original_queue':queue,**{s:counts[s] if s in disclosed else None for s in STATUSES}})
    band_order=[c['key'] for c in batch['c']]
    rows.sort(key=lambda r:(band_order.index(r['band']) if r['band'] in band_order else 999,batch['Q'].index(r['original_queue'])))
    intentions=[]
    for (band,name),numbers in project_sets.items():
        project=next((p for p in projects if p['name']==name),None)
        stock=[r['units'] for r in (project or {}).get('rows',[]) if r.get('band')==band and isinstance(r.get('units'),int)]
        complete=qualified[band]==known[band]
        units=sum(stock) if stock else None
        # Distinct bands must not be credited the same unsplit shared pool.
        overlapping=any(r.get('shared_pool') for r in (project or {}).get('rows',[]) if r.get('band')==band)
        ratio=str((Decimal(len(numbers))/Decimal(units)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if units and complete and not overlapping else None
        intentions.append({'band':band,'project':name,'qualified_households':len(numbers),
                           'initial_units':units if not overlapping else None,'ratio':ratio,'complete':complete})
    project_order=[p['name'] for p in projects]
    intentions.sort(key=lambda r:(band_order.index(r['band']) if r['band'] in band_order else 999,
                                  project_order.index(r['project']) if r['project'] in project_order else 999,r['project']))
    from app.housing.review_details import unit_summary,reason_summary
    return {'original_queue_rows':rows,'project_intentions':intentions,'concrete_units':unit_summary(records,projects),
            'reason_summary':reason_summary(records) if 'ineligible' in disclosed else None,'matched_households':matched,
            'unmatched_households':conflicts,'original_queue_label':'按原摇号分类关联，不是最新各队列人数',
            'intentions_note':'同一家庭可有多个项目意向，各项目人数不能直接相加；意向不等于最终选择。',
            'intention_missing_bands':[b for b,n in qualified.items() if known[b]!=n]}


@lru_cache(maxsize=24)
def _read_payload(filename,sha):
    from app.housing.opportunity import load_payload
    path=Path(filename)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('原名单文件与清单不一致')
    return load_payload(path)


def original_bundle(session,key):
    from app.housing.lookup import read_manifest,build_directory,source_version
    from app.housing.models import HousingNotice
    from sqlalchemy import select
    launch=session.scalar(select(HousingNotice).where(HousingNotice.batch_key_v2==key,HousingNotice.category=='rent_launch'))
    if not launch:return None,None
    manifest=read_manifest();directory=build_directory()
    for name,info in manifest.get('pages',{}).items():
        if name=='index.html' or info.get('district')!=launch.region_code:continue
        payload=_read_payload(str(directory/name),info['sha256'])
        for i,b in enumerate(payload['batches']):
            if b.get('batch_key')!=key or b.get('review_only'):continue
            notice=session.get(HousingNotice,b['id'])
            base=source_version(session,notice,include_related=False)
            if b.get('lottery_base_version',b.get('version'))!=base and b.get('version')!=source_version(session,notice):continue
            return payload,i
    return None,None


def report_statistics(session,key,review,inventory):
    from app.housing.qualification import cache_root,review_history
    history=review_history(session,key)
    meta=next((m for m in history if m['notice_id']==review['notice_id']),None)
    if not meta or not meta.get('private_file'):return None
    path=cache_root()/meta['private_file']
    if not path.exists():return None
    records=json.loads(path.read_text(encoding='utf-8'))['records']
    try:payload,index=original_bundle(session,key)
    except (ValueError,OSError):payload,index=None,None
    if payload is None:
        # Project intentions remain usable even when no original lottery exists;
        # the absence of an index is a visible gap, never a fictional queue.
        result=statistics({'batches':[{'Q':[],'c':[]}],'rows':{}},0,records,(inventory or {}).get('projects',[]),review.get('disclosed_statuses',STATUSES))
        result['issue']='未关联到可核实的原摇号索引，暂不提供原队列交叉统计。'
        return result
    result=statistics(payload,index,records,(inventory or {}).get('projects',[]),review.get('disclosed_statuses',STATUSES))
    result.update(notice_id=meta['notice_id'],published_at=meta['published_at'],phase=meta['phase'])
    return result
