import asyncio
import copy
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.housing.qualification import (parse_pdf,parse_table,normalize_records,summarize_records,
    ingest_review,public_review,attach_reviews,declared_review_counts)
from app.housing.models import HousingNotice,HousingMeta,HousingAttachmentState
from app.housing.opportunity import analyze_simple,review_position


def notice(session,source):
    n=HousingNotice(source_id=source.id,canonical_url='https://example.gov.cn/review',canonical_key='review-test',url_hash='review-test',
        title='测试区第一批配租认租家庭资格核查结果公示',body_text='',category='process',is_target=True,
        batch_key_v2='v2:test',batch_link_status='manual',published_at=datetime(2026,10,28),
        first_seen_at=datetime(2026,10,28),business_version='review-business',
        attachments=[{'name':'合格认租家庭名单.pdf','url':'https://example.gov.cn/1.pdf','kind':'pdf'},
                     {'name':'不合格及撤销认租家庭名单.pdf','url':'https://example.gov.cn/2.pdf','kind':'pdf'},
                     {'name':'未纳入审核名单.pdf','url':'https://example.gov.cn/3.pdf','kind':'pdf'}])
    session.add(n);session.flush();return n


def records(status='qualified',queue=''):
    return [{'apply_no':'TEST000001','band':'50','status':status,'queue':queue,'order':4,'source':'https://example.gov.cn/1.pdf'}]


def test_pdf_wrapped_rows_and_mixed_negative_do_not_store_personal_fields(monkeypatch):
    import pypdf
    text=('合格认租家庭名单\n序号 认租编号 姓名 身份证 意向分类 原意向摇号顺序号\n'
          '1 TEST000001 匿名甲 123456******** 建筑面积50㎡左右的一房\n项目甲 4\n'
          '2 TEST000002 匿名乙 M02012495******** 建筑面积50㎡左右的一房 项目甲 8\n')
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:text)]))
    got=parse_pdf(b'pdf','qualified')
    assert [r['order'] for r in got]==[4,8]
    assert [r['band'] for r in got]==['50','50']
    assert '匿名' not in json.dumps(got,ensure_ascii=False) and '123456' not in json.dumps(got)
    text='不合格及撤销认租名单\n序号 认租编号 意向分类\n1 TEST000001 匿名 建筑面积50㎡左右的一房 4 未按规定提交申请材料\n2 TEST000002 匿名 建筑面积50㎡左右的一房 8 撤销认租\n'
    assert [r['status'] for r in parse_pdf(b'pdf','mixed_negative')]==['ineligible','withdrawn']


def test_excel_custom_band_queues_and_absent_order_are_not_inferred():
    table=[['认租编号','意向分类','队列分类','不合格原因'],
           ['TEST000001','两房','人才优先队列','撤销申请'],['TEST000002','两房','普通队列','材料不符']]
    got=parse_table(table,'mixed_negative')
    assert got[0]['band']=='两房' and got[0]['queue']=='人才优先队列' and got[0]['order'] is None
    assert got[0]['status']=='withdrawn' and got[1]['status']=='ineligible'
    with pytest.raises(ValueError):parse_table([['认租编号'],['BAD']], 'qualified')
    with pytest.raises(ValueError):parse_table([['认租编号'],['TEST000001']], 'mixed_negative')


def test_aggregate_withdrawal_and_unreviewed_denominators_and_conflicts():
    data=[]
    for s,n in [('qualified',344),('ineligible',99),('withdrawn',2),('not_reviewed',1081)]:
        data += [dict(records(s)[0],apply_no=f'TEST{s.upper()}{i:06}') for i in range(n)]
    value=summarize_records(normalize_records(data))['50']
    assert value['reviewed']==443 and value['pass_rate']=='77.7'
    assert not value['queue_complete'] and value['queues']=={}
    assert len(normalize_records([data[0],dict(data[0],source='https://mirror')]))==1
    with pytest.raises(ValueError):normalize_records([data[0],dict(data[0],status='withdrawn')])
    assert declared_review_counts('建筑面积65平方米左右的房源申请100户，审核80户；建筑面积120㎡及以上的房源审核10户。')=={'65':80,'120+':10}


def test_failed_attachment_keeps_verified_set_and_retries_without_business_replay(session,source,tmp_path,monkeypatch):
    import app.housing.qualification as q
    monkeypatch.setattr(q,'cache_root',lambda:tmp_path)
    n=notice(session,source)
    calls=[]
    class Client:
        failed=False
        async def get_bytes(self,url,**kwargs):
            calls.append(url)
            if self.failed and url.endswith('2.pdf'):raise RuntimeError('offline')
            return url.encode()
    c=Client()
    def parse(data,status,url,label):
        return [dict(records({'qualified':'qualified','mixed_negative':'ineligible','not_reviewed':'not_reviewed'}[status])[0],
                     apply_no='TEST00000'+url[-5],source=url)]
    monkeypatch.setattr(q,'parse_pdf',parse)
    original=(n.business_version,n.event_at)
    first=asyncio.run(ingest_review(session,c,n,force=True));session.commit()
    assert original==(n.business_version,n.event_at) and first['total']==3
    state=session.query(HousingAttachmentState).filter_by(notice_id=n.id,purpose='qualification').one()
    old_hashes=copy.deepcopy(state.content_hashes)
    c.failed=True
    again=asyncio.run(ingest_review(session,c,n,force=True));session.commit()
    assert again['version']==first['version'] and state.content_hashes==old_hashes
    assert state.status=='partial' and state.failures==1
    assert public_review(session,n.batch_key_v2)['pending']
    count=len(calls);asyncio.run(ingest_review(session,c,n));assert len(calls)==count
    c.failed=False;asyncio.run(ingest_review(session,c,n,force=True))
    assert state.status=='ok' and _count(session,n)==3


def _count(session,n):return session.get(HousingMeta,f'qualification:{n.id}').value['total']


def bundle():
    batch={'id':10,'batch_key':'v2:test','d':'2026-10-16','Q':['第一队列','第二队列','第三队列'],
           'c':[{'key':'50','l':'50㎡','u':2,'n':3,'q':{'第一队列':1,'第二队列':2},'issue':'','queue_complete':True}],
           'R':{'0|0':[1],'0|1':[4,8],'0|2':[]},
           'qualification':{'pending':False,'published_at':'2026-10-28','bands':[]},
           'review_rows':{'TEST000001':dict(records()[0],label='公示合格',published_at='2026-10-28',phase='publicity',url='https://example.gov.cn/review')},
           'opportunity':{'complete':True,'rule':{'verified':True,'minimum_people':{'50':2}},
                          'projects':[{'id':'A','name':'匿名公寓','rows':[{'band':'50','units':2}]}]}}
    return {'district':'测试区','batches':[batch],'rows':{'TEST000000':[[0,0,0,1,1,0,1]],'TEST000001':[[0,0,1,4,1,1,1]],'TEST000002':[[0,0,1,8,2,1,1]]}}


@pytest.mark.parametrize('status',['qualified','ineligible','withdrawn','not_reviewed','not_found','conflict'])
def test_latest_review_python_browser_parity_and_original_only_by_explicit_choice(status):
    data=bundle();match=data['rows']['TEST000001'][0];b=data['batches'][0]
    b['review_rows']['TEST000001']['status']=status
    values={'apply_no':'TEST000001','queue':'第二队列','people':3,'children':1,'projects':['A']}
    py=analyze_simple(data,match,values)
    assert py['scenario'] is None
    script=Path('app/housing/opportunity.js').read_text(encoding='utf-8')+ '\nconst x=JSON.parse(require("fs").readFileSync(0,"utf8"));process.stdout.write(JSON.stringify(Opportunity.analyzeSimple(x.data,x.match,x.values)));'
    js=json.loads(subprocess.run(['node','-e',script],input=json.dumps({'data':data,'match':match,'values':values}),capture_output=True,text=True,encoding='utf-8',check=True).stdout)
    assert py==js
    old=analyze_simple(data,match,dict(values,use_original_lottery=True))
    assert (old['scenario'] is not None)==(status=='qualified')
    if status=='qualified':assert '原摇号假设' in old['readable']['headline']


def test_complete_current_queues_exclude_negatives_once_and_keep_original_unchanged():
    data=bundle();b=data['batches'][0];original=copy.deepcopy(b['R'])
    b['review_rows']={no:dict(records()[0],status='qualified',queue='第二队列',published_at='2026-10-28') for no in data['rows']}
    b['review_rows']['TEST000000'].update(status='ineligible',queue='')
    match=data['rows']['TEST000001'][0]
    position=review_position(data,match,'TEST000001')
    assert position['verified'] and position['ahead']==0 and position['rank']==1
    result=analyze_simple(data,match,{'apply_no':'TEST000001','queue':'第二队列','projects':['A'],'people':3,'children':1})
    assert result['assumptions']['ahead']==0 and result['self_report']['queue']=='第二队列'
    clash=analyze_simple(data,match,{'apply_no':'TEST000001','queue':'第三队列','projects':['A'],'people':3,'children':1})
    assert clash['scenario'] is None and '队列与本次公开名单记载不一致' in clash['readable']['explanation']
    assert b['R']==original
    b['review_rows']['TEST000002']['status']='not_reviewed'
    assert not review_position(data,match,'TEST000001')['verified']


def test_private_service_only_returns_queried_review_record_and_preserves_browser_math():
    from app.housing.lookup_service import LookupService
    from app.housing.lookup_tokens import issue_token
    data=bundle();b=data['batches'][0]
    b['review_rows']['TEST000002']=dict(records('withdrawn')[0],apply_no='TEST000002')
    secret='anonymous-local-test-signing-secret-1234567890'
    server=LookupService({'test':data},{'version':'v','regions':{'test':{'queues':b['Q']}}},secret,'https://test.example')
    event={'httpMethod':'POST','path':'/query','headers':{'Authorization':'Bearer '+issue_token(secret)},
           'body':json.dumps({'version':'v','region':'test','queue':'第二队列','apply_no':'TEST000001'})}
    result=server.handle(event);assert result['statusCode']==200
    payload=json.loads(result['body'])['payload'];text=json.dumps(payload)
    assert 'TEST000002' not in text and 'TEST000000' not in text
    assert set(payload['batches'][0]['review_rows'])=={'TEST000001'}
    assert not payload['batches'][0]['access_context']['review_position']['verified']


def test_report_prefers_review_with_original_in_details():
    from app.housing.daily_render import Document,competition_tables
    doc=Document();review={'phase':'publicity','published_at':'2026-10-28','url':'https://example.gov.cn/review',
        'queue_note':'各队列合格户数：本次未公开','bands':[dict(summarize_records(normalize_records(records()))['50'],
        band='50',label='50㎡',initial_units=76,qualified_ratio='4.5')]}
    card=SimpleNamespace(qualification=review,lottery_breakdown=[{'label':'50㎡','category':'50','initial_units':76,
        'households':1526,'households_verified':True,'competition_ratio':'20.1','queue_complete':False,'published_at':'2026-10-16'}])
    competition_tables(doc,card)
    html=''.join(doc.html)
    assert html.index('最新资格核查')<html.index('申请阶段统计')<html.index('摇号房源竞争概览')
    assert '<details' in html and '<table' in html and '不合格' in html and '合格率' in html


def test_unreviewed_reason_uses_current_document_evidence_and_stays_with_tables():
    from app.housing.reporting import review_scope_note
    from app.housing.daily_render import Document,review_tables
    source=SimpleNamespace(title='本批资格核查公示',body_text=(
        '单身公寓类、一房类房源认租申请家庭，第一、第二队列申请人/家庭全部列入首批资格核查名单。'
        '若首轮选房结束后仍有可选房源，后续将从第三队列名单内，依照摇号顺序启动第二轮资格核查及选房工作。'))
    review={'phase':'publicity','published_at':'2026-10-28','url':'https://example.gov.cn/review','bands':[
        dict(summarize_records(normalize_records(records()))['50'],band='40',label='40㎡',initial_units=111,qualified_ratio='8.5',not_reviewed=2674),
        dict(summarize_records(normalize_records(records()))['50'],band='50',label='50㎡',initial_units=76,qualified_ratio='4.5',not_reviewed=1081)]}
    original=copy.deepcopy(review['bands']);review['display_scope_note']=review_scope_note(source,review)
    assert '90㎡、100㎡及以上三个队列均纳入首轮' in review['display_scope_note']
    assert '不能仅凭队列推断' in review['display_scope_note']
    assert '也不保证一定进入下一轮' in review['display_scope_note']
    doc=Document();doc.open('批次详情');review_tables(doc,review);doc.close()
    html=''.join(doc.html);text='\n'.join(doc.text)
    assert html.index('未纳入本轮审核')<html.index('为什么暂未审核')<html.index('本轮合格户／套')
    assert '不能视为整批最终竞争比' in html and '不能视为整批最终竞争比' in text
    assert '审核安排原文' in html and review['bands']==original
    assert html.count('overflow-wrap:normal')==3
    from app.housing.share_tool import prepare_document
    image_input=prepare_document(text)
    assert '不能仅凭队列推断' in image_input.text
    assert '不能视为整批最终竞争比' in image_input.text
    assert any(b.kind=='table' and '未纳入本轮审核' in b.table[0][0] for b in image_input.blocks)


def test_other_batch_and_later_round_do_not_inherit_first_round_reason():
    from app.housing.reporting import review_scope_note
    review={'bands':[{'band':'40','not_reviewed':12},{'band':'50','not_reviewed':20}]}
    for title,body in [('其他区核查公告','本轮核查范围另见附件。'),('第二轮核查公告','第三队列暂未全部审核')]:
        note=review_scope_note(SimpleNamespace(title=title,body_text=body),review)
        assert '具体原因尚未核实' in note and '先审核第一、第二队列' not in note
    assert review_scope_note(None,{'bands':[{'band':'40','not_reviewed':0}]})==''


def test_later_partial_round_updates_only_covered_people_and_failed_new_round_keeps_previous(session,source,tmp_path,monkeypatch):
    import app.housing.qualification as q
    monkeypatch.setattr(q,'cache_root',lambda:tmp_path)
    first=notice(session,source)
    second=HousingNotice(source_id=source.id,url_hash='round-two',canonical_key='round-two',canonical_url='https://example.gov.cn/two',
        title='第二轮资格核查结果公示',category='process',batch_key_v2=first.batch_key_v2,published_at=datetime(2026,10,30),
        attachments=[{'name':'合格名单.pdf','url':'https://example.gov.cn/two.pdf','kind':'pdf'}])
    session.add(second);session.flush()
    def install(n,rows):
        counts=summarize_records(rows)
        meta={'notice_id':n.id,'batch_key':n.batch_key_v2,'version':str(n.id),'published_at':n.published_at.isoformat(),
              'phase':'publicity','url':n.canonical_url,'counts':counts,'private_file':str(n.id)+'.json','total':len(rows),
              'disclosed_statuses':['qualified']}
        session.add(HousingMeta(key=f'qualification:{n.id}',value=meta));session.flush()
        (tmp_path/meta['private_file']).write_text(json.dumps({'records':rows}),encoding='utf-8')
    data=bundle()
    install(first,{no:dict(records('not_reviewed')[0],apply_no=no,order=matches[0][3]) for no,matches in data['rows'].items()})
    install(second,{'TEST000001':dict(records()[0],queue='第三队列')})
    attach_reviews(session,data)
    assert data['batches'][0]['review_rows']['TEST000001']['status']=='qualified'
    assert data['batches'][0]['review_rows']['TEST000000']['status']=='not_reviewed'
    # Missing disclosure never becomes a made-up zero/100% pass rate.
    public=public_review(session,first.batch_key_v2)
    assert public['bands'][0]['ineligible'] is None and public['bands'][0]['pass_rate'] is None
    newer=HousingNotice(source_id=source.id,url_hash='round-three',canonical_key='round-three',canonical_url='https://example.gov.cn/three',
        title='终审核查合格名单',category='process',batch_key_v2=first.batch_key_v2,published_at=datetime(2026,11,1),
        attachments=[{'name':'合格名单.pdf','url':'https://example.gov.cn/three.pdf'}])
    session.add(newer);session.flush()
    public=public_review(session,first.batch_key_v2)
    assert public['pending'] and public['counts'] and public['pending_notice']['notice_id']==newer.id
    historical=public_review(session,first.batch_key_v2,at=datetime(2026,10,29))
    assert historical['notice_id']==first.id


def test_lookup_version_changes_on_review_and_final_dependencies_without_lottery_change(session,source):
    from app.housing.lookup import source_version
    lottery=notice(session,source);lottery.category='lottery_result';lottery.attachments=[];session.flush()
    v1=source_version(session,lottery)
    review=HousingNotice(source_id=source.id,url_hash='dependency',canonical_key='dependency',canonical_url='https://example.gov.cn/new',
        title='资格核查结果',category='process',batch_key_v2=lottery.batch_key_v2,business_version='r1')
    session.add(review);session.flush()
    v2=source_version(session,lottery);assert v2!=v1
    session.add(HousingMeta(key=f'qualification:{review.id}',value={'version':'parsed-1'}));session.flush()
    v3=source_version(session,lottery);assert v3!=v2
    review.business_version='official-final-version';session.flush()
    assert source_version(session,lottery)!=v3
    assert source_version(session,lottery,include_related=False)==v1


def test_parser_repairs_do_not_replay_but_verified_official_changes_create_one_revision(session,source,tmp_path,monkeypatch):
    import app.housing.qualification as q
    monkeypatch.setattr(q,'cache_root',lambda:tmp_path)
    n=notice(session,source);n.attachments=n.attachments[:1]
    class Client:
        version=1
        async def get_bytes(self,url,**kw):return str(self.version).encode()
    client=Client()
    def parse(data,status,url,label):
        return [dict(records()[0],queue='第一队列' if data==b'1' else '第二队列')]
    monkeypatch.setattr(q,'parse_pdf',parse)
    first=asyncio.run(ingest_review(session,client,n,force=True));session.commit()
    initial=n.business_version
    monkeypatch.setattr(q,'PARSER_VERSION','qualification-test-repair')
    client.version=2
    repaired=asyncio.run(ingest_review(session,client,n,force=True));session.commit()
    assert repaired['version']!=first['version'] and n.business_version==initial
    client.version=1
    asyncio.run(ingest_review(session,client,n,force=True));session.commit()
    revision=session.get(HousingMeta,f'notice-revisions:{n.id}')
    assert n.business_version!=initial and len(revision.value['revisions'])==1
    assert revision.value['revisions'][0]['reason']=='official-qualification-changed'
    asyncio.run(ingest_review(session,client,n,force=True));session.commit()
    assert len(revision.value['revisions'])==1


def test_recovery_after_failed_new_parser_is_still_a_repair_not_official_change(session,source,tmp_path,monkeypatch):
    import app.housing.qualification as q
    monkeypatch.setattr(q,'cache_root',lambda:tmp_path)
    n=notice(session,source);n.attachments=n.attachments[:1]
    class Client:
        async def get_bytes(self,url,**kw):return b'same-official-file'
    monkeypatch.setattr(q,'parse_pdf',lambda data,status,url,label:records())
    asyncio.run(ingest_review(session,Client(),n,force=True));session.commit()
    record=session.get(HousingMeta,f'qualification:{n.id}')
    record.value=dict(record.value,parser_version='previous-parser',version='old-derived-version')
    state=session.query(HousingAttachmentState).filter_by(notice_id=n.id,purpose='qualification').one()
    state.status='partial';state.parser_version=q.PARSER_VERSION
    original=(n.business_version,n.event_at,n.attachment_version);session.commit()
    asyncio.run(ingest_review(session,Client(),n,force=True));session.commit()
    assert original==(n.business_version,n.event_at,n.attachment_version)
    assert record.value['parser_version']==q.PARSER_VERSION and state.status=='ok'
