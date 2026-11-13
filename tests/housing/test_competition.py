import copy
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.housing.competition import (count_records, summarize_category, batch_summary, save_evidence,
    competition_text, supply_catalog, _pool_overlap, rule_summary, categories_from_payload)
from app.housing.models import HousingNotice, HousingBatchUnit
from app.housing.opportunity import analyze_simple, scenario_comparison
from app.housing.reporting import REGIONS, build_snapshot


def notices(session, region):
    common = dict(region_code=region, batch_key_v2='verified-batch', business_version='v1',
                  is_target=True, published_at=datetime(2026,10,22), attachments=[])
    launch = HousingNotice(canonical_key='launch', url_hash='launch', canonical_url='https://example.gov.cn/launch',
        title=f'{region}住房和建设局关于2026年第一批面向个人配租公共租赁住房的通告',
        category='rent_launch', application_start=datetime(2026,10,1), application_end=datetime(2026,10,15), **common)
    node = HousingNotice(canonical_key='lottery', url_hash='lottery', canonical_url='https://example.gov.cn/lottery',
        title=f'{region}住房和建设局关于2026年第一批公共租赁住房公证摇号结果的通告',
        category='lottery_result', **common)
    session.add_all([launch,node]);session.flush()
    from app.housing.models import HousingBatchAnchor
    session.add(HousingBatchAnchor(key='verified-batch',launch_id=launch.id,region=region))
    session.flush()
    return launch, node


@pytest.mark.parametrize('region', ('深圳市',*REGIONS))
def test_every_region_uses_its_own_band_and_queue_names(session, region):
    launch,node=notices(session,region)
    counts=count_records([('ANON001','优先配租队列',1),('ANON002','其他配租队列',1),('ANON003','其他配租队列',1)])
    save_evidence(session,node,[{'category':'61-69','label':'61—69㎡两房',**counts,
        'url':node.canonical_url,'published_at':'2026-10-22','queue_mode':'unknown'}])
    session.add(HousingBatchUnit(batch_key='verified-batch',category_key='61-69',units=2,
        verified=True,source_notice_id=launch.id,source_url=launch.canonical_url));session.flush()
    card=next(c for c in build_snapshot(session,'evening',now=datetime(2026,10,23,20,30)).cards if node.id in c.notice_ids)
    assert card.lottery_breakdown[0]['competition_ratio']=='1.5'
    text=competition_text(card.lottery_breakdown)
    assert '61—69㎡两房' in text and '优先配租队3户' not in text and '其他配租队2户' in text
    assert '光明户籍' not in '\n'.join(card.lottery_rules)
    assert '40㎡' not in text and '首轮选房后' not in '\n'.join(card.lottery_rules)


@pytest.mark.parametrize('people,units,ratio',[(3746,111,'33.7'),(1526,76,'20.1'),(1848,549,'3.4'),(3810,1227,'3.1'),(1,4,'0.3')])
def test_verified_ratios_use_half_up_and_not_percentages(people,units,ratio):
    result=summarize_category({'category':'x','households':people,'households_verified':True},
                              {'units':units,'verified':True})
    assert result['competition_ratio']==ratio and result['initial_units']==units


def test_unique_households_and_rounds_are_not_record_counts():
    result=count_records([('ANON001','第一队列',1),('ANON001','第一队列',1),('ANON001','第一队列',2),('ANON002','第二队列',1)])
    assert result['households']==2 and result['records']==4 and result['round_ambiguous']
    assert result['queues']=={'第一队列':1,'第二队列':1}
    summary=summarize_category(result,{'units':5,'verified':True})
    assert summary['competition_ratio'] is None
    assert not count_records([('ANON001','第一队列',1),('ANON001','第二队列',1)])['queue_complete']


def test_missing_partial_and_unknown_queue_are_distinct():
    item=count_records([('ANON001','',1)])
    assert item['households_verified'] and not item['queue_complete']
    assert summarize_category(item,{'units':2,'verified':True})['competition_ratio']=='0.5'
    assert not count_records([('ANON001','第一队列',1)],complete=False)['households_verified']
    for supply in ({},{'units':0,'verified':True},{'units':10,'verified':False}):
        assert summarize_category(item,supply)['competition_ratio'] is None
    item.update(category='all',label='统一摇号',queue_mode='none',published_at='2026-10-22')
    assert '本批未划分队列' in competition_text([item])
    item['queue_mode']='unknown'
    assert '队列信息未公开' in competition_text([item])


def test_overlapping_pools_cannot_repeat_supply_and_unknown_hashes_are_not_areas():
    assert _pool_overlap(['40-60','50'])
    assert _pool_overlap(['项目花园|一房','一房'])
    assert not _pool_overlap(['unknown:11a49958312cdb8d','unknown:22a8583733b5e59d'])
    assert not _pool_overlap(['40','50','90','100+'])
    assert summarize_category({'households':1,'households_verified':True},{'units':5,'verified':True},shared_pool=True)['competition_ratio'] is None


def test_source_changes_invalidate_anonymous_evidence_and_wrong_batch_units(session):
    launch,node=notices(session,'宝安区')
    save_evidence(session,node,[{'category':'一房','label':'一房户型',**count_records([('ANON001','',1)]),'published_at':'2026-10-22'}])
    assert batch_summary(session,node)[0]['households']==1
    node.attachment_version='changed'
    assert batch_summary(session,node)==[]
    launch.batch_key_v2='other-batch'
    session.add(HousingBatchUnit(batch_key='verified-batch',category_key='一房',units=80,
        verified=True,source_notice_id=launch.id));session.flush()
    assert supply_catalog(session,'verified-batch')=={}


def test_physical_layouts_do_not_become_guangming_area_bands(session):
    launch,node=notices(session,'宝安区')
    inv={'batch_key':'verified-batch','complete':True,'sources':[{'url':launch.canonical_url}],
         'projects':[{'rows':[{'layout':'一房','area':'约35—39㎡','band':'area:35','units':68},
                             {'layout':'二房','area':'约65—71㎡','units':148},
                             {'layout':'三房','area':'约89㎡','units':37}]}]}
    catalog=supply_catalog(session,'verified-batch',inv)
    assert catalog['一房']['units']==68 and catalog['两房']['units']==148
    assert '40' not in catalog and '50' not in catalog


@pytest.mark.parametrize('ahead,capacity',[(0,1),(1,1),(3,2),(5,4),(1151,550),(834,76)])
def test_sensitivity_ceil_boundaries_and_browser_parity(ahead,capacity):
    result={'status':'scenario','scenario':{},'assumptions':{'ahead':ahead},'facts':{'initial_units':capacity}}
    result['scenario']={'required_not_consuming':max(0,ahead-capacity+1)}
    before=copy.deepcopy(result)
    rows=scenario_comparison(result)
    assert rows[0]['consuming']==ahead
    assert rows[1]['consuming']==(ahead*80+99)//100
    assert rows[2]['consuming']==(ahead*60+99)//100
    assert rows[0]['remaining']<=rows[1]['remaining']<=rows[2]['remaining'] and result==before
    script="const e=require(process.argv[1]);process.stdout.write(JSON.stringify(e.scenarioComparison(JSON.parse(process.argv[2]))));"
    run=subprocess.run(['node','-e',script,str(Path('app/housing/opportunity.js').resolve()),json.dumps(result)],capture_output=True,text=True,encoding='utf-8',check=True)
    assert json.loads(run.stdout)==rows


def test_invalid_analysis_has_no_sensitivity_table():
    assert scenario_comparison({'status':'pending','scenario':None})==[]


def test_real_baoan_layouts_and_priority_are_not_guangming_rules(session):
    fixture=json.loads(Path('tests/housing/fixtures/inventory/baoan-2026-023.json').read_text(encoding='utf-8'))
    launch,node=notices(session,'宝安区')
    launch.title=fixture['title'];launch.body_text=fixture['body_text'];launch.canonical_url=fixture['source_url']
    inv=fixture['inventory'];inv['batch_key']='verified-batch'
    catalog=supply_catalog(session,'verified-batch',inv)
    for label,households,units,ratio in [('一房',15384,167,'92.1'),('两房',8290,259,'32.0'),('三房',1646,37,'44.5')]:
        item={'category':label,'households':households,'households_verified':True}
        result=summarize_category(item,catalog[label])
        assert result['initial_units']==units and result['competition_ratio']==ratio
    text='\n'.join(rule_summary(session,node,launch,inv))
    assert '两房三人及以上' in text and '7套无障碍' in text
    assert '宝安累计社保1年以上' in text and '07-31' in text
    assert '光明' not in text and '首轮选房后仍有房源' not in text


def test_latest_partial_event_never_borrows_previous_complete_round(session):
    launch,node=notices(session,'盐田区')
    save_evidence(session,node,[{'category':'65','label':'65㎡',**count_records([('ANON001','第一队列',1)]),
                               'published_at':'2026-10-22','url':node.canonical_url}])
    later=HousingNotice(canonical_key='later',url_hash='later',canonical_url='https://example.gov.cn/later',
        title='盐田区住房和建设局关于新一轮公共租赁住房公证摇号结果的通告',region_code='盐田区',
        batch_key_v2='verified-batch',category='lottery_result',is_target=True,published_at=datetime(2026,10,23))
    session.add(later);session.flush()
    snapshot=build_snapshot(session,'evening',now=datetime(2026,10,23,20,30))
    card=next(c for c in snapshot.cards if later.id in c.notice_ids)
    assert card.lottery_breakdown[0]['category']=='unparsed'
    assert card.lottery_breakdown[0]['url']==later.canonical_url


def test_unlinked_mirrors_merge_but_distinct_rounds_survive(session):
    from app.housing.lookup_data import event_groups,merge_mirror_payload
    launch,node=notices(session,'坪山区');node.batch_key_v2=None
    node.attachments=[{'url':'https://example.gov.cn/attachment/1/2/333/a.pdf'}]
    alias=HousingNotice(canonical_key='alias',url_hash='alias',canonical_url='https://example.gov.cn/alias',title=node.title,region_code=node.region_code,
        category='lottery_result',published_at=node.published_at,attachments=[{'url':'https://example.gov.cn/gkmlpt/attachment/1/2/333/b.pdf'}])
    later=HousingNotice(canonical_key='later',url_hash='later',canonical_url='https://example.gov.cn/later',title=node.title,region_code=node.region_code,
        category='lottery_result',published_at=node.published_at+timedelta(days=1),attachments=node.attachments)
    session.add_all([alias,later]);session.flush()
    assert len(event_groups([node,alias,later]))==2
    def batch(n):return {'id':n.id,'c':[{'key':'65','l':'65㎡'}],'Q':['未标注'],'source_ids':[n.id],'sources':[n.canonical_url]}
    payload={'batches':[batch(node),batch(alias),batch(later)],'rows':{'ANON001':[[0,0,0,1,1,0,1],[1,0,0,1,1,0,1],[2,0,0,1,1,0,2]]}}
    merge_mirror_payload(session,payload)
    assert len(payload['batches'])==2 and len(payload['rows']['ANON001'])==2
    assert payload['batches'][0]['source_ids']==[node.id,alias.id]


def test_priority_group_unknown_never_becomes_positive_personal_conclusion():
    from tests.housing.test_simple_opportunity import example,values
    data,match=example()
    data['batches'][0]['opportunity']['rule'].update(special_priority_band='50',evidence='本档存在特殊优先')
    result=analyze_simple(data,match,values(people=3,children=1))
    assert '优先重排' in result['readable']['headline'] and len(result['comparison'])==3
    data['batches'][0]['final_rows']={'TEST000001':{'rank':1,'ahead_same_band':0,'scope_verified':True,'url':'https://example.gov.cn/final'}}
    final=analyze_simple(data,match,values(queue='第二队列',people=4,children=2))
    assert final['assumptions']['basis']=='official-final' and final['missing']==[]
    assert '名额范围内' in final['readable']['headline']


def test_other_batches_have_their_own_queue_order_and_family_priority():
    from tests.housing.test_simple_opportunity import example,values
    from app.housing.opportunity import queue_reference
    data,match=example()
    rule=data['batches'][0]['opportunity']['rule']
    rule.update(id='another-reviewed-document',queue_order=['第三队列','第一队列','第二队列'],
        special_priority_band='50',special_priority_conditions=[{'min_people':2,'min_children':1}],evidence='本批自有优先规则')
    assert queue_reference(data,match,'第三队列')['ahead']==1
    result=analyze_simple(data,match,values(people=3,children=1))
    assert result['self_report']['priority_candidate'] and result['assumptions']['ahead']==1
    script="const e=require(process.argv[1]),x=JSON.parse(process.argv[2]);process.stdout.write(JSON.stringify(e.analyzeSimple(x.d,x.m,x.v)));"
    run=subprocess.run(['node','-e',script,str(Path('app/housing/opportunity.js').resolve()),json.dumps({'d':data,'m':match,'v':values(people=3,children=1)})],capture_output=True,text=True,encoding='utf-8',check=True)
    assert json.loads(run.stdout)==result
    rule.pop('special_priority_conditions')
    assert not analyze_simple(data,match,values(people=4,children=2))['self_report']['priority_candidate']
