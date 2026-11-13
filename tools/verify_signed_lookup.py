"""Real-data local HTTP + owned headless Chrome acceptance; no cloud/group writes."""
from __future__ import annotations
import argparse
import base64
import json
import subprocess
import tempfile
import threading
import time
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

import httpx
import websocket

from app.housing.lookup_secure_build import secure_page
from app.housing.lookup_service import LookupService
from app.housing.lookup_tokens import issue_token,BEIJING


def verify(release,output):
    release=Path(release);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    data=json.loads((release/'function/data.json').read_text(encoding='utf-8'))
    metadata=json.loads((release/'function/metadata.json').read_text(encoding='utf-8'))
    key='local-headless-acceptance-only-never-production-123456'
    stamp=[datetime(2026,10,26,15,tzinfo=BEIJING)];requested=[]
    service=LookupService(data,metadata,key,'',lambda:stamp[0])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            name=self.path.split('?',1)[0].strip('/') or 'index.html'
            region='' if name=='index.html' else name.removesuffix('.html')
            if region and region not in data:self.send_error(404);return
            text=secure_page(region,origin+'/housing-lookup',metadata['version'],metadata['regions'])
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(text.encode())
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
            requested.append({'path':self.path,'body_keys':sorted(json.loads(body))})
            result=service.handle({'httpMethod':'POST','path':self.path,'headers':dict(self.headers),'body':body})
            self.send_response(result['statusCode'])
            for k,v in result['headers'].items():self.send_header(k,v)
            self.end_headers();self.wfile.write(result['body'].encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);origin=f'http://127.0.0.1:{server.server_port}';service.origin=origin;service.origins=frozenset([origin])
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    checks=[];ws=None;proc=None
    try:
        token=issue_token(key,stamp[0]);headers={'Authorization':'Bearer '+token,'Origin':origin}
        with httpx.Client(trust_env=False) as client:
            assert client.post(origin+'/housing-lookup/session',json={}).status_code==401
            for slug,payload in data.items():
                no=next(iter(payload['rows']));match=payload['rows'][no][0];queue=payload['batches'][match[0]]['Q'][match[2]]
                result=client.post(origin+'/housing-lookup/query',headers=headers,json={'region':slug,'apply_no':no,'queue':queue,'version':metadata['version']})
                assert result.status_code==200,(slug,result.status_code)
                limited=result.json()['payload'];assert set(limited['rows'])=={no}
                assert all('R' not in b and set(b.get('final_rows',{}))<={no} for b in limited['batches'])
            checks.append('all_published_regions_real_record_minimal_response')
        with tempfile.TemporaryDirectory(prefix='housing-signed-chrome-',ignore_cleanup_errors=True) as temp:
            profile=Path(temp)
            proc=subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless=new','--disable-gpu','--no-first-run','--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.monotonic()+20
            while not (profile/'DevToolsActivePort').exists():
                if time.monotonic()>deadline:raise RuntimeError('Chrome startup timeout')
                time.sleep(.05)
            port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
            with httpx.Client(trust_env=False) as client:tab=next(t for t in client.get(f'http://127.0.0.1:{port}/json/list').json() if t['type']=='page')
            ws=websocket.create_connection(tab['webSocketDebuggerUrl'],suppress_origin=True,timeout=30);seq=0
            def call(method,params=None):
                nonlocal seq
                seq+=1;ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    message=json.loads(ws.recv())
                    if message.get('id')!=seq:continue
                    if 'error' in message:raise RuntimeError(str(message['error']))
                    return message.get('result',{})
            def evaluate(text):
                result=call('Runtime.evaluate',{'expression':text,'returnByValue':True,'awaitPromise':True})
                if result.get('exceptionDetails'):raise RuntimeError(str(result['exceptionDetails']))
                return result.get('result',{}).get('value')
            def wait_for(expression):
                limit=time.monotonic()+12
                while not evaluate(expression):
                    if time.monotonic()>limit:raise RuntimeError('browser condition timeout: '+expression)
                    time.sleep(.03)
            call('Page.enable');call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            call('Page.addScriptToEvaluateOnNewDocument',{'source':"const RealDate=Date;window.Date=class extends RealDate{constructor(...args){super(...(args.length?args:['2035-01-01T00:00:00Z']));}static now(){return 2051222400000;}};"})
            call('Page.navigate',{'url':origin+'/index.html?d=20261026'})
            wait_for("document.getElementById('gate')?.textContent.includes('入口已升级')")
            assert evaluate("document.getElementById('app').hidden")
            checks.append('legacy_d_cannot_upgrade_to_token')
            call('Page.navigate',{'url':origin+'/index.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            assert evaluate(f"document.querySelectorAll('a[data-region]').length==={len(data)}&&Array.from(document.querySelectorAll('a[data-region]')).every(a=>a.href.includes('?t='))")
            checks.append('valid_token_index_and_cross_region_phone_time_2035')
            call('Page.navigate',{'url':origin+'/guangming.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            evaluate("$('q').value='SQ2026BZ360002688';$('q-queue').value='第三队列';run()")
            wait_for("document.getElementById('out').textContent.includes('391')")
            assert evaluate("$('out').textContent.includes('50㎡')&&$('out').textContent.includes('547')&&$('out').textContent.includes('76套')")
            checks.append('real_50sqm_third_queue_391_current_complete_roster')
            if any(b.get('qualification') for b in data['guangming']['batches']):
                assert evaluate("$('out').textContent.includes('公示合格')&&$('out').textContent.includes('77.7%')&&$('out').textContent.includes('4.5户／套')&&$('out').textContent.includes('本次名单未公开')")
                assert evaluate("Array.from($('out').querySelectorAll('details')).some(d=>d.querySelector('summary')?.textContent.includes('原摇号')&&!d.open)")
                checks.append('latest_review_first_and_original_lottery_collapsed')
                if evaluate("DATA.batches.some(b=>b.access_context?.qualified_reference?.available)"):
                    assert evaluate("$('out').textContent.includes('118户')&&$('out').textContent.includes('第 119 个')&&$('out').textContent.includes('不是当前梯队')")
                    checks.append('qualified_original_order_119_does_not_claim_current_queue_rank')
            evaluate("document.querySelector('input[name=project-0]').checked=true;$('people-0').value='3';$('children-0').value='1';runAnalysis(0)")
            wait_for("document.getElementById('scenario-0').textContent.includes('分析结果')")
            has_formal=evaluate("DATA.batches.some(b=>b.final_rows?.['SQ2026BZ360002688']?.scope_verified)")
            if has_formal:
                assert evaluate("$('out').textContent.includes('官方正式选房排序')&&$('out').textContent.includes('169')&&$('out').textContent.includes('最新选房名单记载队列')")
                checks.append('new_official_selection_rank_169_and_queue_separated')
            if any(b.get('qualification') for b in data['guangming']['batches']) and not has_formal:
                assert evaluate("$('scenario-0').textContent.includes('最新完整队列')&&!$('scenario-0').textContent.includes('暂时排不到你')")
                checks.append('missing_review_queues_do_not_invent_current_chance')
                if evaluate("!!$('review-filter-0')"):
                    evaluate("$('review-filter-0').checked=true;runAnalysis(0)")
                    assert evaluate("$('scenario-0').textContent.includes('原参考前序834户')&&$('scenario-0').textContent.includes('暂剔除101户')&&$('scenario-0').textContent.includes('733户')&&$('scenario-0').textContent.includes('390户未审核')&&$('scenario-0').textContent.includes('658户')")
                    assert evaluate("$('scenario-0').textContent.includes('344户至少申报')")
                    checks.append('filtered_scenario_733_keeps_390_unreviewed_and_union_344')
                    for width in (320,375,390):
                        call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':True})
                        evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
                        assert evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        evaluate("Array.from(document.querySelectorAll('h3')).find(e=>e.textContent.includes('本轮合格名单的原顺序参考'))?.scrollIntoView()")
                        shot=call('Page.captureScreenshot',{'format':'png'});(output/f'city-query-{width}.png').write_bytes(base64.b64decode(shot['data']))
                    checks.append('current_query_tables_and_controls_fit_320_375_390')
            assert all(set(r['body_keys'])<= {'region','apply_no','queue','version'} for r in requested)
            checks.append('family_information_browser_only_analysis_available')
            shot=call('Page.captureScreenshot',{'format':'png'});(output/'query-mobile-390.png').write_bytes(base64.b64decode(shot['data']))
            for slug,payload in data.items():
                number=next((no for no,ms in payload['rows'].items() if any(payload['batches'][m[0]].get('review_only') and payload['batches'][m[0]].get('review_rows',{}).get(no,{}).get('status')=='qualified' for m in ms)),None)
                if not number:continue
                match=next(m for m in payload['rows'][number] if payload['batches'][m[0]].get('review_only'))
                queue=payload['batches'][match[0]]['Q'][match[2]]
                call('Page.navigate',{'url':origin+'/'+slug+'.html?t='+token})
                wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
                evaluate("$('q').value="+json.dumps(number)+";$('q-queue').value="+json.dumps(queue)+";run()")
                wait_for("$('out').textContent.includes('官方核查记录')")
                assert evaluate("$('out').textContent.includes('公示合格')&&!$('out').querySelector('.rank-grid')&&$('out').textContent.includes('暂不提供排名和机会分析')")
                shot=call('Page.captureScreenshot',{'format':'png'});(output/f'review-only-{slug}.png').write_bytes(base64.b64decode(shot['data']))
                checks.append('review_only_real_record_'+slug+'_no_fabricated_rank')
            call('Page.navigate',{'url':origin+'/guangming.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            evaluate("$('q').value='SQ2026BZ360002688';$('q-queue').value='第三队列';run()")
            wait_for("$('out').textContent.includes('公示合格')")
            evaluate("$('q').value='SQ2026BZ360007741';$('q').dispatchEvent(new Event('input'));$('q-queue').value='第三队列';run()")
            wait_for("document.getElementById('out').textContent.includes('40㎡')")
            assert evaluate("Object.keys(DATA.rows).length===1&&!('SQ2026BZ360002688' in DATA.rows)")
            checks.append('consecutive_queries_clear_previous_records')
            stamp[0]=datetime(2026,10,27,tzinfo=BEIJING)
            evaluate('run()')
            wait_for("document.getElementById('gate').textContent.includes('已过期')")
            assert evaluate("$('app').hidden&&DATA.batches.length===0&&Object.keys(DATA.rows).length===0&&$('out').textContent==='' ")
            checks.append('server_next_midnight_rejects_and_clears_loaded_results')
            # A fresh page gets <2 seconds of server-approved time and clears itself without another query.
            stamp[0]=datetime(2026,10,26,23,59,58,500000,tzinfo=BEIJING)
            call('Page.navigate',{'url':origin+'/guangming.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            wait_for("document.getElementById('gate').textContent.includes('已过期')")
            checks.append('server_deadline_timer_expires_without_user_action')
            call('Browser.close');ws.close();ws=None;proc.wait(timeout=10)
        result={'all_passed':True,'checks':checks,'phone_clock_ignored':True,'real_cloud_deployed':False,'group_messages_sent':0}
        (output/'local-browser-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
    finally:
        if ws:
            try:ws.close()
            except Exception:pass
        if proc and proc.poll() is None:proc.terminate();proc.wait(timeout=10)
        server.shutdown();server.server_close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    print(json.dumps(verify(args.release,args.output),ensure_ascii=False))
