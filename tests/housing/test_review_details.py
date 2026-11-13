import copy
import json
from datetime import datetime
from types import SimpleNamespace

from app.housing.review_details import concrete_intentions,unit_summary,reason_summary,reason_text
from app.housing.handling import extra_windows,state,display,guidance


def notice(body):
    return SimpleNamespace(id=1,published_at=datetime(2026,10,28),published_unverified=False,
        canonical_url='https://example.gov.cn/notice',body_text=body)


def test_same_area_different_layout_and_multiple_intentions_count_once():
    types=concrete_intentions('明皓府建筑面积125㎡左右的三房二厅；明皓府建筑面积125㎡左右的四房二厅；明皓府建筑面积125㎡左右的三房二厅',['明皓府'])
    assert len(types)==2
    records={'A':{'apply_no':'A','status':'qualified','band':'100+','types_verified':True,'intention_types':types},
             'B':{'apply_no':'B','status':'qualified','band':'100+','types_verified':True,'intention_types':types[:1]}}
    projects=[{'name':'明皓府','rows':[{'band':'100+','area':'约125','layout':'三房二厅','units':69},
                                   {'band':'100+','area':'约125','layout':'四房二厅','units':71}]}]
    original=copy.deepcopy(records);result=unit_summary(records,projects)
    assert [r['qualified_intentions'] for r in result]==[2,1] and records==original
    projects[0]['rows'][0]['shared_pool']=True
    assert unit_summary(records,projects)[0]['ratio'] is None
    records['B']['types_verified']=False
    assert all(r['ratio'] is None for r in unit_summary(records,projects))


def test_reason_categories_and_exact_combinations_have_distinct_denominators():
    records={str(i):{'status':status,'reason':reason,'reason_verified':bool(reason)} for i,(status,reason) in enumerate([
        ('ineligible','未按规定提交申请材料'),('ineligible','未按规定提交申请材料；申请人不符合人才引进迁户核准条件'),
        ('ineligible',''),('withdrawn','撤销认租'),('not_reviewed','')])}
    s=reason_summary(records)
    assert s['denominator']==3 and s['parsed']==2 and s['missing']==1 and s['coverage']=='66.7'
    assert next(r for r in s['categories'] if r['label']=='材料缺失')['households']==2
    assert sum(r['households'] for r in s['exact'])==2
    assert next(r for r in s['categories'] if r['label']=='人才条件或类型不匹配')['households']==1
    assert reason_text('身份证123456********')==''


def test_gm_publicity_appeal_and_withdrawal_do_not_share_period_or_scope():
    from app.housing.daily_lifecycle import action_windows
    body=('公示时间为2026年10月28日至2026年11月3日。\n'
          '本次公示仅受理对所属队列认定结果的异议意见，受理时间：2026年10月29日至10月30日，工作日9:30-11:30，14:30-17:00。\n'
          '认租家庭如需放弃本次认租，应于2026年10月29日9:00至11月3日18:00在线提交撤销本批认租申请。\n'
          '公示期内提交放弃认租申请，不计入弃选次数。')
    rows=action_windows(notice(body));by={r['kind']:r for r in rows}
    assert {'review','appeal','withdrawal'}<=set(by)
    assert by['appeal']['end']=='2026-10-30T17:00:00'
    assert by['appeal']['scope']=='仅受理所属队列认定异议'
    assert by['withdrawal']['end']=='2026-11-03T18:00:00'
    assert by['withdrawal']['start']=='2026-10-29T09:00:00'
    now=datetime(2026,11,3,10)
    assert state(by['appeal'],now)=='已截止'
    assert state(by['withdrawal'],now)=='办理中'
    assert '17:00' in display(by['appeal'],now)
    assert not any(r['kind']=='supplement' for r in rows)
    assert '仅受理所属队列' in '；'.join(guidance('ineligible',rows,now))


def test_other_district_cross_year_and_explicit_supplement_do_not_copy_gm_rules():
    n=notice('复核受理时间为2026年12月30日至2027年1月4日，工作日9:00-12:00、14:00-17:30。补正时间为2027年1月5日9:00至1月6日16:00，材料范围详见清单。')
    rows=extra_windows(n);appeal=next(r for r in rows if r['kind']=='appeal');supp=next(r for r in rows if r['kind']=='supplement')
    assert appeal['end']=='2027-01-04T17:30:00' and '仅受理所属队列' not in appeal['scope']
    assert supp['end']=='2027-01-06T16:00:00'
    assert state(appeal,datetime(2027,1,4,13))=='期间内，当前不在受理时段'
    assert state(supp,datetime(2027,1,6,16,1))=='已截止'
    assert not extra_windows(notice('本批不设补正，2026年11月3日结束公示。'))


def test_date_only_deadline_never_prints_internal_2359_or_invents_permission():
    n=notice('异议受理时间为2026年10月29日至11月3日。')
    r=extra_windows(n)[0]
    text=display(r,datetime(2026,11,3,11))
    assert '具体时刻见原文' in text and '23:59' not in text
    assert '补交' not in '；'.join(guidance('ineligible',[r],datetime(2026,11,3,11)))


def test_concrete_unmatched_area_range_does_not_force_competition():
    projects=[{'name':'测试公寓','rows':[{'band':'一房','layout':'一房一厅','area':'约35—45','units':100}]}]
    row={'apply_no':'A','status':'qualified','band':'一房','types_verified':True,
         'intention_types':[{'project':'测试公寓','area':'40','layout':'一房一厅'}]}
    assert not unit_summary({'A':row},projects)


def test_query_returns_only_own_reason_and_server_time_handling_guidance():
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token,BEIJING
    from tests.housing.test_review_statistics import fixture
    data,m=fixture();b=data['batches'][0]
    b['review_rows']['TEST000003'].update(status='ineligible',label='不合格',reason='申请人未按规定提交材料',reason_verified=True)
    b['review_rows']['TEST000002'].update(reason='OTHER-HOUSEHOLD-PRIVATE-REASON',reason_verified=True)
    b['handling']=extra_windows(notice('仅受理对所属队列认定结果的异议，受理时间：2026年10月29日至10月30日，工作日9:30-11:30、14:30-17:00。撤销认租可于2026年10月29日9:00至11月3日18:00办理。'))
    b['qualification']['reason_summary']={'categories':[],'exact':[{'reason':'COMBINATION-NOT-NEEDED-IN-QUERY'}]}
    now=datetime(2026,11,3,10,tzinfo=BEIJING);secret='anonymous-review-details-private-key-1234567890'
    s=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://test.example',lambda:now)
    r=s.handle({'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret,now)},
        'body':json.dumps({'region':'test','apply_no':'TEST000003','queue':'普通组','version':'v'})})
    assert r['statusCode']==200
    text=r['body'];p=json.loads(text)['payload']['batches'][0]
    assert 'OTHER-HOUSEHOLD' not in text and 'TEST000002' not in text and 'COMBINATION-NOT-NEEDED' not in text
    assert p['review_rows']['TEST000003']['reason']=='申请人未按规定提交材料'
    assert any('异议／复核：已截止' in t for t in p['handling_guidance'])
    assert any('撤销认租：办理中' in t for t in p['handling_guidance'])


def test_table_reason_preserves_combinations_negation_and_is_not_personal_judgment():
    from app.housing.qualification import parse_table
    rows=[['认租编号','意向分类','不合格原因'],['TEST000001','一房','未按规定提交材料；不符合本批人才条件'],
          ['TEST000002','一房','撤销认租']]
    result=parse_table(rows,'mixed_negative')
    assert result[0]['reason']=='未按规定提交材料；不符合本批人才条件' and result[0]['reason_verified']
    assert result[1]['status']=='withdrawn'


def test_wrapped_html_dates_afternoon_and_attachment_footer_are_not_handling_windows():
    text=('异议受理时间为2026年10月22日至10月28日，工作日（上午9:00-12:00，下午2:00-6:00）。'
          '网上撤销本次认租项目的时间为2026年10月22日\n9\n:00至10月28日18:00，逾期不再受理。'
          '附件2：不合格及撤销认租家庭名单\n住房建设局\n2026年10月28日')
    rows=extra_windows(notice(text));assert len(rows)==2
    assert all(r['end']=='2026-10-28T18:00:00' for r in rows)
    assert next(r for r in rows if r['kind']=='withdrawal')['start']=='2026-10-22T09:00:00'


def test_single_deadline_does_not_turn_into_a_future_opening_day():
    r=extra_windows(notice('如需放弃本次认租，请在公示期间内（2026年11月5日18:00前）提交撤销认租申请。'))[0]
    text=display(r,datetime(2026,11,3,10))
    assert '办理中' in text and '截至11-05 18:00' in text and '11-05—11-05' not in text


def test_reason_column_continues_without_repeated_header(monkeypatch):
    import pdfplumber
    from app.housing.review_details import pdf_reason_columns
    class PDF:
        pages=[SimpleNamespace(extract_tables=lambda:[[['序号','备案回执号','不合格原因'],['1','TEST000001','材料缺失']]]),
               SimpleNamespace(extract_tables=lambda:[[['2','TEST000002','主申请人社保不符'],['3','TEST000003','未更新家庭信息']]])]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda _:PDF())
    rows=pdf_reason_columns(b'pdf')
    assert len(rows)==3 and rows['TEST000002']=='主申请人社保不符'


def test_selection_document_is_not_a_new_review_scope_and_rank_is_explicit(monkeypatch):
    import pypdf
    from app.housing.qualification import parse_pdf,review_files
    text='合格认租家庭选房排位名单\n选房排位号 认租编号 意向分类 队列分类 摇号顺序号\n169 TEST000001 建筑面积50㎡左右的一房 第二队列 547'
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:text)]))
    rows=parse_pdf(b'pdf','qualified')
    assert rows[0]['selection_rank']==169 and rows[0]['queue']=='第二队列'
    n=notice('选房安排');n.title='合格家庭选房签约通告';n.attachments=[{'name':'合格认租家庭选房排位名单.pdf'}]
    assert not review_files(n)


def test_formal_selection_uses_official_queue_without_rewriting_self_confirmation():
    from tests.housing.test_review_statistics import fixture
    from app.housing.opportunity import analyze_simple
    data,m=fixture();b=data['batches'][0]
    b['final_rows']={'TEST000003':{'rank':2,'ahead_same_band':1,'scope_verified':True,'queue':'优先组','published_at':'2026-11-03','url':'https://example.gov.cn/selection'}}
    values={'apply_no':'TEST000003','queue':'普通组','projects':['A'],'people':3,'children':1}
    r=analyze_simple(data,m,values)
    assert r['assumptions']['basis']=='official-final' and r['assumptions']['queue']=='优先组'
    assert values['queue']=='普通组' and any('不修改本人选择' in t for t in r['messages'])


def test_query_only_returns_own_queue_adjustment_and_keeps_review_status():
    from tests.housing.test_review_statistics import fixture
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token
    data,m=fixture();b=data['batches'][0]
    b['queue_updates']={'TEST000003':{'queue':'普通组','published_at':'2026-11-03','url':'https://example.gov.cn/update'},
                        'TEST000002':{'queue':'OTHER-PRIVATE-QUEUE-RECORD'}}
    b['final_notice']={'verified':True,'url':'https://example.gov.cn/selection'}
    secret='anonymous-queue-adjustment-test-key-123456789012'
    s=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://test.example')
    r=s.handle({'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret)},
        'body':json.dumps({'region':'test','apply_no':'TEST000003','queue':'普通组','version':'v'})})
    assert r['statusCode']==200 and 'OTHER-PRIVATE' not in r['body']
    batch=json.loads(r['body'])['payload']['batches'][0]
    assert set(batch['queue_updates'])=={'TEST000003'} and batch['review_rows']['TEST000003']['status']=='qualified'
    assert batch['selection_status']=='not_listed_in_verified_scope'


def test_pdf_queue_adjustment_header_continues_on_later_pages(monkeypatch):
    import pypdf
    from app.housing.qualification import parse_pdf
    pages=['认租编号 意向分类 最新队列分类\nTEST000001 建筑面积40㎡ 第三队列',
           'TEST000002 建筑面积50㎡ 第三队列']
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda t=t:t) for t in pages]))
    rows=parse_pdf(b'pdf','qualified')
    assert [r['queue'] for r in rows]==['第三队列','第三队列']
    assert [r['band'] for r in rows]==['40','50']


def test_query_accepts_explicitly_absent_final_notice():
    from tests.housing.test_review_statistics import fixture
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token
    data,m=fixture();b=data['batches'][0];b['final_notice']=None
    secret='anonymous-no-selection-test-key-123456789012'
    service=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://test.example')
    result=service.handle({'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret)},
        'body':json.dumps({'region':'test','apply_no':'TEST000003','queue':'普通组','version':'v'})})
    assert result['statusCode']==200
    assert json.loads(result['body'])['payload']['batches'][0]['selection_status']=='unverified'
