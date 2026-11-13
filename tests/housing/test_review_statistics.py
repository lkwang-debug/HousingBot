import copy
import json
import subprocess
from pathlib import Path

import pytest

from app.housing.review_statistics import statistics
from app.housing.opportunity import qualified_reference,filtered_reference,analyze_simple,intention_union
from app.housing.qualification import parse_pdf,parse_table,normalize_records,project_intentions


def fixture():
    batch={'Q':['优先组','普通组'],'c':[{'key':'两房','l':'两房','n':4,'u':2,'q':{'优先组':2,'普通组':2},'queue_complete':True}],
           'R':{'0|0':[1,2],'0|1':[3,4]},'qualification':{'pending':False,'notice_id':10,'bands':[{'band':'两房','qualified':2}]},
           'opportunity':{'complete':True,'rule':{'verified':True,'queue_order':['优先组','普通组']},
                          'projects':[{'id':'A','name':'匿名花园甲','rows':[{'band':'两房','units':2}]}]},
           'review_rows':{}}
    data={'district':'测试区','batches':[batch],'rows':{}}
    for i,status in enumerate(['qualified','ineligible','qualified','not_reviewed'],1):
        no=f'TEST00000{i}';queue=0 if i<3 else 1;rank=i if i<3 else i-2
        data['rows'][no]=[[0,0,queue,i,rank,queue,1]]
        batch['review_rows'][no]={'band':'两房','status':status,'queue':'','notice_id':10,'published_at':'2026-10-28',
                                  'phase':'publicity','intentions':['匿名花园甲','匿名花园乙'],'intentions_verified':True}
    return data,data['rows']['TEST000003'][0]


def test_cross_tab_uses_historical_labels_and_project_sets_are_households():
    data,m=fixture();b=data['batches'][0]
    projects=[{'name':'匿名花园甲','rows':[{'band':'两房','units':2}]},{'name':'匿名花园乙','rows':[{'band':'两房','units':1}]}]
    result=statistics(data,0,b['review_rows'],projects)
    assert result['matched_households']==4 and result['unmatched_households']==0
    row=next(r for r in result['original_queue_rows'] if r['original_queue']=='普通组')
    assert row['qualified']==1 and row['not_reviewed']==1
    assert '不是最新各队列' in result['original_queue_label']
    assert [r['qualified_households'] for r in result['project_intentions']]==[2,2]
    assert [r['ratio'] for r in result['project_intentions']]==['1.0','2.0']
    b['review_rows']['TEST000001']['intentions_verified']=False
    result=statistics(data,0,b['review_rows'],projects)
    assert all(r['ratio'] is None for r in result['project_intentions'])


def test_qualified_original_order_is_not_current_queue_rank():
    data,m=fixture();result=qualified_reference(data,m,'TEST000003')
    assert result['available'] and result['ahead']==1 and result['rank']==2 and result['total']==2
    assert '不是当前梯队' in result['message']
    data['batches'][0]['qualification']['bands'][0]['qualified']=3
    assert not qualified_reference(data,m,'TEST000003')['available']


def test_filtered_reference_keeps_unreviewed_and_moves_only_self():
    data,m=fixture();before=copy.deepcopy(data)
    result=filtered_reference(data,m,'TEST000003','普通组')
    assert result['available'] and result['before']==2 and result['excluded']==1 and result['ahead']==1
    assert data==before
    data['batches'][0]['review_rows']['TEST000002']['status']='not_reviewed'
    result=filtered_reference(data,m,'TEST000003','普通组')
    assert result['excluded']==0 and result['ahead']==2 and result['unreviewed_ahead']==1
    data['batches'][0]['review_rows']['TEST000002']['status']='qualified'
    assert filtered_reference(data,m,'TEST000003','普通组')['excluded']==0


@pytest.mark.parametrize('queue',['优先组','普通组'])
def test_python_js_filtered_analysis_and_reference_parity(queue):
    data,m=fixture();values={'apply_no':'TEST000003','queue':queue,'projects':['A'],'people':3,'children':1,'use_review_filter':True}
    py={'analysis':analyze_simple(data,m,values),'qualified':qualified_reference(data,m,values['apply_no']),
        'filtered':filtered_reference(data,m,values['apply_no'],queue)}
    script=Path('app/housing/opportunity.js').read_text(encoding='utf-8')+'''
const x=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify({analysis:Opportunity.analyzeSimple(x.data,x.match,x.values),qualified:Opportunity.qualifiedReference(x.data,x.match,x.values.apply_no),filtered:Opportunity.filteredReference(x.data,x.match,x.values.apply_no,x.values.queue)}));'''
    js=json.loads(subprocess.run(['node','-e',script],input=json.dumps({'data':data,'match':m,'values':values}),capture_output=True,text=True,encoding='utf-8',check=True).stdout)
    assert py==js
    assert py['analysis']['scenario'] is not None


def test_pending_and_conflicting_or_duplicate_scope_never_produce_filtered_reference():
    data,m=fixture();data['batches'][0]['qualification']['pending']=True
    assert not filtered_reference(data,m,'TEST000003','普通组')['available']
    data['batches'][0]['qualification']['pending']=False
    data['rows']['TEST000001'].append(data['rows']['TEST000001'][0])
    assert not filtered_reference(data,m,'TEST000003','普通组')['available']


def test_excel_intentions_only_use_explicit_column_and_same_batch_catalog():
    table=[['备案回执号','认租户型','认租项目'],['TEST000001','两房','匿名花园甲；匿名花园乙；匿名花园甲']]
    r=parse_table(table,'qualified',project_catalog=['匿名花园甲','匿名花园乙'])
    assert r[0]['intentions']==['匿名花园乙','匿名花园甲'] and r[0]['intentions_verified']
    table[0][2]='姓名'
    assert not parse_table(table,'qualified',project_catalog=['匿名花园甲'])[0]['intentions_verified']


def test_pdf_intentions_do_not_keep_raw_names_or_ids(monkeypatch):
    import pypdf
    from types import SimpleNamespace
    text='序号 认租编号 姓名 身份证 意向分类 认租意向 原意向摇号顺序号\n1 TEST000001 匿名甲 123456******** 建筑面积50㎡左右 匿名花园甲50㎡；匿名花园乙50㎡ 547'
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:text)]))
    r=parse_pdf(b'pdf','qualified',project_catalog=['匿名花园甲','匿名花园乙'])
    assert len(r[0]['intentions'])==2 and r[0]['order']==547
    assert '123456' not in json.dumps(r) and '匿名甲' not in json.dumps(r,ensure_ascii=False)


def test_same_family_multi_project_rows_merge_but_status_or_queue_conflicts_do_not():
    a={'apply_no':'TEST000001','band':'两房','queue':'普通组','status':'qualified','order':None,
       'intentions':['匿名花园甲'],'intentions_verified':True}
    b=dict(a,intentions=['匿名花园乙'])
    result=normalize_records([a,b])
    assert len(result)==1 and len(result['TEST000001']['intentions'])==2
    with pytest.raises(ValueError):normalize_records([a,dict(b,status='ineligible')])
    with pytest.raises(ValueError):normalize_records([a,dict(b,queue='优先组')])
    assert project_intentions('明皓府；嘉禧苑；虹霓轩',['明皓府','嘉禧苑','虹霓轩'])==['嘉禧苑','明皓府','虹霓轩']


def test_intention_union_uses_anonymous_patterns_without_adding_projects_or_leaking_ids():
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token
    data,m=fixture();b=data['batches'][0]
    b['opportunity']['projects'].append({'id':'B','name':'匿名花园乙','rows':[{'band':'两房','units':1}]})
    assert intention_union(data,m,'TEST000003',['A','B'])['households']==2
    secret='anonymous-privacy-test-key-1234567890123456789'
    server=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://lookup.example')
    result=server.handle({'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret)},
        'body':json.dumps({'region':'test','apply_no':'TEST000003','queue':'普通组','version':'v'})})
    assert result['statusCode']==200
    limited=json.loads(result['body'])['payload'];text=json.dumps(limited)
    assert all(no not in text for no in ['TEST000001','TEST000002','TEST000004'])
    assert intention_union(limited,limited['rows']['TEST000003'][0],'TEST000003',['A','B'])['households']==2
    b['qualification']['pending']=True
    assert not intention_union(data,m,'TEST000003',['A','B'])['verified']


def test_review_only_records_are_queryable_without_fabricated_orders_or_ranks():
    from app.housing.lookup import validate_payload
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token
    from app.housing.opportunity import query_summary,review_position
    data,m=fixture();b=data['batches'][0];b['review_only']=True;b['R']={}
    for ms in data['rows'].values():
        for row in ms:row[3]=None;row[4]=None
    assert validate_payload(data)
    assert query_summary(data,m,'普通组')['queue_rank'] is None
    assert not review_position(data,m,'TEST000003')['verified']
    assert not qualified_reference(data,m,'TEST000003')['available']
    secret='anonymous-review-only-test-key-123456789012345'
    server=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://lookup.example')
    r=server.handle({'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret)},'body':json.dumps({'region':'test','apply_no':'TEST000003','queue':'普通组','version':'v'})})
    assert r['statusCode']==200
    result=json.loads(r['body'])['payload'];assert result['batches'][0]['access_context']['summary']['order'] is None
    assert set(result['rows'])=={'TEST000003'}
