import copy
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.housing.inventory import capture_detail, finalize, html_tables, inventory_text, merge_projects, parse_table, pdf_tables
from app.housing.lookup_data import attach_inventory, event_groups, parse_final_tables
from app.housing.models import HousingNotice
from app.housing.opportunity import analyze, queue_reference
from app.housing.report_inventory import additions
from tools.build_lottery_lookup import _render_html

FIXTURES = Path(__file__).parent / "fixtures/inventory"


def test_nanshan_2026_second_batch_has_seven_projects_289_units_and_two_rent_policies(session):
    from app.housing.parsers.detail import parse_detail
    html=(FIXTURES/'nanshan-2026-037.html').read_text(encoding='utf-8')
    detail=parse_detail(html,'https://www.szns.gov.cn/bsfw/ztfw/zflyfwzt/xmfpjtcgg/content/post_13039393.html')
    n=HousingNotice(canonical_key='nanshan-037',url_hash='nanshan-037',canonical_url='https://example.gov.cn/nanshan',
        title=detail.title,region_code='南山区',category='rent_launch',body_text=detail.body_text,
        business_version='fixture-v1',published_at=datetime(2026,10,26))
    session.add(n);session.flush()
    value=capture_detail(session,n,detail.content_html)
    assert value['complete'] and value['declared_projects']==len(value['projects'])==7
    assert value['declared_total']==value['parsed_total']==289
    assert {p['name']:p['total'] for p in value['projects']}=={'冠铭花园':104,'众悦家园':60,'长源京基御领公馆':28,'山海逸居':40,'硅谷公寓':37,'龙海家园':7,'龙瑞佳园':13}
    project=next(p for p in value['projects'] if p['name']=='冠铭花园')
    options=project['fees']['rent']['options']
    assert [o['value'] for o in options]==['2.25元/㎡·月','22.5元/㎡·月']
    assert '特困' in options[0]['label'] and '352' in options[1]['label']
    assert any('面积标准' in e['heading'] and '42㎡' in e['text'] and '70㎡' in e['text'] for e in value['evidence'])


def notice(session, number=4):
    from app.housing.parsers.detail import parse_detail
    html = (FIXTURES / f"notice-{number}.html").read_text(encoding="utf-8")
    detail = parse_detail(html, "https://www.szgm.gov.cn/example.html")
    row = HousingNotice(canonical_key=f"test:{number}", url_hash=f"hash:{number}", canonical_url="https://example.gov.cn/notice",
        title="光明区2026年第一批次个人保租房（住保租〔2026〕038号）" if number==4 else f"测试配租{number}",
        region_code="光明区", category="rent_launch", body_text=detail.body_text, business_version="v1",
        batch_key_v2=f"batch:{number}", published_at=datetime(2026,9,30))
    session.add(row); session.flush()
    payload = capture_detail(session, row, detail.content_html)
    if number==4:
        for table in pdf_tables((FIXTURES / "guangming-2026-038-inventory.pdf").read_bytes()):
            merge_projects(payload, parse_table(table))
        finalize(payload)
    return row, payload


def bundle(session):
    _, inventory = notice(session)
    batch = {"id":1,"t":"光明2026第一批","d":"2026-10-16","Q":["第一队列","第二队列","第三队列"],
             "c":[{"key":"100+","l":"100㎡及以上","u":1227,"n":3810,"q":{"第一队列":1551,"第二队列":359,"第三队列":1900},"issue":""}],
             "u":"https://example.gov.cn/lottery","sources":["https://example.gov.cn/lottery"]}
    attach_inventory(batch, inventory)
    return {"district":"光明区","batches":[batch],"rows":{"TEST000001":[[0,0,0,2823,1152,0,0]]}}


def values(data, **extra):
    projects = data["batches"][0]["opportunity"]["projects"]
    selected=[p["id"] for p in projects if p["name"] in {"凤凰英荟城","明汇府","红坳新居"}]
    return {"apply_no":"TEST000001","projects":selected,"people":4,"children":2,"basis":"preliminary",**extra}


def test_official_inventory_reconciles_1963_rows_and_keeps_fees(session):
    _, data = notice(session)
    assert data["complete"] and data["parsed_total"]==data["declared_total"]==1963
    assert len(data["projects"])==8
    p=next(p for p in data["projects"] if p["name"]=="虹霓轩")
    assert p["rows"][0]["layout"]=="二房二厅" and p["rows"][0]["band"]=="90"
    p=next(p for p in data["projects"] if p["name"]=="明皓府")
    assert p["fees"]["rent"]=="22.8元/㎡·月" and p["fees"]["market"]=="38元/㎡·月"
    assert sum(r["units"] for r in p["rows"])==677
    assert data["rule"]["verified"] and any("未成年" in e["text"] for e in data["evidence"])


@pytest.mark.parametrize("number,total",[(2249,894),(2270,311),(2379,612)])
def test_real_html_and_prose_inventory_reconciles_total_without_duplicate_projects(session, number,total):
    _, data=notice(session,number)
    assert data["complete"] and data["parsed_total"]==data["declared_total"]==total
    if number==2270:
        p=next(p for p in data["projects"] if p["name"]=="海安居一期")
        assert p["fees"]["maintenance"]=="0元/㎡·月"
        assert "停车：待核实" in inventory_text(data,"gongzu")
    if number==2249:
        p=next(p for p in data['projects'] if p['name']=='卓越和奕府')
        assert [o['value'] for o in p['fees']['rent']['options']]==['36元/㎡·月','18元/㎡·月']
        text=inventory_text(data,'gongzu')
        assert '不申请核对家庭收入财产状况' in text and '具有有效核对报告且符合条件' in text


def test_mismatched_total_disables_capacity_but_keeps_field_evidence(session):
    _,data=notice(session)
    data["projects"][0]["rows"][0]["units"]-=1
    finalize(data)
    assert not data["complete"] and data["issues"]
    assert data["projects"][0]["fees"]["market"]


def test_repeated_attachment_tables_never_accumulate_households(session):
    _,data=notice(session)
    original=data["parsed_total"]
    for table in pdf_tables((FIXTURES/'guangming-2026-038-inventory.pdf').read_bytes()):merge_projects(data,parse_table(table))
    assert finalize(data)["parsed_total"]==original


def test_mirrors_are_one_event_while_distinct_round_attachments_remain(session):
    first,_=notice(session)
    first.category="lottery_result";first.attachments=[{"url":"https://example.gov.cn/attachment/1/2/333/a.pdf"}]
    alias=HousingNotice(id=990,batch_key_v2=first.batch_key_v2,category=first.category,published_at=first.published_at,
                        attachments=[{"url":"https://example.gov.cn/gkmlpt/attachment/1/2/333/b.pdf"}])
    other=HousingNotice(id=991,batch_key_v2=first.batch_key_v2,category=first.category,published_at=first.published_at,
                        attachments=[{"url":"https://example.gov.cn/attachment/1/2/444/a.pdf"}])
    assert [[n.id for n in g] for g in event_groups([first,alias,other])]==[[first.id,990],[991]]


def test_final_rank_requires_explicit_column_not_serial_or_lottery_order():
    table=[["序号","认租编号","队内摇号结果"],[1,"TEST000001",7]]
    assert parse_final_tables([table])=={}
    table[0][2]="选房排位"
    assert parse_final_tables([table])=={"TEST000001":7}


def test_multi_round_rank_is_separate_and_every_record_survives():
    from app.housing.lookup_data import normalize_round_ranks
    p={'batches':[{'c':[{}]}],'rows':{'TEST000001':[[0,0,0,1,99,0,1],[0,0,0,2,99,0,2]],
                            'TEST000002':[[0,0,0,2,99,0,1],[0,0,0,1,99,0,2]]}}
    normalize_round_ranks(p)
    assert [r[4] for r in p['rows']['TEST000001']]==[1,2]
    assert len(p['rows']['TEST000001'])==2


def test_machine_lottery_rounds_with_unique_applicants_do_not_reset_queue_rank():
    from app.housing.lookup_data import normalize_round_ranks
    p={'batches':[{'c':[{}]}],'rows':{'TEST000001':[[0,0,0,547,99,0,1]],'TEST000002':[[0,0,0,2823,99,0,3]]}}
    normalize_round_ranks(p)
    assert p['rows']['TEST000002'][0][4]==2


def test_change_check_ignores_outside_window_but_detects_selected_update_and_new_event(session,monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from app.housing import lookup
    from app.housing.models import HousingAttachmentState, utcnow
    from app.housing.lookup_data import selected_events
    notices=[]
    def add(index, published):
        n=HousingNotice(canonical_key=f'event:{index}',url_hash=f'hash:{index}',canonical_url=f'https://example.gov.cn/{index}',
            title=f'测试摇号{index}',category='lottery_result',region_code='光明区',is_target=True,
            published_at=published,business_version='v1',batch_key_v2=f'event-batch:{index}')
        session.add(n);session.flush()
        session.add(HousingAttachmentState(notice_id=n.id,purpose='lottery',status='ok'))
        session.commit();return n
    now=utcnow()
    for i in range(7):notices.append(add(i,now-timedelta(days=i)))
    expected={str(group[0].id):lookup.source_version(session,group[0]) for group in selected_events(session,'光明区')}
    assert len(expected)==6 and str(notices[6].id) not in expected
    monkeypatch.setattr(lookup,'SessionLocal',lambda:nullcontext(session))
    monkeypatch.setattr(lookup,'read_manifest',lambda:{'pages':{'guangming.html':{'district':'光明区','notices':expected}}})
    calls=[]
    async def build(regions):calls.append(regions);return {'status':'built'}
    monkeypatch.setattr(lookup,'build_regions',build)
    def no_verified_cache():raise ValueError('fixture does not contain verified cached roster files')
    monkeypatch.setattr(lookup,'refresh_review_metadata',no_verified_cache)
    notices[6].business_version='outside-change';session.commit()
    assert asyncio.run(lookup.build_changed())=={'status':'unchanged'} and not calls
    notices[0].business_version='selected-change';session.commit()
    assert asyncio.run(lookup.build_changed())=={'status':'built'} and calls==[['光明区']]
    expected[str(notices[0].id)]=lookup.source_version(session,notices[0]);calls.clear()
    notices[0].business_version='review-dependency-change';session.commit()
    monkeypatch.setattr(lookup,'refresh_review_metadata',lambda:{'status':'built','reused_roster':True})
    assert asyncio.run(lookup.build_changed())=={'status':'built','reused_roster':True} and not calls
    expected[str(notices[0].id)]=lookup.source_version(session,notices[0])
    add(8,now+timedelta(minutes=1))
    assert asyncio.run(lookup.build_changed())=={'status':'built'} and calls==[['光明区']]


def test_one_failed_region_does_not_block_other_lookup_updates(session,monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from app.housing import lookup
    from app.housing.models import HousingAttachmentState,utcnow
    for region in ('光明区','福田区'):
        n=HousingNotice(canonical_key=region,url_hash=region,canonical_url='https://example.gov.cn/test',title='测试摇号',category='lottery_result',region_code=region,is_target=True,published_at=utcnow())
        session.add(n);session.flush();session.add(HousingAttachmentState(notice_id=n.id,purpose='lottery',status='ok'))
    session.commit()
    monkeypatch.setattr(lookup,'SessionLocal',lambda:nullcontext(session));monkeypatch.setattr(lookup,'read_manifest',lambda:{})
    calls=[]
    async def build(regions):
        calls.extend(regions)
        if regions==['光明区']:raise ValueError('broken historical attachment')
        return {'status':'built','regions':regions}
    monkeypatch.setattr(lookup,'build_regions',build)
    result=asyncio.run(lookup.build_changed())
    assert result['status']=='degraded' and result['updated_regions']==['福田区'] and result['failed_regions']==['光明区']
    assert set(calls)=={'光明区','福田区'}


def test_1152_rank_550_units_has_602_threshold_only_as_named_scenario(session):
    data=bundle(session);original=copy.deepcopy(data)
    result=analyze(data,data["rows"]["TEST000001"][0],values(data,not_consuming=602))
    assert result["facts"]["initial_units"]==550 and result["facts"]["rank"]==1152
    assert result["self_report"]["priority_candidate"]
    assert result["scenario"]=={"required_not_consuming":602,"remaining_before_self":1,"within_capacity":True}
    assert result["missing"] and result["assumptions"]["basis"]=="preliminary" and original==data
    no_basis=analyze(data,data["rows"]["TEST000001"][0],values(data,basis=""))
    assert no_basis["status"]=="pending" and no_basis["scenario"] is None


@pytest.mark.parametrize("patch",[{"people":None},{"people":2},{"children":None},{"children":4},{"not_consuming":1152},{"ahead":-1,"basis":"custom"},{"projects":["not-this-band"]}])
def test_unknown_family_does_not_infer_four_people_or_accept_wrong_band(session,patch):
    data=bundle(session)
    assert analyze(data,data["rows"]["TEST000001"][0],values(data,**patch))["status"]=="invalid"


def test_official_final_rank_is_not_given_priority_a_second_time(session):
    data=bundle(session)
    data["batches"][0]["final_rows"]={"TEST000001":{"rank":100,"ahead_same_band":99,"scope_verified":True,"url":"https://example.gov.cn/final"}}
    result=analyze(data,data["rows"]["TEST000001"][0],values(data,basis="official-final"))
    assert result["assumptions"]["ahead"]==99 and result["scenario"]["required_not_consuming"]==0
    assert result["facts"]["rank"]==1152 and result["facts"]["final_selection_rank"]==100


@pytest.mark.parametrize("problem", [{"issue":"重复顺序号尚未核实"}, {"rank_scope":"multiple-records"}])
def test_ambiguous_roster_keeps_facts_but_blocks_scenarios_in_both_engines(session,problem):
    data=bundle(session);match=data['rows']['TEST000001'][0]
    data['batches'][0]['c'][match[1]].update(problem)
    expected=analyze(data,match,values(data))
    assert expected['scenario'] is None and expected['status']=='pending' and expected['facts']['rank']==1152
    script="const fs=require('fs'),e=require(process.argv[1]),d=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(e.analyze(d.data,d.match,d.values)));"
    run=subprocess.run(['node','-e',script,str(Path('app/housing/opportunity.js').resolve())],input=json.dumps({'data':data,'match':match,'values':values(data)}),text=True,encoding='utf-8',capture_output=True,check=True)
    assert json.loads(run.stdout)==expected


def test_manual_queue_move_includes_self_and_removes_self_from_old_queue():
    data={'batches':[{'Q':['第一队列','第二队列','第三队列'],'c':[{'q':{'第一队列':1,'第二队列':2,'第三队列':3}}],
        'R':{'0|0':[2],'0|1':[5,8],'0|2':[1,6,9]}}]}
    original=copy.deepcopy(data);match=[0,0,1,5,1,1,1]
    third=queue_reference(data,match,'第三队列')
    assert (third['rank'],third['ahead'],third['households'])==(2,3,4)
    first=queue_reference(data,match,'第一队列')
    assert (first['rank'],first['ahead'],first['households'])==(2,1,2)
    recorded=queue_reference(data,match,'第二队列')
    assert (recorded['rank'],recorded['ahead'],recorded['households'])==(1,1,2)
    assert data==original
    script="const fs=require('fs'),e=require(process.argv[1]),d=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(d.targets.map(q=>e.queueReference(d.data,d.match,q))));"
    run=subprocess.run(['node','-e',script,str(Path('app/housing/opportunity.js').resolve())],input=json.dumps({'data':data,'match':match,'targets':['第三队列','第一队列','第二队列']}),text=True,encoding='utf-8',capture_output=True,check=True)
    assert json.loads(run.stdout)==[third,first,recorded]
    del data['batches'][0]['R']['0|2']
    assert queue_reference(data,match,'第三队列')['rank'] is None


def test_manual_queue_overrides_scenario_only_and_cannot_reuse_old_final_rank(session):
    data=bundle(session);match=data['rows']['TEST000001'][0]
    data['batches'][0]['R']={'0|2':list(range(3000,4900))}
    original=copy.deepcopy(data)
    result=analyze(data,match,values(data,queue='第三队列'))
    assert result['facts']['queue']=='第一队列' and result['facts']['rank']==1152
    assert result['self_report']['queue']=='第三队列' and result['assumptions']['queue_rank_reference']==1
    assert result['assumptions']['ahead']==1551+359-1 and data==original
    result=analyze(data,match,values(data,queue='第三队列',basis='official-final'))
    assert result['scenario'] is None and any('最新正式排序' in s for s in result['missing'])


def test_python_browser_parity_for_valid_invalid_pending_and_final_cases(session):
    data=bundle(session);match=data["rows"]["TEST000001"][0]
    inputs=[values(data),values(data,basis=""),values(data,people=None),values(data,people=3),
            values(data,basis="custom",ahead=800,not_consuming=251),values(data,not_consuming=-1),
            values(data,queue='第三队列'),values(data,queue='不存在的队列'),values(data,queue='第三队列',basis='custom',ahead=800)]
    js=Path('app/housing/opportunity.js').resolve()
    script="const fs=require('fs'),e=require(process.argv[1]);const d=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(d.inputs.map(x=>e.analyze(d.data,d.match,x))));"
    run=subprocess.run(['node','-e',script,str(js)],input=json.dumps({'data':data,'match':match,'inputs':inputs}),text=True,encoding='utf-8',capture_output=True,check=True)
    assert json.loads(run.stdout)==[analyze(data,match,v) for v in inputs]


def test_ui_exact_number_mapping_edit_reset_unknown_and_missing_units():
    data={"district":"光明区","batches":[{"id":1,"t":"测试批次","d":"2026-10-16","Q":["第二队列","第三队列"],
        "c":[{"l":"50㎡左右（一房类）","n":1525,"u":0,"q":{"第二队列":259}},
             {"l":"40㎡左右（单身公寓类）","n":3744,"u":111,"q":{"第三队列":2673}}]}],
        "rows":{"SQ2026BZ360002688":[[0,0,0,547,94,0,1]],"SQ2026BZ360007741":[[0,1,1,1,1,1,1]]}}
    html=_render_html(data);script=re.search(r'<script>\s*(const DATA = .*?)</script>',html,re.S)[1]
    harness='''const vm=require('vm'),fs=require('fs');const nodes={};const document={getElementById(id){return nodes[id]||={value:'',innerHTML:'',style:{},events:{},addEventListener(k,f){this.events[k]=f;}};}};
const c=vm.createContext({document});vm.runInContext(fs.readFileSync(0,'utf8'),c);
nodes.q.value='SQ2026BZ360002688';vm.runInContext('run()',c);
if(!nodes.out.innerHTML.includes('请先选择本人队列')||nodes.out.innerHTML.includes('50㎡左右'))throw Error('queue required');
nodes['q-queue'].value='第二队列';vm.runInContext('run()',c);
if(!nodes.out.innerHTML.includes('50㎡左右')||nodes.out.innerHTML.includes('40㎡左右')||!nodes.out.innerHTML.includes('第 94 位'))throw Error('wrong 2688');
nodes.q.value='SQ2026BZ360007741';nodes.q.events.input();if(nodes.out.innerHTML!==''||nodes.out.style.display!=='none')throw Error('stale result');
nodes['q-queue'].value='第三队列';
vm.runInContext('run()',c);if(!nodes.out.innerHTML.includes('40㎡左右')||nodes.out.innerHTML.includes('50㎡左右'))throw Error('wrong 7741');
nodes['q-queue'].events.change();if(nodes.out.innerHTML!=='')throw Error('stale queue');
nodes.q.value='DOESNOTEXIST';vm.runInContext('run()',c);if(nodes.out.innerHTML.includes('40㎡左右')||!nodes.out.innerHTML.includes('查不到不代表不合格'))throw Error('stale missing');'''
    run=subprocess.run(['node','-e',harness],input=script,text=True,encoding='utf-8',capture_output=True)
    assert run.returncode==0,run.stderr
    assert '例如 SQ2026' not in html and '<summary>分析我的上车机会</summary>' in html
    ui=Path('app/housing/lookup_ui.js').read_text(encoding='utf-8')
    for removed in ['本人申报的具体户型','本人意向项目','公证名单当时的队列（非最终）','演算依据','自填前序户数','假设其中不消耗']:
        assert removed not in ui


def test_inventory_daily_stage_gate_does_not_hide_lottery_counts(session):
    from types import SimpleNamespace
    _,data=notice(session)
    card=SimpleNamespace(is_open=True,stage='applying',inventory=data,profile=SimpleNamespace(housing_type='baozu'),
        lottery_breakdown=[],in_transit=True,batch_id=1,references=[])
    snap=SimpleNamespace(kind='digest')
    assert '明皓府' in additions(card,snap)
    card.stage='lottery';card.lottery_breakdown=[{'label':'50㎡左右','category':'50','households':1525,
        'queues':{'第一队列':186,'第二队列':259,'第三队列':1080},'complete':True,'published_at':'2026-10-16','url':'source'}]
    text=additions(card,snap)
    assert '明皓府' not in text and '1525户' in text and '第一队186户' in text
    card.stage='waiting';card.is_open=False
    assert '明皓府' not in additions(card,snap)
