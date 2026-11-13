"""Read-only city/district availability inventory; never sends a report."""
import argparse
import json
from pathlib import Path
from sqlalchemy import select
from app.db import SessionLocal
from app.housing.models import HousingNotice,utcnow
from app.housing.reporting import build_snapshot,REGIONS
from app.housing.selection_progress import source_candidates


def audit():
    with SessionLocal() as s:
        snap=build_snapshot(s,'evening',now=utcnow());batches=[]
        for c in snap.cards:
            if c.profile.housing_type!='baozu':continue
            urls=list(dict.fromkeys(u for id in c.notice_ids if (n:=s.get(HousingNotice,id)) for u in source_candidates(n)))
            p=c.selection_progress or {};scopes=p.get('scopes',[])
            batches.append({'code':c.code,'region':c.profile.region,'stage':c.stage,'audience':c.profile.audience,
                'sources':urls,'scopes':len(scopes),'verified_types':sum(sum(r['level']=='type' for r in v['rows']) for v in scopes),
                'state':'有已核实范围' if scopes else '需登录后导入' if '登录' in (p.get('issue') or '') else '尚未核实实际结果' if c.stage in {'selection','signing'} else '未进入当前选房阶段',
                'issue':p.get('issue','')})
        return {'generated_at':utcnow().isoformat(),'regions':[{'region':r,'tracking_baozu':sum(b['region']==r for b in batches),
                'selection_batches':sum(b['region']==r and b['stage'] in {'selection','signing'} for b in batches),
                'available_batches':sum(b['region']==r and b['scopes']>0 for b in batches)} for r in ['深圳市',*REGIONS]],
                'batches':batches,'note':'只盘点已采集、仍在跟进的保租房；未采到不代表官方没有资料，不把演练或截图写入正式统计。'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    result=audit();(out/'coverage.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    lines=['# 选房进度覆盖清单','','|区域|跟进保租房|选房／签约批次|已有可靠进度|','|---|---:|---:|---:|']
    lines+=['|'+ '|'.join(str(r[k]) for k in ['region','tracking_baozu','selection_batches','available_batches'])+'|' for r in result['regions']]
    lines+=['','|批次|区域|对象|当前资料状态|','|---|---|---|---|']
    lines+=['|'+ '|'.join([r['code'],r['region'],r['audience'],r['state']])+'|' for r in result['batches']]
    lines+=['',result['note']];(out/'coverage.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps({'regions':len(result['regions']),'tracking_baozu':len(result['batches']),'batches_with_verified_progress':sum(b['scopes']>0 for b in result['batches'])},ensure_ascii=False))
