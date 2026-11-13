import copy
import json
import subprocess
from pathlib import Path

import pytest

from app.housing.opportunity import analyze_simple,query_summary


def example():
    batch={'t':'测试批次','d':'2026-10-16','Q':['第一队列','第二队列','第三队列'],
           'c':[{'key':'50','l':'50㎡左右','u':2,'n':6,'q':{'第一队列':1,'第二队列':2,'第三队列':3},'issue':''}],
           'R':{'0|0':[2],'0|1':[3,5],'0|2':[1,4,6]},
           'opportunity':{'complete':True,'projects':[{'id':'A','name':'公寓A','rows':[{'band':'50','units':2}]}],
                          'rule':{'verified':True,'minimum_people':{'50':2},'special_priority_band':'100+'}}}
    return {'batches':[batch]},[0,0,1,3,1,1,1]


def values(**change):
    return {'apply_no':'TEST000001','queue':'第三队列','projects':['A'],'people':3,'children':1,**change}


def test_overall_lottery_rank_is_not_queue_priority_combined_rank():
    data,match=example();original=copy.deepcopy(data)
    result=query_summary(data,match,'第三队列')
    assert result['overall_rank']==3 and result['queue_rank']==2
    assert result['queue_rank_is_reference'] and result['initial_units']==2 and data==original
    assert result['round']==1 and result['published_at']=='2026-10-16'
    del data['batches'][0]['R']['0|1']
    assert query_summary(data,match,'第三队列')['overall_rank'] is None


def test_plain_conclusion_explains_higher_queues_and_no_probability():
    data,match=example();original=copy.deepcopy(data)
    result=analyze_simple(data,match,values())
    assert result['readable']['headline']=='按初始房源保守测算，暂时排不到你'
    assert '前面队列有2户' in result['readable']['explanation']
    assert '本队列在你前面有1户，共3户' in result['readable']['explanation']
    assert '至少要有2户' in result['readable']['needed_change']
    assert '不全是放弃' in result['readable']['needed_change'] and data==original
    assert '选择其他小区' not in result['readable']['needed_change']
    assert '概率' not in result['readable']['headline'] and '%' not in str(result['readable'])


def test_simple_uses_verified_final_order_automatically_when_comparable():
    data,match=example();data['batches'][0]['final_rows']={'TEST000001':{'rank':1,'ahead_same_band':0,'scope_verified':True,'url':'https://example.gov.cn/final'}}
    result=analyze_simple(data,match,values(queue='第二队列'))
    assert result['assumptions']['basis']=='official-final' and '名额范围内' in result['readable']['headline']
    changed=analyze_simple(data,match,values())
    assert changed['assumptions']['basis']=='preliminary'


def test_missing_data_or_queue_does_not_produce_fake_success():
    data,match=example()
    result=analyze_simple(data,match,values(queue=''))
    assert result['scenario'] is None and '选择本人队列' in result['readable']['explanation']
    data['batches'][0]['opportunity']['complete']=False
    result=analyze_simple(data,match,values())
    assert result['scenario'] is None and '资料不足' in result['readable']['headline']


def test_unverified_historical_cache_cannot_produce_rank_or_chance_conclusion():
    data,match=example();data['batches'][0]['stale']=True
    summary=query_summary(data,match,'第三队列')
    assert summary['overall_rank'] is None and summary['queue_rank'] is None
    result=analyze_simple(data,match,values())
    assert result['scenario'] is None and result['readable']['headline']=='目前资料不足，暂不能判断能否轮到你'


def test_real_rank_comes_from_sorted_roster_even_with_gaps_or_missing_units():
    data,match=example();data['batches'][0]['R']={'0|0':[10],'0|1':[30,50],'0|2':[20,40,60]}
    data['batches'][0]['c'][0]['u']=0;match[3]=30
    result=query_summary(data,match,'第二队列')
    assert result['overall_rank']==3 and result['order']==30 and result['queue_rank']==1
    assert result['initial_units'] is None


def test_special_priority_is_a_missing_group_rank_not_a_fake_rejection():
    data,match=example();batch=data['batches'][0]
    batch['opportunity']['rule'].update(special_priority_band='50',evidence='对应序位内的特殊优先',
        special_priority_conditions=[{'people':4,'min_children':2}])
    result=analyze_simple(data,match,values(people=4,children=2))
    assert result['readable']['headline']=='你涉及家庭优先，暂不能只凭普通排名判断结果'
    assert '特殊优先条款' in result['readable']['next_step']


def test_complete_roster_without_queue_classification_still_has_overall_rank():
    data,match=example();cat=data['batches'][0]['c'][0]
    cat.update(roster_complete=True,queue_complete=False,issue='部分队列未标注，不能确认队列优先关系')
    result=query_summary(data,match,'第三队列')
    assert result['overall_rank']==3 and result['queue_rank'] is None


def test_missing_batch_units_does_not_hide_verified_queue_rank():
    data,match=example();data['batches'][0]['c'][0].update(roster_complete=True,queue_complete=True,u=0,issue='批次未核实，暂停房源估算')
    result=query_summary(data,match,'第二队列')
    assert result['queue_rank']==1 and result['overall_rank']==3 and result['initial_units'] is None


@pytest.mark.parametrize('input_change',[{}, {'queue':'第二队列'}, {'people':None}, {'queue':''}])
def test_plain_results_and_query_ranks_match_browser_engine(input_change):
    data,match=example();v=values(**input_change)
    js=Path('app/housing/opportunity.js').resolve()
    script="const fs=require('fs'),e=require(process.argv[1]),d=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify({query:e.querySummary(d.data,d.match,d.values.queue),analysis:e.analyzeSimple(d.data,d.match,d.values)}));"
    run=subprocess.run(['node','-e',script,str(js)],input=json.dumps({'data':data,'match':match,'values':v}),text=True,encoding='utf-8',capture_output=True,check=True)
    assert json.loads(run.stdout)=={'query':query_summary(data,match,v['queue']),'analysis':analyze_simple(data,match,v)}
