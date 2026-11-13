"""Inventory all collected regions and lottery batches; never sends or edits notices."""
import argparse
import json
from collections import Counter
from pathlib import Path
from sqlalchemy import select
from app.db import SessionLocal
from app.housing.models import HousingNotice,HousingAttachmentState
from app.housing.reporting import build_snapshot,REGIONS
from app.housing.lookup_tokens import beijing_now
from app.housing.qualification import review_files,public_review


def audit():
    with SessionLocal() as s:
        snapshot=build_snapshot(s,'evening',now=beijing_now().replace(tzinfo=None))
        cards={s.get(HousingNotice,c.batch_id).batch_key_v2:c for c in snapshot.cards if c.batch_id}
        collected=list(s.scalars(select(HousingNotice).where(HousingNotice.is_target.is_(True))))
        lotteries={n.batch_key_v2 or 'N'+str(n.id):n for n in sorted(collected,key=lambda n:n.id,reverse=True) if n.category=='lottery_result'}
        batch_rows=[]
        for key in sorted(set(cards)|set(lotteries)):
            c=cards.get(key);lottery=lotteries.get(key)
            region=c.profile.region if c else lottery.region_code
            review=c.qualification if c else public_review(s,key)
            nodes=[n for n in collected if n.batch_key_v2==key and review_files(n)]
            states=[r for n in nodes for r in s.scalars(select(HousingAttachmentState).where(HousingAttachmentState.notice_id==n.id,HousingAttachmentState.purpose=='qualification'))]
            stats=(review or {}).get('statistics') or {}
            status='verified' if review and not review.get('pending') else 'incomplete' if nodes else 'not_collected'
            batch_rows.append({'batch':c.code if c else 'N'+str(lottery.id),'region':region,'tracking':bool(c),
                'stage':c.stage if c else '历史记录，不计日报','lottery_collected':bool(lottery),'review_status':status,
                'review_notice':(review or {}).get('notice_id'),'review_attachments':sum(len(review_files(n)) for n in nodes),
                'historical_queue_statistics':bool(stats.get('original_queue_rows')),
                'project_intentions':len(stats.get('project_intentions',[])),
                'concrete_housing_types':len(stats.get('concrete_units',[])),
                'concrete_types_verified':sum(r.get('complete',False) for r in stats.get('concrete_units',[])),
                'reason_coverage':(review or {}).get('reason_summary',{}).get('coverage') if (review or {}).get('reason_summary') else None,
                'handling_windows':len((review or {}).get('handling',[])),
                'gaps':[v for v in [stats.get('issue'),*(r.error for r in states if r.status!='ok')] if v] or
                        (['尚未采集到可靠关联的资格核查名单'] if not nodes else [])})
        region_rows=[]
        for region in ['深圳市',*REGIONS]:
            rows=[r for r in batch_rows if (r['region'] or '深圳市')==region]
            notices=[n for n in collected if (n.region_code or '深圳市')==region]
            region_rows.append({'region':region,'collected_notices':len(notices),'tracking_batches':sum(r['tracking'] for r in rows),
                'tracking_lottery_batches':sum(r['tracking'] and r['lottery_collected'] for r in rows),
                'tracking_review_verified':sum(r['tracking'] and r['review_status']=='verified' for r in rows),
                'tracking_review_incomplete':sum(r['tracking'] and r['review_status']=='incomplete' for r in rows)})
        return {'generated_at':beijing_now().isoformat(),'regions':region_rows,'batches':batch_rows,
                'note':'只代表已配置来源及已采集资料；未采集到不代表官方没有发布或没有房源。历史批次保留查询但不恢复日报。'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    result=audit();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    (out/'coverage.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 全市核查统计覆盖盘点','','|区域|已采公告|当前跟进|其中已采摇号|核查已验证|核查不完整|','|---|---:|---:|---:|---:|---:|']
    lines += ['|'+ '|'.join(str(r[k]) for k in ['region','collected_notices','tracking_batches','tracking_lottery_batches','tracking_review_verified','tracking_review_incomplete'])+'|' for r in result['regions']]
    lines += ['',result['note'],'','## 批次资料及缺项','','|批次|区域|跟进|核查资料|缺项|','|---|---|---|---|---|']
    lines += ['|'+ '|'.join([r['batch'],r['region'],str(r['tracking']),r['review_status'],'；'.join(r['gaps']).replace('|','／')])+'|' for r in result['batches']]
    (out/'coverage.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'regions':len(result['regions']),'collected_lottery_and_tracking_batches':len(result['batches']),
                      'tracking_review_status':dict(Counter(r['review_status'] for r in result['batches'] if r['tracking']))},ensure_ascii=False))
