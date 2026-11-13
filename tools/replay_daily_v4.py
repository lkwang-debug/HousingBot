"""Read-only report replay and coverage audit; creates local review artifacts only."""
from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from app.db import SessionLocal
from app.housing.reporting import build_snapshot
from app.housing.report_render import render_report_parts
from app.housing.models import HousingMeta


def write_preview(out,name,part):
    (out/(name+'.md')).write_text(part.content,encoding='utf-8')
    (out/(name+'.html')).write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:12px;font-family:Arial,"Microsoft YaHei",sans-serif}h2{font-size:21px}h3{font-size:18px}h4{font-size:16px}a{color:#075da9}summary{color:#075da9;cursor:pointer}</style>'+part.wire_html,encoding='utf-8')


def replay(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);results=[]
    with SessionLocal() as session:
        for name,kind,stamp in [('20261024-digest','digest',datetime(2026,10,24,8,30)),
                                ('20261024-evening','evening',datetime(2026,10,24,20,30)),
                                ('20261025-digest','digest',datetime(2026,10,25,8,30))]:
            snapshot=build_snapshot(session,kind,now=stamp)
            old=session.get(HousingMeta,f'report:{kind}:{stamp:%Y-%m-%d}')
            if old:
                audit=old.value.get('audit',{})
                snapshot.source_status=audit.get('source_status',snapshot.source_status)
                if audit.get('source_updated_at'):snapshot.source_updated_at=datetime.fromisoformat(audit['source_updated_at'])
            part,=render_report_parts(snapshot);write_preview(out,name,part)
            doc=BeautifulSoup(part.wire_html,'html.parser')
            for detail in doc.select('details'):detail.decompose()
            oldchars=len(old.value['parts'][0]['content']) if old else None
            results.append({'name':name,'baseline':snapshot.baseline_key,'old_chars':oldchars,'new_chars':len(part.content),
                'visible_chars':len(doc.get_text('',strip=True)),'wire_utf16':len(part.wire_html.encode('utf-16-le'))//2,
                'open':snapshot.totals.open_batches,'tracked':snapshot.totals.in_transit_batches,
                'new_notices':snapshot.totals.new_notices,'updated_notices':snapshot.totals.updated_notices,
                'part_count':1,'codes':list(part.batch_codes),
                'partitions':{k:{'open':v.open_batches,'tracked':v.in_transit_batches} for k,v in snapshot.housing_totals.items()},
                'exited':snapshot.archived_cards})
            assert {'B147','B501','B803'}.isdisjoint(part.batch_codes)
            assert snapshot.totals.open_batches==3 and snapshot.housing_totals['gongzu'].open_batches==2 and snapshot.housing_totals['mixed'].open_batches==1
        # Full layouts use real source data but are explicitly labelled as demos.
        for code in ('B2249','B2270','B2379','B4'):
            snap=build_snapshot(session,'digest',now=datetime(2026,10,25,8,30))
            for c in snap.cards:
                if c.code==code:
                    c.references[0].is_new=True;c.delta_reasons=['真实数据排版示例，不表示官方发布新公告']
            part,=render_report_parts(snap);write_preview(out,'layout-'+code,part)
            assert len(part.wire_html.encode('utf-16-le'))//2<=19800
    result={'replays':results,'no_messages_sent':True,'frozen_history_modified':False}
    (out/'replay-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    lines=['# 历史退出核对清单','', '退出日报不表示整批全部配完；历史记录、查询及后台来源检查保留。','']
    for item in results[-1]['exited']:
        lines.extend([f"- {item['code']} {item['title']}：{item['reason']}",
            *[f"  - {a['kind']}：{a['start']}—{a['end']}；{a['url']}" for a in item['actions']]])
    (out/'history-exits.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k not in {'codes','exited'}} for r in results],ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);replay(parser.parse_args().output)
