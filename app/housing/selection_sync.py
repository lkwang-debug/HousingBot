"""Verified selection metadata refresh; private records never enter reports."""
import json
from pathlib import Path
from bisect import bisect_left
from sqlalchemy import select


async def ingest_selection(session,client,notice,*,force=False):
    from app.housing.ingestion import _state,_manifest,_finish
    from app.housing.models import HousingMeta,utcnow
    from app.housing.qualification import cache_root,digest
    from app.housing.lookup_data import final_selection
    from app.housing.batches import batch_key
    if '选房' not in (notice.title or '') or not batch_key(notice):return
    if not any('选房' in a.get('name','') and any(t in a.get('name','') for t in ['名单','排序','排位']) for a in notice.attachments or []):return
    state=_state(session,notice,'selection');manifest=_manifest(notice,'selection')
    if not force and state.manifest_hash==manifest and state.parser_version=='selection-3' and state.next_attempt_at and state.next_attempt_at>utcnow():return
    previous=session.get(HousingMeta,f'selection:{notice.id}')
    previous_version=previous.value.get('version') if previous else None
    parser_changed=state.parser_version!='selection-3'
    # Commit the claim before the downloader; final_selection does not hold a
    # transaction while waiting when invoked here.
    if not session.info.get('housing_dry_run'):session.commit()
    try:
        rows,info=await final_selection(session,client,batch_key(notice))
        if not rows or not info or not info['verified']:raise ValueError('正式排序附件未完整核实')
        grouped={}
        for row in rows.values():
            if not row.get('band') or not row.get('queue'):raise ValueError('正式排序缺少档位或队列证据')
            grouped.setdefault(row['band'],[]).append(row['rank'])
        for values in grouped.values():
            if len(set(values))!=len(values):raise ValueError('正式排位存在同档重复，需核对轮次')
            values.sort()
        for r in rows.values():r['ahead_same_band']=bisect_left(grouped[r['band']],r['rank']);r['scope_verified']=True
        updates={}
        from app.housing.qualification import parse_pdf
        for a in notice.attachments or []:
            if not any(t in a.get('name','') for t in ['队列调整','调整为','调整至']):continue
            if a.get('kind')!='pdf':continue
            data=await client.get_bytes(a['url'],referer=notice.canonical_url)
            for row in parse_pdf(data,'qualified',a['url'],a['name']):
                if not row['queue']:raise ValueError('队列调整附件未明确队列')
                updates[row['apply_no']]={'queue':row['queue'],'band':row['band'],'url':a['url'],
                    'published_at':(notice.published_at or notice.first_seen_at).isoformat(),'notice_id':notice.id}
        version=digest([rows,updates]);root=cache_root();root.mkdir(exist_ok=True)
        file='selection-'+version+'.json'
        if not session.info.get('housing_dry_run'):
            temp=root/(file+'.tmp');temp.write_text(json.dumps({'rows':rows,'notice':info,'queue_updates':updates},ensure_ascii=False),encoding='utf-8');temp.replace(root/file)
        if previous is None:previous=HousingMeta(key=f'selection:{notice.id}');session.add(previous)
        previous.value={'version':version,'file':file,'verified':True}
        _finish(state,manifest,{'semantic':version},[],session=session)
        if previous_version and previous_version!=version and not parser_changed:
            from app.housing.events import business_version,record_revision
            old_business=notice.business_version
            notice.attachment_version=digest([notice.attachment_version,'selection',version]);notice.business_version=business_version(notice);notice.event_at=utcnow()
            record_revision(session,notice,old_business,at=notice.event_at,reason='official-selection-changed')
        state.manifest_hash=_manifest(notice,'selection')
    except Exception as exc:_finish(state,manifest,{},[str(exc) if isinstance(exc,ValueError) else type(exc).__name__],session=session)
    state.parser_version='selection-3';session.flush()
