"""Query verified review-only batches without fabricating a lottery rank."""
import json
from pathlib import Path

UNCONFIRMED='当前队列未公开／暂未核实'


def payloads(session,pages):
    from app.housing.reporting import build_snapshot
    from app.housing.models import HousingNotice,utcnow
    from app.housing.qualification import cache_root,review_history,public_review,LABELS
    from app.housing.lookup import source_version
    from app.housing.lookup_data import attach_inventory
    from app.housing.review_statistics import statistics
    from tools.build_lottery_lookup import _district_slug
    snapshot=build_snapshot(session,'evening',now=utcnow())
    original_keys={b.get('batch_key') for p in pages.values() for b in p['batches'] if not b.get('review_only')}
    for p in pages.values():
        keep={i:b for i,b in enumerate(p['batches']) if not (b.get('review_only') and b.get('batch_key') in original_keys)}
        remap={i:j for j,i in enumerate(keep)}
        p['batches']=list(keep.values())
        p['rows']={no:[[remap[m[0]],*m[1:]] for m in ms if m[0] in remap] for no,ms in p['rows'].items()}
        p['rows']={no:ms for no,ms in p['rows'].items() if ms}
    for card in snapshot.cards:
        q=card.qualification
        if not q or q.get('pending'):continue
        notice=session.get(HousingNotice,q['notice_id']);key=notice.batch_key_v2
        if key in original_keys:continue
        slug=_district_slug(card.profile.region)
        payload=pages.setdefault(slug+'.html',{'district':card.profile.region,'batches':[],'rows':{}})
        # Rebuild the verified scope, keeping other batches and their row indexes.
        prior=next((i for i,b in enumerate(payload['batches']) if b.get('review_only') and b.get('batch_key')==key),None)
        index=prior if prior is not None else len(payload['batches'])
        for number,ms in list(payload['rows'].items()):
            kept=[m for m in ms if m[0]!=index]
            if kept:payload['rows'][number]=kept
            else:payload['rows'].pop(number)
        records={}
        for meta in review_history(session,key):
            filename=meta.get('private_file','')
            if not filename or Path(filename).name!=filename:continue
            path=cache_root()/filename
            if not path.exists():continue
            for no,r in json.loads(path.read_text(encoding='utf-8'))['records'].items():
                records[no]={**r,'notice_id':meta['notice_id'],'published_at':meta['published_at'],'phase':meta['phase'],
                    'url':meta['url'],'label':('终审合格' if meta['phase']=='final' else '公示合格') if r['status']=='qualified' else LABELS[r['status']]}
        if not records:continue
        bands=list(dict.fromkeys(r['band'] for r in records.values()))
        queues=[UNCONFIRMED,*dict.fromkeys(r['queue'] for r in records.values() if r.get('queue'))]
        batch={'id':notice.id,'batch_key':key,'review_only':True,'version':source_version(session,notice),
               'lottery_base_version':source_version(session,notice,include_related=False),'t':notice.title,
               'd':q['published_at'][:10],'u':notice.canonical_url,'sources':[notice.canonical_url],'source_ids':[notice.id],
               'Q':queues,'R':{},'c':[{'key':band,'l':'本次名单未公开面积档位' if band=='all' else band+'㎡' if band.isdigit() else band,
                    'n':sum(r['band']==band for r in records.values()),'u':0,'q':{},'queue_complete':False,'issue':''} for band in bands],
               'review_rows':records,'qualification':public_review(session,key),'final_rows':{}}
        batch['handling']=batch['qualification'].get('handling',[])
        attach_inventory(batch,card.inventory)
        batch['review_statistics']=statistics({'batches':[batch],'rows':{}},0,records,(card.inventory or {}).get('projects',[]),q.get('disclosed_statuses',()))
        batch['review_statistics']['issue']='本批尚无可核实的原摇号索引，仅提供官方核查记录，不生成摇号或队内排名。'
        if prior is None:payload['batches'].append(batch)
        else:payload['batches'][index]=batch
        for no,r in records.items():
            payload['rows'].setdefault(no,[]).append([index,bands.index(r['band']),queues.index(r.get('queue') or UNCONFIRMED),r.get('order'),None,0,0])
    return pages
