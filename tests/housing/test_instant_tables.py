import asyncio
import json
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.housing.reporting import build_snapshot
from app.housing.report_render import render_report_parts
from app.housing.models import HousingMeta,HousingOutbox,HousingGroupState
from app.housing.inventory import capture_detail
from app.housing.parsers.detail import parse_detail
from app.housing.message_format import compact_html
from tests.housing.test_reports import notice

NOW=datetime(2026,10,26,14,0)


def fresh(session,key,**changes):
    values=dict(region='南山区',kind='gongzu',published=NOW-timedelta(hours=1),
        first_seen_at=NOW-timedelta(hours=1),event_at=NOW-timedelta(hours=1),
        application_start=NOW-timedelta(hours=2),application_end=NOW+timedelta(days=7))
    values.update(changes)
    return notice(session,key,**values)


def real_inventory(session,row):
    html=Path('tests/housing/fixtures/inventory/nanshan-2026-037.html').read_text(encoding='utf-8')
    detail=parse_detail(html,row.canonical_url);row.body_text=detail.body_text
    return capture_detail(session,row,detail.content_html)


def test_new_notice_uses_daily_tables_folds_and_numbered_area_rules(session):
    row=fresh(session,'new');data=real_inventory(session,row)
    unrelated=fresh(session,'unrelated',region='光明区',kind='baozu')
    snap=build_snapshot(session,'instant',now=NOW,notices=[row])
    part,=render_report_parts(snap)
    soup=BeautifulSoup(part.wire_html,'html.parser')
    assert part.batch_codes==(f'B{row.id}',) and unrelated.id not in part.notice_ids
    assert '本次涉及1批｜新增1条｜更新0条' in part.content
    assert '本次未涉及已确认保租房' in part.content and '市级配租' not in part.content
    assert '在途' not in part.content and '全市去重' not in part.content
    fold=next(d for d in soup.select('details') if '房源明细与官方费用' in d.summary.get_text())
    assert not fold.has_attr('open') and len(fold.select('table'))>=14
    assert data['parsed_total']==289 and '本批已核实初始房源289套' in soup.get_text()
    assert '1. 一人及以上家庭' in part.content and '3. 三人及以上家庭' in part.content
    assert '2.25' in soup.get_text() and '22.5' in soup.get_text() and '特困' in soup.get_text()
    assert 'HOUSINGFOLD' not in part.wire_html
    assert len(part.wire_html.encode('utf-16-le'))//2<=19800


def test_announced_launch_has_complete_materials_but_is_not_claimed_open(session):
    row=fresh(session,'future',application_start=NOW+timedelta(days=2));real_inventory(session,row)
    part,=render_report_parts(build_snapshot(session,'instant',now=NOW,notices=[row]))
    assert '其中认租中0批' in part.content and '认租尚未开始' in part.content
    assert '房源明细与官方费用' in part.content and '289套' in part.content


def test_legacy_list_before_fold_never_leaks_internal_markers():
    text='- 现在要做：核对申请条件。\n**房源明细与官方费用**\n\n项目A，76套。\n\n**申请条件与办理规则**\n\n条件以原文为准。'
    html=compact_html(text)
    soup=BeautifulSoup(html,'html.parser')
    assert len(soup.select('details'))==1 and 'HOUSINGFOLD' not in html
    assert '76套' in soup.select_one('details').get_text()


def test_empty_instant_does_not_create_a_zero_value_report(session):
    fresh(session,'not_in_packet')
    assert render_report_parts(build_snapshot(session,'instant',now=NOW,notices=[]))==[]


def test_historical_snapshot_does_not_count_a_future_publication(session):
    fresh(session,'future-publication',published=NOW+timedelta(days=2),first_seen_at=NOW+timedelta(days=2),
        application_start=NOW+timedelta(days=2),application_end=NOW+timedelta(days=9))
    snap=build_snapshot(session,'digest',now=NOW)
    assert not snap.cards and snap.totals.in_transit_batches==0


def test_large_event_set_splits_at_whole_batches_without_dropping_members(session):
    rows=[fresh(session,'batch-'+str(i),region='光明区') for i in range(8)]
    snap=build_snapshot(session,'instant',now=NOW,notices=rows)
    parts=render_report_parts(snap,max_chars=2000,max_bytes=6000)
    assert len(parts)>1
    assert {n for p in parts for n in p.notice_ids}=={n.id for n in rows}
    assert len([code for p in parts for code in p.batch_codes])==len(rows)
    assert all(p.wire_html and 'HOUSINGFOLD' not in p.wire_html and len(p.content)<=2000 for p in parts)
    assert all('本部分统计仅含对应批次' in p.content for p in parts)


def test_multigroup_instant_freezes_html_and_retries_only_failed_original_packet(session,monkeypatch,tmp_path):
    from app.housing.delivery import HousingPushClient,push_pending
    from app.housing.groups import SubscribeGroup
    from app.housing.outbox import flush_outbox
    groups=[SubscribeGroup(name=x,id=x,topic=x,tier='pro') for x in ('A','B','C')]
    for g in groups:session.add(HousingGroupState(group_id=g.id,name=g.name,topic=g.topic,enabled=True,
        activated_at=NOW-timedelta(days=2),eligible_since=NOW-timedelta(days=2)))
    row=fresh(session,'first');real_inventory(session,row);session.commit()
    clock=[NOW];requests=[];failed_once=[False]
    def handler(request):
        payload=json.loads(request.content);requests.append(payload)
        if payload['topic']=='B' and not failed_once[0]:
            failed_once[0]=True;return httpx.Response(503,json={'code':500,'msg':'temporary'})
        return httpx.Response(200,json={'code':200,'data':'mock-accepted'})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler));sender=HousingPushClient(client)
    sender.settings=SimpleNamespace(pushplus_token='test-only')
    monkeypatch.setattr('app.housing.groups.load_groups',lambda:groups)
    monkeypatch.setattr('app.housing.delivery.HousingPushClient',lambda:sender)
    monkeypatch.setattr('app.housing.delivery.utcnow',lambda:clock[0])
    monkeypatch.setattr('app.housing.outbox.utcnow',lambda:clock[0])
    monkeypatch.setattr('app.housing.outbox.get_settings',lambda:SimpleNamespace(data_dir=tmp_path))
    from unittest.mock import AsyncMock
    monkeypatch.setattr('app.housing.outbox._account_rate_limit',AsyncMock())
    first=asyncio.run(push_pending(session));assert first['queued']==3
    assert len(requests)==3 and all(p['template']=='html' for p in requests)
    assert len({p['content'] for p in requests})==1
    old_html=requests[1]['content']
    failed=session.scalar(select(HousingOutbox).where(HousingOutbox.group_id=='B',HousingOutbox.status=='failed'))
    assert failed and session.get(HousingMeta,f'outbox-render:{failed.id}')
    clock[0]=NOW+timedelta(minutes=2)
    newer=fresh(session,'second',published=clock[0],event_at=clock[0],first_seen_at=clock[0]);session.commit()
    second=asyncio.run(push_pending(session));assert second['queued']==3 and len(requests)==6
    assert all(f'｜B{row.id}】' not in p['content'] for p in requests[3:])
    assert all(f'｜B{newer.id}】' in p['content'] for p in requests[3:])
    clock[0]=NOW+timedelta(minutes=6)
    retried=asyncio.run(flush_outbox(session,client=sender,now=clock[0],rate_limit=False,
        lock_path=tmp_path/'send.lock',job_ids=[failed.id]))
    assert retried['results'][0]['status']=='submitted' and len(requests)==7
    assert requests[-1]['topic']=='B' and requests[-1]['content']==old_html
    assert not session.scalar(select(HousingMeta).where(HousingMeta.key.like('report:digest:%')))
    asyncio.run(client.aclose())
