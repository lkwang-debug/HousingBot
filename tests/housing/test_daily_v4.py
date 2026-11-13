import asyncio
import copy
import hashlib
import json
from datetime import datetime,timedelta
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.housing.models import HousingNotice,HousingMeta,HousingOutbox,HousingOutboxEvent,HousingGroupState,HousingAttachmentState
from app.housing.reporting import build_snapshot
from app.housing.report_render import render_report_parts
from app.housing.daily_delta import validate_totals,accepted_baseline
from app.housing.daily_lifecycle import area_clauses,action_windows
from app.housing.scheduled_reports import prepare_report
from tests.housing.test_reports import notice,link

NOW=datetime(2026,10,25,8,30)


def launch(session,key,**kw):
    data=dict(published=NOW-timedelta(days=2),application_start=NOW-timedelta(days=3),application_end=NOW+timedelta(days=3))
    data.update(kw);return notice(session,key,**data)


def test_three_equals_two_public_and_one_mixed_and_all_distributions_balance(session):
    launch(session,'public1',kind='gongzu',region='盐田区')
    launch(session,'public2',kind='gongzu',region='龙华区')
    launch(session,'mixed',audience='enterprise',region='龙华区',housing_types=['baozu','talent'],title='龙华区住房和建设局关于面向重点企业配租保租房与人才住房的通告')
    snap=build_snapshot(session,'digest',now=NOW)
    assert snap.totals.open_batches==3 and snap.housing_totals['gongzu'].open_batches==2
    assert snap.housing_totals['mixed'].open_batches==1 and validate_totals(snap)
    part,=render_report_parts(snap)
    assert '公租房认租2批' in part.content and '混合房源认租1批' in part.content
    assert '在途' not in part.content and '跟进3批' in part.content


@pytest.mark.parametrize('kind,body,publication',[
    ('南山区','正式选房时间为2026年8月17日至8月20日。',datetime(2026,8,12)),
    ('龙华区','选房时间为2026年6月23日，具体时段为8:30-12:00、13:00-19:00。',datetime(2026,6,22)),
    ('坪山区','选房时间：2026年9月8日至9月9日。',datetime(2026,9,2)),
])
def test_past_handling_period_exits_all_daily_counts_but_preserves_rows(session,kind,body,publication):
    root=launch(session,'old',region=kind,published=publication-timedelta(days=20),application_end=publication-timedelta(days=15))
    node=launch(session,'node',region=kind,published=publication,category='process',title=kind+'住房和建设局关于选房签约事项的通告',body_text=body,application_start=None,application_end=None)
    link(session,root,node)
    snap=build_snapshot(session,'digest',now=NOW)
    assert not snap.cards and snap.totals.in_transit_batches==0
    assert any(c['code']==f'B{root.id}' and c['status']=='period_elapsed' for c in snap.archived_cards)
    assert session.get(HousingNotice,root.id) is root and session.get(HousingNotice,node.id) is node
    assert f'B{root.id}' not in render_report_parts(snap)[0].content


@pytest.mark.parametrize('days,expected',[(29,'active'),(30,'review_needed'),(59,'review_needed'),(60,'history_unverified'),(90,'history_unverified')])
def test_waiting_30_60_day_boundaries(session,days,expected):
    root=launch(session,'waiting',published=NOW-timedelta(days=days+10),application_end=NOW-timedelta(days=days))
    snap=build_snapshot(session,'digest',now=NOW)
    if days<60:assert snap.cards[0].daily_status==expected
    else:assert not snap.cards and snap.archived_cards[0]['status']==expected


def test_old_notice_with_future_window_survives_and_partial_result_does_not_close_it(session):
    root=launch(session,'future-root',published=datetime(2026,7,1),application_end=datetime(2026,7,20))
    node=launch(session,'future-node',published=datetime(2026,9,1),category='process',title='光明区住房和建设局关于选房安排的通告',body_text='选房时间：2026年10月20日至11月5日。',application_end=None)
    result=launch(session,'partial',published=NOW-timedelta(days=1),category='process',title='光明区住房和建设局关于第一队列当日选房结果公示',body_text='现公布当日结果，不代表整批已结束。',application_end=None)
    link(session,root,node);link(session,root,result)
    snap=build_snapshot(session,'digest',now=NOW)
    assert snap.cards[0].daily_status=='active' and snap.cards[0].stage!='closed'
    part,=render_report_parts(snap)
    assert '2026-11-05' in part.content and '办理时间见原文（未核实）' not in part.content


def test_missing_old_selection_dates_go_to_history_not_forever_pending(session):
    root=launch(session,'root',published=datetime(2026,6,1),application_end=datetime(2026,6,15))
    node=launch(session,'node',published=datetime(2026,8,1),category='process',title='光明区住房和建设局关于选房通知',application_end=None)
    link(session,root,node)
    snap=build_snapshot(session,'evening',now=NOW)
    assert not snap.cards and snap.archived_cards[0]['status']=='history_unverified'


def test_true_new_handling_notice_reopens_history_but_old_parser_revision_does_not(session):
    root=launch(session,'root',published=datetime(2026,6,1),application_end=datetime(2026,6,15))
    assert not build_snapshot(session,'digest',now=NOW).cards
    from app.housing.events import record_revision
    record_revision(session,root,'old',at=NOW-timedelta(hours=1),reason='official-attachment-changed')
    assert not build_snapshot(session,'digest',now=NOW).cards
    node=launch(session,'new',published=NOW-timedelta(minutes=30),category='process',title='光明区住房和建设局关于选房签约安排的通告',body_text='选房时间：2026年10月26日至10月28日。',application_end=None)
    link(session,root,node)
    snap=build_snapshot(session,'digest',now=NOW)
    assert snap.cards[0].daily_status=='active' and snap.cards[0].changed


def test_same_announcement_is_not_new_again_and_metadata_does_not_become_update(session):
    root=launch(session,'root',published=NOW-timedelta(hours=1))
    baseline=prepare_report(session,'digest',NOW)
    baseline['key']='digest:2026-10-25'
    from app.housing.events import record_revision
    root.business_version='new-byte-hash'
    record_revision(session,root,'v1',at=NOW+timedelta(hours=1),reason='official-attachment-changed')
    evening=build_snapshot(session,'evening',now=NOW+timedelta(hours=12),baseline=baseline)
    assert evening.totals.new_notices==0 and evening.totals.updated_notices==0
    part,=render_report_parts(evening)
    assert '认租机会简表' in part.content and '房源明细与官方费用' not in part.content


def test_afternoon_new_node_is_reported_once_against_morning_baseline(session):
    root=launch(session,'root')
    baseline=prepare_report(session,'digest',NOW);baseline['key']='digest:2026-10-25'
    node=launch(session,'afternoon',published=NOW+timedelta(hours=4),category='process',title='光明区住房和建设局关于资格核查结果的公示',body_text='公示期为2026年10月25日至10月30日。',application_end=None)
    link(session,root,node)
    snap=build_snapshot(session,'evening',now=NOW+timedelta(hours=12),baseline=baseline)
    assert snap.totals.new_notices==1 and any(r.id==node.id and r.is_new for r in snap.cards[0].references)
    second=prepare_report(session,'evening',NOW+timedelta(hours=12));second['audit']=snap.audit_dict();second['key']='evening:2026-10-25'
    second['audit']=json.loads(json.dumps(second['audit'],default=lambda d:d.isoformat()))
    later=build_snapshot(session,'digest',now=NOW+timedelta(days=1),baseline=second)
    assert later.totals.new_notices==0


def test_same_day_date_only_notice_found_after_morning_is_not_lost(session):
    launch(session,'known',published=NOW-timedelta(days=1))
    baseline=prepare_report(session,'digest',NOW);baseline['key']='digest:2026-10-25'
    new=launch(session,'new-at-eleven',published=NOW.replace(hour=0,minute=0),first_seen_at=NOW+timedelta(hours=2),
               application_start=NOW+timedelta(hours=2),application_end=NOW+timedelta(days=10))
    snap=build_snapshot(session,'digest',now=NOW+timedelta(hours=3),baseline=baseline)
    card=next(c for c in snap.cards if c.batch_id==new.id)
    assert card.changed and snap.totals.new_notices==1
    # A months-old backfill must not masquerade as a new publication.
    old=launch(session,'old-backfill',published=NOW-timedelta(days=90),first_seen_at=NOW+timedelta(hours=2))
    snap=build_snapshot(session,'digest',now=NOW+timedelta(hours=3),baseline=baseline)
    assert not next(c for c in snap.cards if c.batch_id==old.id).changed


def test_clause_splitting_keeps_decimals_negation_notes_and_original_numbering():
    text='说明。（一）一人家庭可申请40㎡，租金1.5元。（二）二至三人可申请50㎡，不限户籍或特殊项目除外。注：自愿租较小面积不补偿。'
    clauses=area_clauses(text)
    assert len(clauses)==4 and '1.5元' in clauses[1]
    assert '不限户籍或特殊项目除外' in clauses[2] and clauses[-1].startswith('注：')
    assert area_clauses('日期2026.10.25，租金1.5元，具体见原文。')==['日期2026.10.25，租金1.5元，具体见原文。']
    plain='标准如下：一人家庭可认租单身公寓；两人家庭可认租52㎡以下房源；三人家庭可认租70㎡以下房源；四人及以上家庭可认租全部房源，但特殊项目除外。注：不作其他补偿。'
    split=area_clauses(plain)
    assert len(split)==6 and split[1].startswith('一人家庭') and split[4].endswith('但特殊项目除外。')


def test_tables_escape_source_text_and_zero_is_not_missing(session):
    root=launch(session,'table',published=NOW-timedelta(hours=1))
    snap=build_snapshot(session,'digest',now=NOW);card=snap.cards[0]
    card.inventory={'complete':True,'projects':[{'name':'项目<script>alert(1)</script>','address':'<img src=x>','rows':[
        {'layout':'一房','area':'50㎡','units':76,'fees':{},'notes':'无障碍房源有特殊要求'}],
        'fees':{'rent':'16.2元/㎡·月','maintenance':'0元/㎡·月'}}],
        'evidence':[{'heading':'三、配租面积标准','text':'（一）完整条件一。（二）不要求社保满5年，但特殊项目除外。'}],
        'sources':[{'url':root.canonical_url}],'rule':{},'issues':[]}
    part,=render_report_parts(snap)
    assert '<table' in part.wire_html and '<script>' not in part.wire_html and '&lt;script&gt;' in part.wire_html
    soup=BeautifulSoup(part.wire_html,'html.parser')
    assert any('专项维修费用' in r.get_text() and '0' in r.get_text() for r in soup.select('tr'))
    assert '待核实' in soup.get_text() and '不要求社保满5年，但特殊项目除外' in soup.get_text()


def test_partially_failed_attachments_do_not_advance_verified_version(session):
    from app.housing.ingestion import _finish,attachment_event
    root=launch(session,'partial')
    root.attachment_version='verified-version'
    state=HousingAttachmentState(notice_id=root.id,purpose='lottery',status='ok',content_hashes={'a':'hash-a','b':'hash-b'},failures=0)
    session.add(state);session.flush()
    _finish(state,'manifest',{'a':'hash-a'},['download failed'])
    attachment_event(session,root)
    assert state.content_hashes=={'a':'hash-a','b':'hash-b'} and root.attachment_version=='verified-version'


def test_semantic_attachment_fingerprint_ignores_metadata_then_records_real_change(session):
    from app.housing.ingestion import attachment_event
    root=launch(session,'semantic');root.attachment_version='legacy-byte-version'
    attachment_event(session,root,purpose='lottery',semantic='same-roster')
    assert root.attachment_version=='legacy-byte-version'
    attachment_event(session,root,purpose='lottery',semantic='same-roster')
    assert session.get(HousingMeta,f'notice-revisions:{root.id}') is None
    attachment_event(session,root,purpose='lottery',semantic='changed-order')
    assert root.attachment_version!='legacy-byte-version'
    assert session.get(HousingMeta,f'notice-revisions:{root.id}')


def test_date_only_handling_does_not_invent_clock_time(session):
    root=launch(session,'root',application_end=NOW-timedelta(days=5))
    node=launch(session,'review',published=NOW-timedelta(days=1),title='光明区住房和建设局关于核查结果的公示',category='process',body_text='公示期为2026年10月25日至10月28日。',application_end=None)
    link(session,root,node)
    part,=render_report_parts(build_snapshot(session,'digest',now=NOW))
    assert '具体时刻见原文' in part.content and '23:59' not in part.content


def test_baseline_is_last_accepted_formal_report_not_test_or_failed(session):
    from app.housing.groups import SubscribeGroup
    from app.housing.outbox import enqueue
    a=SubscribeGroup(name='A',id='A',topic='A',tier='pro')
    session.add(HousingGroupState(group_id='A',name='A',enabled=True,topic='A'))
    def add(key,kind,stamp,status):
        session.add(HousingMeta(key='report:'+key,value={'generated_at':stamp.isoformat(),'audit':{'cards':[]}}))
        job=enqueue(session,a,kind,'title','body',[(key,None)]);job.created_at=stamp;job.status=status;session.flush()
    add('evening:2026-10-24','evening',NOW-timedelta(hours=12),'submitted')
    add('digest:2026-10-25','digest',NOW-timedelta(hours=1),'failed')
    add('report-test:evening:123','test',NOW-timedelta(minutes=30),'submitted')
    assert accepted_baseline(session,NOW)['key']=='evening:2026-10-24'
    session.get(HousingGroupState,'A').enabled=False;session.flush()
    assert accepted_baseline(session,NOW) is None


def test_frozen_html_is_used_for_transport_and_survives_failure_retry(session,monkeypatch,tmp_path):
    from app.housing.delivery import HousingPushClient
    from app.housing.outbox import enqueue,flush_outbox
    from app.housing.frozen_wire import freeze_wire
    from app.housing.groups import SubscribeGroup
    group=SubscribeGroup(name='A',id='A',topic='A',tier='pro')
    monkeypatch.setattr('app.housing.groups.load_groups',lambda:[group])
    session.add(HousingGroupState(group_id='A',name='A',enabled=True,topic='A'))
    job=enqueue(session,group,'evening','晚报','纯文本',[('evening:2026-10-25',None)])
    send_time=NOW.replace(hour=20,minute=30);job.created_at=send_time
    html='<table><tr><th>套数</th></tr><tr><td>76</td></tr></table>'
    freeze_wire(session,job,html);session.commit()
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content));return httpx.Response(200,json={'code':200,'data':'mock-message'})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender=HousingPushClient(client);sender.settings=SimpleNamespace(pushplus_token='test-only')
    result=asyncio.run(flush_outbox(session,client=sender,now=send_time,rate_limit=False,lock_path=tmp_path/'send.lock',job_ids=[job.id]))
    assert result['results'][0]['status']=='submitted' and requests[0]['template']=='html'
    assert requests[0]['content']==html
    # Explicit retry simulates a known failure; the same frozen body is reused.
    job=session.get(HousingOutbox,job.id);job.status='failed';job.next_attempt_at=NOW-timedelta(minutes=1);session.commit()
    asyncio.run(flush_outbox(session,client=sender,now=send_time+timedelta(minutes=5),rate_limit=False,lock_path=tmp_path/'send.lock',job_ids=[job.id]))
    assert len(requests)==2 and requests[1]['content']==requests[0]['content']
    asyncio.run(client.aclose())


def test_actual_deadline_reservations_require_a_displayed_item(session):
    root=launch(session,'deadline',application_end=NOW+timedelta(days=3))
    report=prepare_report(session,'evening',NOW)
    assert '3天后认租截止' in report['parts'][0]['content']
    assert root.id in report['parts'][0]['deadline_notice_ids']
    assert any(e['notice_id']==root.id for e in report['deadline_events'])


def test_private_priority_rules_are_not_invented_when_competition_changes(session):
    from app.housing.daily_render import competition_tables,Document
    doc=Document();card=SimpleNamespace(lottery_breakdown=[
        {'label':'一房','category':'一房','households_verified':True,'households':10,'initial_units':5,'competition_ratio':'2.0','queue_complete':False,'published_at':'2026-10-24'},
        {'label':'两房','category':'两房','households_verified':True,'households':8,'initial_units':4,'competition_ratio':'2.0','queue_complete':False,'published_at':'2026-10-24'}])
    competition_tables(doc,card)
    html=''.join(doc.html)
    assert '<table' in html and 'width:100%' in html and '个人成功概率' in html
    assert '待核实' not in ''.join(doc.text[:3]) and '不填0' in ''.join(doc.text)
