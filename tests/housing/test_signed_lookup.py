import base64
import copy
import hashlib
import hmac
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.housing.lookup_tokens import AccessError, BEIJING, issue_token, verify_token
from app.housing.lookup_service import LookupService
from app.housing.opportunity import query_summary, analyze_simple

SECRET='local-test-secret-never-used-in-production-123456789'
NOW=datetime(2026,10,26,15,0,tzinfo=BEIJING)


def fixture():
    batch={'id':1,'t':'匿名测试批次','d':'2026-10-16','Q':['第一队列','第二队列','第三队列'],
           'c':[{'key':'50','l':'50㎡左右','u':2,'n':5,'q':{'第一队列':1,'第二队列':2,'第三队列':2},'issue':'','queue_complete':True}],
           'R':{'0|0':[1],'0|1':[2,4],'0|2':[3,5]},'u':'https://example.gov.cn/test',
           'opportunity':{'complete':True,'rule':{'verified':True,'minimum_people':{'50':2}},
                          'projects':[{'id':'one','name':'匿名小区','rows':[{'band':'50','units':2}]}]},
           'final_rows':{'TEST000001':{'rank':3,'url':'https://example.gov.cn/final','ahead_same_band':2,'scope_verified':True},
                         'TEST000002':{'rank':4,'url':'https://example.gov.cn/final'}}}
    return {'district':'测试区','batches':[batch],'rows':{'TEST000001':[[0,0,1,4,2,1,1]],'TEST000002':[[0,0,2,5,2,2,1]]}}


def service(clock=lambda:NOW):
    return LookupService({'test':fixture()},{'version':'test-v1','regions':{'test':{'district':'测试区','file':'test.html','queues':['第一队列','第二队列','第三队列']}}},SECRET,'https://lookup.example',clock)


def event(path='/query',token=None,**body):
    return {'httpMethod':'POST','path':path,'headers':{'Authorization':'Bearer '+(token or issue_token(SECRET,NOW)),
            'Origin':'https://lookup.example'},'body':json.dumps({'region':'test','apply_no':'TEST000001','queue':'第三队列','version':'test-v1',**body})}


@pytest.mark.parametrize('day',['2026-10-26','2026-10-31','2026-12-31','2028-02-29'])
def test_beijing_midnight_boundaries_and_utc_conversion(day):
    start=datetime.fromisoformat(day).replace(tzinfo=BEIJING)
    token=issue_token(SECRET,start+timedelta(hours=23,minutes=59))
    assert verify_token(token,SECRET,start)['nbf']==int(start.timestamp())
    assert verify_token(token,SECRET,(start+timedelta(days=1,microseconds=-1)).astimezone(timezone.utc))
    with pytest.raises(AccessError,match='已过期'):verify_token(token,SECRET,start+timedelta(days=1))
    with pytest.raises(AccessError,match='尚未生效'):verify_token(token,SECRET,start-timedelta(microseconds=1))


def test_forged_dates_signature_and_wrong_purpose_are_rejected():
    token=issue_token(SECRET,NOW);prefix,payload,signature=token.split('.')
    claims=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
    claims['exp']+=86400
    encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    with pytest.raises(AccessError):verify_token(prefix+'.'+encoded+'.'+signature,SECRET,NOW)
    wrong=hmac.new(SECRET.encode(),('v1.'+encoded).encode(),hashlib.sha256).digest()
    wrong=base64.urlsafe_b64encode(wrong).decode().rstrip('=')
    with pytest.raises(AccessError):verify_token('v1.'+encoded+'.'+wrong,SECRET,NOW)
    for invalid in ['', '20261026','v1.x.x',token+'=',token.replace('v1.','v2.',1)]:
        with pytest.raises(AccessError):verify_token(invalid,SECRET,NOW)
    with pytest.raises(ValueError,match='密钥'):issue_token('',NOW)


def test_session_and_every_query_require_bearer_and_server_time():
    clock=[NOW];server=service(lambda:clock[0]);request=event()
    assert server.handle(request)['statusCode']==200
    request['headers']={};request['queryStringParameters']={'d':'20261026','t':issue_token(SECRET,NOW)}
    assert server.handle(request)['statusCode']==401
    request=event('/session');result=server.handle(request)
    assert result['headers']['Cache-Control']=='no-store' and json.loads(result['body'])['server_time']==NOW.timestamp()
    clock[0]=NOW.replace(hour=0)+timedelta(days=1)
    assert server.handle(event())['statusCode']==401
    assert server.handle(event('/session'))['statusCode']==401


def test_query_returns_only_one_number_no_roster_order_arrays_or_other_final_rows():
    response=service().handle(event());assert response['statusCode']==200
    result=json.loads(response['body'])['payload'];text=json.dumps(result)
    assert set(result['rows'])=={'TEST000001'} and 'TEST000002' not in text
    assert 'R' not in result['batches'][0]
    expected=query_summary(fixture(),fixture()['rows']['TEST000001'][0],'第三队列')
    assert result['batches'][0]['access_context']['summary']==expected
    assert fixture()['batches'][0]['R'] # No source mutation.


@pytest.mark.parametrize('change',[{'queue':'不存在'},{'region':'../data'},{'apply_no':'<script>'},{'apply_no':[]},{'unexpected':'secret'}])
def test_bad_query_input_is_rejected(change):
    assert service().handle(event(**change))['statusCode']==400


def test_version_changes_and_wrong_origins_never_return_data():
    assert service().handle(event(version='old'))['statusCode']==409
    request=event();request['headers']['Origin']='https://evil.example'
    assert service().handle(request)['statusCode']==403
    request=event('/data.json');assert service().handle(request)['statusCode']==404


def test_domain_migration_keeps_both_exact_origins_and_requires_daily_token():
    server=service()
    server=LookupService(server.data,server.metadata,SECRET,
        'https://lookup.example, https://new.example',lambda:NOW)
    for origin in ['https://lookup.example','https://new.example']:
        request=event();request['headers']['Origin']=origin
        result=server.handle(request)
        assert result['statusCode']==200
        assert result['headers']['Access-Control-Allow-Origin']==origin
        request['headers'].pop('Authorization')
        assert server.handle(request)['statusCode']==401
        request=event(token=issue_token(SECRET,NOW-timedelta(days=1)))
        request['headers']['Origin']=origin
        assert server.handle(request)['statusCode']==401
    for origin in ['https://new.example.evil.test','http://new.example','null','*']:
        request=event();request['headers']['Origin']=origin
        result=server.handle(request)
        assert result['statusCode']==403
        assert 'Access-Control-Allow-Origin' not in result['headers']


@pytest.mark.parametrize('queue',['第一队列','第二队列','第三队列'])
def test_private_python_and_reduced_browser_analysis_are_identical(queue):
    payload=json.loads(service().handle(event(queue=queue))['body'])['payload']
    match=fixture()['rows']['TEST000001'][0]
    values={'apply_no':'TEST000001','queue':queue,'projects':['one'],'people':3,'children':1}
    expected=analyze_simple(fixture(),match,values)
    js=(Path('app/housing/opportunity.js').read_text(encoding='utf-8')+
        '\nconst x=JSON.parse(require("fs").readFileSync(0,"utf8"));process.stdout.write(JSON.stringify({summary:Opportunity.querySummary(x.data,x.match,x.values.queue),analysis:Opportunity.analyzeSimple(x.data,x.match,x.values)}));')
    result=subprocess.run(['node','-e',js],input=json.dumps({'data':payload,'match':payload['rows']['TEST000001'][0],'values':values}),capture_output=True,text=True,encoding='utf-8',check=True)
    result=json.loads(result.stdout)
    assert result['summary']==query_summary(fixture(),match,queue)
    assert result['analysis']==expected


def test_public_build_has_no_embedded_records_and_private_zip_has_no_secret(tmp_path):
    from app.housing.lookup_secure_build import build_release
    from tools.build_lottery_lookup import _render_html
    source=tmp_path/'source';source.mkdir();text=_render_html(fixture())
    (source/'test.html').write_text(text,encoding='utf-8')
    (source/'manifest.json').write_text(json.dumps({'version':'old','pages':{'test.html':{'sha256':hashlib.sha256((source/'test.html').read_bytes()).hexdigest(),'notices':{}}}}),encoding='utf-8')
    out=tmp_path/'release';info=build_release(source,out,'https://api.example/lookup','https://lookup.example')
    for p in (out/'static').iterdir():
        contents=p.read_text(encoding='utf-8')
        assert 'TEST000001' not in contents and 'TEST000002' not in contents and SECRET not in contents
        assert 'VALID_DAYS = 7' not in contents and 'const DATA = {"batches":[],"rows":{}}' in contents if p.suffix=='.html' and p.name!='index.html' else True
    assert info['version'] and (out/'cloud-function.zip').exists()
    assert SECRET not in (out/'function/index.py').read_text(encoding='utf-8')


def test_signed_link_preserves_unrelated_params_and_has_no_legacy_date(monkeypatch):
    from app.housing.config import get_housing_settings
    from app.housing.render import lookup_url_for
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setenv('HOUSING_LOOKUP_ACCESS_MODE','signed');monkeypatch.setenv('HOUSING_LOOKUP_SIGNING_SECRET',SECRET)
    monkeypatch.setenv('HOUSING_LOOKUP_URL','https://lookup.example/index.html?from=push&d=20260101&t=old')
    get_housing_settings.cache_clear()
    try:
        params=parse_qs(urlsplit(lookup_url_for(NOW)).query)
        assert set(params)=={'from','t'} and params['from']==['push']
        assert verify_token(params['t'][0],SECRET,NOW)
        monkeypatch.setenv('HOUSING_LOOKUP_SIGNING_SECRET','');get_housing_settings.cache_clear()
        with pytest.raises(ValueError,match='密钥'):lookup_url_for(NOW)
    finally:get_housing_settings.cache_clear()


def test_release_checks_new_domain_before_changing_active_push_configuration(monkeypatch):
    from tools.secure_lookup import release_lookup_url
    from app.housing.config import get_housing_settings
    monkeypatch.setenv('HOUSING_LOOKUP_URL','https://old.example/hzktest/index.html')
    get_housing_settings.cache_clear()
    try:
        assert release_lookup_url({'site_origin':'https://new.example'})=='https://new.example/hzktest/index.html'
        assert get_housing_settings().lookup_url=='https://old.example/hzktest/index.html'
    finally:get_housing_settings.cache_clear()


def test_http_runtime_adapter_rejects_unauthed_bulk_and_serves_minimal_query(tmp_path,monkeypatch):
    import threading
    import httpx
    from app.housing.lookup_http import create_server
    monkeypatch.setenv('HOUSING_LOOKUP_SIGNING_SECRET',SECRET)
    monkeypatch.setenv('HOUSING_LOOKUP_SITE_ORIGIN','https://lookup.example')
    (tmp_path/'data.json').write_text(json.dumps({'test':fixture()}),encoding='utf-8')
    (tmp_path/'metadata.json').write_text(json.dumps(service().metadata),encoding='utf-8')
    server=create_server(tmp_path,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base=f'http://127.0.0.1:{server.server_port}'
        with httpx.Client(trust_env=False) as client:
            assert client.post(base+'/session',json={}).status_code==401
            assert client.get(base+'/data.json').status_code==401
            headers={'Authorization':'Bearer '+issue_token(SECRET)}
            result=client.post(base+'/query',headers=headers,json={'region':'test','apply_no':'TEST000001','queue':'第三队列','version':'test-v1'})
            assert result.status_code==200 and result.headers['Cache-Control']=='no-store'
            # CloudBase gateway adds CORS; this adapter must not duplicate it.
            assert 'Access-Control-Allow-Origin' not in result.headers
            assert set(result.json()['payload']['rows'])=={'TEST000001'}
            assert client.get(base+'/data.json',headers=headers).status_code==405
            assert client.post(base+'/session',headers={**headers,'Origin':'https://wrong.example'},json={}).status_code==403
    finally:server.shutdown();server.server_close()


def test_signed_mode_builds_in_private_directory_and_legacy_receipt_is_not_ready(monkeypatch,session):
    from app.housing.config import get_housing_settings
    from app.housing.lookup import build_directory,published_ready,mark_published
    from app.config import PROJECT_ROOT
    from app.housing.models import HousingMeta
    monkeypatch.setenv('HOUSING_LOOKUP_ACCESS_MODE','signed');get_housing_settings.cache_clear()
    try:
        assert build_directory()==PROJECT_ROOT/'data/lookup-source'
        assert build_directory(PROJECT_ROOT/'lookup')==PROJECT_ROOT/'data/lookup-source'
        session.add(HousingMeta(key='lookup-published',value={'version':'legacy','notices':{'1':'old'}}));session.flush()
        assert not published_ready(session=session)
        with pytest.raises(ValueError,match='activate'):mark_published(session,'legacy')
    finally:get_housing_settings.cache_clear()
