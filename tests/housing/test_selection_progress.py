import asyncio
import copy
import json
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.housing.models import HousingMeta,HousingNotice
from app.housing.selection_progress import (parse_tables,read_tables,catalog_rows,prepare_import,register,
    public_progress,progress_net,source_candidates,collect_notice,family_tables,digest)

HEAD=['项目名称','建筑面积（平方米）','户型','本轮供应','已选套数','剩余可选套数']
CAT=[{'project':'公寓A','area':'40','layout':'一房一厅','initial_supply':4,'band':'40','pool':''},
     {'project':'公寓A','area':'50','layout':'一房一厅','initial_supply':6,'band':'50','pool':''},
     {'project':'小区B','area':'125','layout':'三房二厅','initial_supply':3,'band':'100+','pool':''},
     {'project':'小区B','area':'125','layout':'四房二厅','initial_supply':2,'band':'100+','pool':''}]
TABLE=[HEAD,['公寓A','约40','一房一厅',4,1,3],['公寓A',50,'一房一厅',6,0,6],
       ['小区B',125,'三房二厅',3,2,1],['小区B',125,'四房二厅',2,1,1]]


def snap(selected=4,as_of='2026-11-05T17:55:00',**kw):
    rows=parse_tables([TABLE],catalog=CAT,complete=True)
    rows[0].update(selected=selected-3,remaining=7-selected)
    s={'schema':1,'batch_key':'test','notice_id':1,'round':'首轮','scope':'整批','source':'https://example.gov.cn/result',
       'as_of':as_of,'complete':True,'rows':rows,'supply_version':'inventory','families':[]}
    s.update(kw);s['version']=digest(s);return s


def test_separate_actual_area_and_equal_area_different_layout_and_mirrors():
    result=parse_tables([TABLE,TABLE],catalog=CAT,complete=True)
    assert len(result)==4
    assert [(r['area'],r['layout'],r['selected']) for r in result]==[
        ('40','一房一厅',1),('50','一房一厅',0),('125','三房二厅',2),('125','四房二厅',1)]
    assert result[0]['initial_supply']==4


def test_room_dedup_cross_project_and_building_preserves_states_and_dates():
    t=[['项目','实际面积','户型','楼栋','房号','状态','选定日期'],
       ['A',50,'一房一厅','1栋','101','已选定','2026-11-05'],
       ['A',50,'一房一厅','1栋','101','已选定','2026-11-05'],
       ['A',50,'一房一厅','2栋','101','已签约','2026-11-04'],
       ['A',50,'一房一厅','1栋','102','可选',''],
       ['A',50,'一房一厅','1栋','103','不可选',''],
       ['B',50,'一房一厅','1栋','101','撤回','']]
    rows=parse_tables([t],complete=True)
    assert rows[0]['supply']==4 and rows[0]['selected']==2 and rows[0]['signed']==1
    assert rows[0]['remaining']==1 and rows[0]['unavailable']==1 and rows[0]['selected_by_day']['2026-11-05']==1
    assert rows[1]['withdrawn']==1 and rows[1]['selected']==0
    assert all('room' not in r for r in rows)


def test_partial_room_results_do_not_claim_remaining_or_supply():
    r=parse_tables([[['项目','实际面积','户型','房源编号','房源状态'],['A',50,'一房一厅','1','已选']]])[0]
    assert r['supply'] is None and r['remaining'] is None and r['selected']==1


@pytest.mark.parametrize('tail',[
    ['A',50,'一房一厅','1','可选'],['A',50,'二房一厅','1','已选']])
def test_conflicting_same_room_is_rejected(tail):
    with pytest.raises(ValueError,match='冲突'):
        parse_tables([[['项目','实际面积','户型','房源编号','状态'],['A',50,'一房一厅','1','已选'],tail]])


def test_no_proportional_allocation_of_project_totals():
    rows=parse_tables([[['项目','已选套数','剩余套数'],['公寓A',2,8]]],catalog=CAT)
    assert len(rows)==1 and rows[0]['level']=='project' and not rows[0]['area'] and rows[0]['supply'] is None
    with pytest.raises(ValueError,match='重叠'):
        parse_tables([TABLE,[['项目','已选套数'],['公寓A',1]]],catalog=CAT)


def test_unknown_zero_interval_and_missing_type_stay_distinct():
    rows=parse_tables([[HEAD,['A','40-45','一房一厅',8,0,'未公开']]])
    assert rows[0]['selected']==0 and rows[0]['remaining'] is None and rows[0]['area']=='40-45'
    with pytest.raises(ValueError,match='整份'):
        parse_tables([[HEAD,['A','40㎡档','一房一厅',8,1,7]]])


def test_family_rate_and_explicit_abandonment_not_inferred():
    t=[['安排日期','场次','安排家庭数','选定家庭数'],['2026-11-05','下午',240,212]]
    r=family_tables([t,t])[0]
    assert r['rate']=='88.3' and r['abandoned'] is None
    assert family_tables([[['安排日期','场次','安排单位数','选定套数'],['2026-11-05','下午',2,20]]])==[]


@pytest.mark.parametrize('suffix,data',[
    ('csv',('项目,实际面积,户型,本轮供应,已选,剩余\nA,50,一房一厅,6,0,6').encode('utf-8-sig')),
    ('html','<table><tr><th>项目</th><th>实际面积</th><th>户型</th><th>本轮供应</th><th>已选</th><th>剩余</th></tr><tr><td>A</td><td>50</td><td>一房一厅</td><td>6</td><td>0</td><td>6</td></tr></table>'.encode())])
def test_file_formats_keep_verified_zero(suffix,data):
    assert parse_tables(read_tables(data,suffix))[0]['remaining']==6


def test_excel_sheets_and_pdf_continuation(tmp_path,monkeypatch):
    import openpyxl,pdfplumber
    p=tmp_path/'official.xlsx';w=openpyxl.Workbook();s=w.active
    for row in TABLE:s.append(row)
    w.save(p);w.close();assert len(parse_tables(read_tables(p.read_bytes(),'xlsx')))==4
    class PDF:
        pages=[SimpleNamespace(extract_tables=lambda:[TABLE[:3]]),SimpleNamespace(extract_tables=lambda:[TABLE[3:]])]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda _:PDF())
    assert len(parse_tables(read_tables(b'pdf','pdf')))==4


def test_immutable_idempotent_snapshot_and_net_decrease_not_daily(session,tmp_path):
    a=snap(5);register(session,a,private_root=tmp_path)
    assert register(session,a,private_root=tmp_path)['status']=='existing'
    b=snap(4,'2026-11-06T09:00:00');register(session,b,private_root=tmp_path)
    p=public_progress(session,'test',at=datetime(2099,1,1))
    assert len(p['scopes'])==1 and p['scopes'][0]['totals']['selected']==4
    assert p['scopes'][0]['net_change']['selected']==-1 and p['scopes'][0]['daily_selected'] is None
    assert '累计已选减少' in p['scopes'][0]['correction_note']
    assert len(session.get(HousingMeta,'selection-progress-index:test').value['keys'])==2


def test_partial_cannot_replace_last_verified_and_unknown_time_not_newest(session,tmp_path):
    register(session,snap(5),private_root=tmp_path)
    register(session,snap(2,'2026-11-06T09:00:00',complete=False),private_root=tmp_path)
    register(session,snap(1,None,complete=False),private_root=tmp_path)
    s=public_progress(session,'test',at=datetime(2099,1,1))['scopes'][0]
    assert s['totals']['selected']==5 and s['latest_incomplete']


def test_shared_pool_and_overlapping_scopes_no_false_sum(session,tmp_path):
    s=snap();s['rows'][0]['pool']='shared';s['version']=digest(s)
    register(session,s,private_root=tmp_path)
    p=public_progress(session,'test',at=datetime(2099,1,1));assert p['scopes'][0]['totals'] is None


def test_shared_pool_is_displayed_once_and_mixed_rounds_are_rejected():
    t=[[*HEAD,'共享池编号'],['A',40,'一房一厅',10,2,8,'pool'],['A',50,'一房一厅',10,2,8,'pool']]
    rows=parse_tables([t],complete=True)
    assert len(rows)==1 and rows[0]['level']=='pool' and rows[0]['selected']==2
    assert len(rows[0]['pool_members'])==2
    with pytest.raises(ValueError,match='共享池'):
        parse_tables([[t[0],t[1],['A',50,'一房一厅',10,3,7,'pool']]])
    with pytest.raises(ValueError,match='多个轮次'):
        parse_tables([[[*HEAD,'轮次'],['A',40,'一房一厅',4,1,3,'首轮'],['A',50,'一房一厅',6,0,6,'第二轮']]])


def test_daily_actual_requires_complete_individual_selection_dates(session,tmp_path):
    t=[['项目','实际面积','户型','房源编号','状态','选定日期'],['A',50,'一房一厅','1','已选','2026-11-05'],
       ['A',50,'一房一厅','2','已签约','2026-11-04'],['A',50,'一房一厅','3','可选','']]
    s=snap(rows=parse_tables([t],complete=True));register(session,s,private_root=tmp_path)
    p=public_progress(session,'test',at=datetime(2099,1,1))['scopes'][0]
    assert p['daily_selected']=={'day':'2026-11-05','count':1}


def test_public_official_table_complete_inventory_and_update_time(session):
    from tests.housing.test_reports import notice
    from app.housing.inventory import save_inventory
    n=notice(session,'public-progress',category='process',body_text='选房时间为2026年11月4日至11月14日。选房结果查询：https://example.gov.cn/result')
    n.batch_key_v2='progress-public'
    root=notice(session,'public-launch');root.batch_key_v2=n.batch_key_v2
    save_inventory(session,root,{'projects':[{'name':'A','rows':[{'area':'50','layout':'一房一厅','units':6,'band':'50'}]}],
        'complete':True,'checked_at':'2026-11-01','notice_version':root.business_version,'batch_key':root.batch_key_v2})
    session.flush()
    text='<p>数据截至：2026-11-05 17:55</p><table><tr>'+''.join('<th>'+v+'</th>' for v in HEAD)+'</tr><tr><td>A</td><td>50</td><td>一房一厅</td><td>6</td><td>2</td><td>4</td></tr></table>'
    async def get(url):return SimpleNamespace(url=url,status=200,text=text,content=text.encode())
    asyncio.run(collect_notice(session,SimpleNamespace(get=get),n,now=datetime(2026,11,6,10)))
    p=public_progress(session,n.batch_key_v2,at=datetime(2099,1,1))['scopes'][0]
    assert p['as_of']=='2026-11-05T17:55:00' and p['totals']['remaining']==4


def test_supply_cannot_be_overcounted_and_signed_is_not_added_to_selected():
    with pytest.raises(ValueError,match='超过本轮供应'):
        parse_tables([[HEAD,['A',50,'一房一厅',6,4,4]]])
    r=parse_tables([[[*HEAD,'已签约套数'],['A',50,'一房一厅',6,4,2,3]]])[0]
    assert r['selected']==4 and r['signed']==3 and r['remaining']==2


def test_formal_baseline_comparison_and_future_selection_note(session,tmp_path):
    old=snap(4);register(session,old,private_root=tmp_path)
    before=public_progress(session,'test',at=datetime(2099,1,1))
    register(session,snap(5,'2026-11-06T10:00:00'),private_root=tmp_path)
    after=public_progress(session,'test',at=datetime(2099,1,1));progress_net(after,before)
    assert after['scopes'][0]['report_net']==1
    progress_net(after,None);assert after['scopes'][0]['report_net'] is None


def test_import_matches_all_initial_types_and_does_not_fill_missing(session,tmp_path):
    from tests.housing.test_reports import notice
    from app.housing.inventory import save_inventory
    from app.housing.batches import batch_key
    n=notice(session,'progress-launch',title='光明区住房和建设局关于面向个人配租保租房的通告')
    assert batch_key(n)
    n.batch_key_v2=batch_key(n);session.flush()
    save_inventory(session,n,{'projects':[{'name':'公寓A','rows':[{'area':'40','layout':'一房一厅','units':4,'band':'40'},
        {'area':'50','layout':'一房一厅','units':6,'band':'50'}]}],'complete':True,'declared_total':10,'checked_at':'2026-11-01',
        'notice_version':n.business_version,'batch_key':batch_key(n)})
    p=tmp_path/'source.csv';p.write_text('项目,实际面积,户型,本轮供应,已选,剩余\n公寓A,40,一房一厅,4,1,3\n公寓A,50,一房一厅,6,0,6',encoding='utf8')
    result,_=prepare_import(session,n,p,'https://example.gov.cn/source',complete=True)
    assert len(result['rows'])==2 and result['as_of'] is None
    p.write_text('项目,实际面积,户型,本轮供应,已选,剩余\n公寓A,40,一房一厅,4,1,3',encoding='utf8')
    with pytest.raises(ValueError,match='全部实际户型'):prepare_import(session,n,p,'https://example.gov.cn/source',complete=True)
    with pytest.raises(ValueError,match='官方政府'):prepare_import(session,n,p,'https://mp.weixin.qq.com/source')


def test_source_discovery_and_login_failure_preserves_verified(session,tmp_path):
    from tests.housing.test_reports import notice
    n=notice(session,'progress-login',title='光明区住房和建设局关于保租房选房事项的通告',category='process',
             body_text='选房时间为2026年11月4日至11月14日。选房结果查询：https://zjj.sz.gov.cn/result?code=ONE')
    n.batch_key_v2='login-batch';session.flush()
    assert source_candidates(n)==['https://zjj.sz.gov.cn/result?code=ONE']
    async def get(url):return SimpleNamespace(url='https://tyrz.gd.gov.cn/sso/?client_id=x',status=200,text='登录',content=b'')
    asyncio.run(collect_notice(session,SimpleNamespace(get=get),n,now=datetime(2026,11,6,10)))
    check=session.get(HousingMeta,f'selection-progress-check:{n.id}')
    assert check.value['status']=='failed' and '登录' in check.value['error']


def test_render_four_column_table_without_repeating_project_totals():
    from app.housing.daily_render import Document,progress_tables
    s=snap();s['totals']={'supply':15,'selected':4,'remaining':11};s['net_change']=None
    c=SimpleNamespace(selection_progress={'scopes':[s],'check':None},references=[])
    d=Document();progress_tables(d,c)
    text='\n\n'.join(d.text)
    assert '| 小区及实际户型 | 本轮供应 | 已选 | 剩余 |' in text
    assert '125㎡三房二厅' in text and '125㎡四房二厅' in text
    assert '当日选房' not in text and '<details' not in ''.join(d.html) and '<a ' not in ''.join(d.html)
    d=Document();progress_tables(d,c,compact=True)
    assert '累计已选4套' in ''.join(d.text) and '<table' in ''.join(d.html) and '<a ' not in ''.join(d.html)


def test_oversized_progress_is_not_replaced_by_an_external_link(session):
    from tests.housing.test_reports import notice,link
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_report_parts
    n=notice(session,'large-progress',published=datetime(2026,11,1),application_end=datetime(2026,11,2))
    p=notice(session,'large-selection',category='process',published=datetime(2026,11,3),
        title='光明区住房和建设局关于保租房选房安排的通告',body_text='选房时间为2026年11月4日至11月14日。',application_start=None,application_end=None)
    link(session,n,p);snapshot=build_snapshot(session,'digest',now=datetime(2026,11,6,10))
    card=next(c for c in snapshot.cards if c.batch_id==n.id)
    s=snap();s['rows']=s['rows']*300;s['totals']={'supply':4500,'selected':1200,'remaining':3300}
    card.selection_progress={'scopes':[s],'check':None}
    with pytest.raises(ValueError,match='超过单条容量'):
        render_report_parts(snapshot)


def test_pending_progress_lists_known_supply_inline_and_unknown_results():
    from bs4 import BeautifulSoup
    from app.housing.daily_render import Document,progress_tables
    c=SimpleNamespace(selection_progress={'scopes':[],'check':{'checked_at':'2026-11-06T10:00','error':'登录','urls':['https://example.gov.cn/error']}},
        inventory={'projects':[{'name':'A','rows':[{'area':'35','layout':'单房','units':45},{'area':'50','layout':'一房一厅','units':76}]}]})
    d=Document();progress_tables(d,c);html=''.join(d.html);doc=BeautifulSoup(html,'html.parser')
    assert not doc.select('a,details')
    assert len(doc.select('table tr'))==3
    assert '35㎡单房' in html and '50㎡一房一厅' in html
    assert html.count('<td>未核实</td>')==4 and '官方进度入口' not in html


def test_selection_progress_table_is_outside_directory_folds(session):
    from tests.housing.test_reports import notice,link
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_report_parts
    from bs4 import BeautifulSoup
    n=notice(session,'inline-root',published=datetime(2026,11,1),application_end=datetime(2026,11,2))
    p=notice(session,'inline-selection',category='process',published=datetime(2026,11,3),
        title='光明区住房和建设局关于保租房选房安排的通告',body_text='选房时间为2026年11月4日至11月14日。',application_start=None,application_end=None)
    link(session,n,p);snapshot=build_snapshot(session,'digest',now=datetime(2026,11,6,10));c=next(c for c in snapshot.cards if c.batch_id==n.id)
    s=snap();s['totals']={'supply':15,'selected':4,'remaining':11}
    c.selection_progress={'scopes':[s],'check':None}
    doc=BeautifulSoup(render_report_parts(snapshot)[0].wire_html,'html.parser')
    table=next(t for t in doc.select('table') if '小区及实际户型' in t.get_text())
    assert not table.find_parent('details') and 'https://example.gov.cn/result' not in str(table)


def test_template_preview_requires_confirmed_version_before_apply(session,tmp_path,monkeypatch):
    from typer.testing import CliRunner
    from app.housing.cli import app
    import app.housing.cli as cli
    from tests.housing.test_reports import notice
    from contextlib import contextmanager
    n=notice(session,'cli-progress')
    p=tmp_path/'official.csv';p.write_text('项目,实际面积,户型,本轮供应,已选,剩余\nA,50,一房一厅,6,0,6',encoding='utf8')
    @contextmanager
    def db():yield session
    monkeypatch.setattr(cli,'SessionLocal',db)
    from app.housing import operations
    monkeypatch.setattr(operations,'backup_database',lambda **kw:None)
    args=['selection-progress-import','--notice-id',str(n.id),'--file',str(p),'--source','https://example.gov.cn/source']
    runner=CliRunner();preview=runner.invoke(app,args);assert preview.exit_code==0
    result=json.loads(preview.stdout)
    assert result['status']=='preview' and result['messages_sent']==0
    assert runner.invoke(app,[*args,'--apply']).exit_code!=0
    # The successful registration is exercised directly with private-root isolation
    # above; this CLI check verifies a changed file cannot bypass reviewed version.
    p.write_text('项目,实际面积,户型,本轮供应,已选,剩余\nA,50,一房一厅,6,1,5',encoding='utf8')
    assert runner.invoke(app,[*args,'--apply','--confirm-version',result['version']]).exit_code!=0


@pytest.mark.parametrize('region,audience',[('深圳市','personal'),('宝安区','enterprise'),('坪山区','personal')])
def test_city_and_district_progress_is_independent_of_queue_rules(session,tmp_path,region,audience):
    from tests.housing.test_reports import notice
    from app.housing.reporting import build_snapshot
    n=notice(session,'scope-'+region,region=region,audience=audience,published=datetime(2026,11,1),application_end=datetime(2026,11,2))
    from tests.housing.test_reports import link
    p=notice(session,'selection-'+region,region=region,audience=audience,category='process',published=datetime(2026,11,3),
             title=('深圳市住房和建设局' if region=='深圳市' else region+'住房和建设局')+'关于保租房选房安排的通告',
             body_text='选房时间为2026年11月4日至11月14日。',application_start=None,application_end=None)
    link(session,n,p)
    snap=build_snapshot(session,'evening',now=datetime(2026,11,6,10))
    card=next(c for c in snap.cards if c.batch_id==n.id)
    assert card.selection_progress['pending']
    assert card.qualification is None
