"""Prepare, verify and activate the signed CloudBase lookup release.

Never uploads automatically or logs bearer tokens/secrets. Verification precedes activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def ensure_key():
    from app.housing.config import get_housing_settings
    from app.config import PROJECT_ROOT
    path=PROJECT_ROOT/'.env'
    value=get_housing_settings().lookup_signing_secret.get_secret_value()
    if value:
        if len(value.encode())<32:raise ValueError('已配置密钥过短，请更正；未覆盖现有密钥')
        return {'status':'already_configured'}
    value=secrets.token_urlsafe(48)
    with path.open('a',encoding='utf-8') as stream:
        stream.write('\n# Signed lookup: do not copy this secret into static pages or ZIP files.\nHOUSING_LOOKUP_SIGNING_SECRET='+value+'\n')
    get_housing_settings.cache_clear()
    return {'status':'key_configured','secret_printed':False}


def _release(path):
    folder=Path(path)
    release=json.loads((folder/'release.json').read_text(encoding='utf-8'))
    for directory, key in [('static','static_sha256'),('function','function_sha256')]:
        for name,digest in release[key].items():
            if Path(name).name!=name or hashlib.sha256((folder/directory/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('发布包文件已变化，请重新构建')
    return folder,release


def release_lookup_url(release):
    """Verify the candidate domain before changing the active push URL."""
    from app.housing.config import get_housing_settings
    current=urlsplit(get_housing_settings().lookup_url)
    origin=urlsplit(release['site_origin'])
    if current.scheme!='https' or not current.path or origin.scheme!='https' or not origin.netloc:
        raise ValueError('查询入口及发布域名必须配置为HTTPS')
    return urlunsplit((origin.scheme,origin.netloc,current.path,current.query,current.fragment))


def verify_release(path):
    import httpx
    from app.housing.config import get_housing_settings
    from app.housing.lookup_tokens import issue_token,beijing_now
    from datetime import timedelta
    folder,release=_release(path)
    settings=get_housing_settings();secret=settings.lookup_signing_secret.get_secret_value()
    token=issue_token(secret)
    base=release_lookup_url(release)
    if urlsplit(base).scheme!='https' or not base:raise ValueError('查询入口未配置为HTTPS')
    base=base.rsplit('/',1)[0]+'/'
    headers={'Authorization':'Bearer '+token,'Origin':release['site_origin']}
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        for name,digest in release['static_sha256'].items():
            response=client.get(base+name,headers={'Cache-Control':'no-cache'})
            if response.status_code!=200 or hashlib.sha256(response.content).hexdigest()!=digest:
                raise ValueError('线上页面尚未全部替换为此版本；未启用签名入口')
        response=client.post(release['api']+'/session',headers=headers,json={})
        if response.status_code!=200 or response.json().get('version')!=release['version']:
            raise ValueError('云端版本或签名密钥不一致；未启用签名入口')
        if 'no-store' not in response.headers.get('cache-control','').lower():raise ValueError('云端响应缓存配置不正确')
        def check_cors(res):
            values=res.headers.get_list('access-control-allow-origin')
            if values!=[release['site_origin']]:raise ValueError('云端跨域响应头重复或不匹配，浏览器无法查询；未启用')
        check_cors(response)
        preflight=client.options(release['api']+'/session',headers={'Origin':release['site_origin'],
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'})
        if preflight.status_code not in (200,204):raise ValueError('浏览器跨域预检未通过')
        check_cors(preflight)
        for invalid in ['',token[:-1]+('a' if token[-1]!='a' else 'b'),issue_token(secret,beijing_now()-timedelta(days=1))]:
            invalid_headers={'Origin':release['site_origin']}
            if invalid:invalid_headers['Authorization']='Bearer '+invalid
            res=client.post(release['api']+'/session',headers=invalid_headers,json={})
            if res.status_code!=401:raise ValueError('云端无凭证/篡改/过期验证未通过')
        # Compare a real record from every region to the local full source, without logging identifiers.
        from app.housing.lookup_service import LookupService
        data=json.loads((folder/'function/data.json').read_text(encoding='utf-8'))
        metadata=json.loads((folder/'function/metadata.json').read_text(encoding='utf-8'))
        local=LookupService(data,metadata,secret,release['site_origin'])
        for slug,payload in data.items():
            number=next(iter(payload['rows']));match=payload['rows'][number][0];queue=payload['batches'][match[0]]['Q'][match[2]]
            body={'region':slug,'apply_no':number,'queue':queue,'version':release['version']}
            res=client.post(release['api']+'/query',headers=headers,json=body)
            expected=json.loads(local.handle({'httpMethod':'POST','path':'/query','headers':headers,'body':json.dumps(body)})['body'])
            if res.status_code!=200 or res.json().get('payload')!=expected['payload']:raise ValueError('线上真实编号查询与发布包不一致')
    receipt={'version':release['version'],'verified_at':beijing_now().isoformat(),'regions_verified':len(release['regions']),
             'static_files_verified':len(release['static_sha256']),'access_tests_passed':True,'browser_cors_preflight_verified':True}
    (folder/'online-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    return receipt


def activate(path):
    """Run live verification again; only then replace local public shells and receipt."""
    verified=verify_release(path)
    folder,release=_release(path)
    from app.config import PROJECT_ROOT
    from app.db import SessionLocal
    from app.housing.config import get_housing_settings
    from app.housing.models import HousingMeta
    from app.housing.lookup import source_version
    from app.housing.models import HousingNotice
    from app.housing.lock import process_lock
    private=PROJECT_ROOT/'data/lookup-source';public=PROJECT_ROOT/'lookup'
    with process_lock(PROJECT_ROOT/'data/housing.send.lock'),process_lock(PROJECT_ROOT/'data/housing.lookup.lock'),SessionLocal() as session:
        for key,version in release['notices'].items():
            notice=session.get(HousingNotice,int(key))
            if notice is None or source_version(session,notice)!=version:raise ValueError('来源版本已变化，请重新构建；未切换')
        if not private.exists():shutil.copytree(public,private)
        env=PROJECT_ROOT/'.env'
        previous_env=env.read_bytes()
        previous_pages={p.name:p.read_bytes() for p in public.iterdir() if p.is_file()}
        try:
            # Existing public rosters are overwritten only after complete online validation.
            for page in (folder/'static').iterdir():shutil.copy2(page,public/page.name)
            candidate=env.with_name('.env.lookup.tmp')
            candidate.write_bytes(previous_env+('\nHOUSING_LOOKUP_ACCESS_MODE=signed\nHOUSING_LOOKUP_API_URL='+release['api']+'\nHOUSING_LOOKUP_URL='+release_lookup_url(release)+'\n').encode('utf-8'))
            candidate.replace(env)
            record=session.get(HousingMeta,'lookup-published')
            if record is None:record=HousingMeta(key='lookup-published');session.add(record)
            record.value={'schema':'signed-lookup-v1','version':release['version'],'source_version':release['source_version'],
                          'published_at':verified['verified_at'],'notices':release['notices'],'api':release['api']}
            session.commit()
        except Exception:
            session.rollback();env.write_bytes(previous_env)
            for name,contents in previous_pages.items():(public/name).write_bytes(contents)
            raise
    get_housing_settings.cache_clear()
    return {'status':'activated','version':release['version'],'scheduled_keys_changed':False}


def serve(path,port):
    """Loopback-only packaged gateway for local acceptance. Never exposes bulk files."""
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from app.housing.lookup_service import LookupService
    from app.housing.config import get_housing_settings
    folder,release=_release(path)
    service=LookupService(json.loads((folder/'function/data.json').read_text(encoding='utf-8')),
        json.loads((folder/'function/metadata.json').read_text(encoding='utf-8')),
        get_housing_settings().lookup_signing_secret.get_secret_value(),release['site_origin'])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            name=self.path.split('?',1)[0].lstrip('/') or 'index.html'
            if Path(name).name!=name or not name.endswith('.html') or not (folder/'static'/name).exists():self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers()
            self.wfile.write((folder/'static'/name).read_bytes())
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'))
            if length>2048:self.send_error(413);return
            result=service.handle({'path':self.path,'httpMethod':'POST','headers':dict(self.headers),'body':self.rfile.read(length).decode('utf-8')})
            self.send_response(result['statusCode'])
            for key,value in result['headers'].items():self.send_header(key,value)
            self.end_headers();self.wfile.write(result['body'].encode('utf-8'))
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


def main():
    parser=argparse.ArgumentParser(description='查询链接当天有效：准备、核对及启用')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('key')
    build=sub.add_parser('build');build.add_argument('--source');build.add_argument('--output',required=True);build.add_argument('--api',required=True);build.add_argument('--origin',required=True)
    for name in ['verify','activate']:
        item=sub.add_parser(name);item.add_argument('--release',required=True)
    item=sub.add_parser('serve');item.add_argument('--release',required=True);item.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    if args.command=='key':result=ensure_key()
    elif args.command=='build':
        from app.housing.lookup_secure_build import build_release
        from app.housing.lookup import build_directory
        result=build_release(build_directory(args.source),args.output,args.api,args.origin)
        result={'status':'built','version':result['version'],'regions':len(result['regions']),'publication':'待部署并线上核对，未启用'}
    elif args.command=='verify':result=verify_release(args.release)
    elif args.command=='activate':result=activate(args.release)
    else:return serve(args.release,args.port)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
