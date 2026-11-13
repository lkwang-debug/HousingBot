"""Verify deployed signed lookup in an isolated mobile Chrome; no group sending."""
import argparse
import base64
import json
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
import httpx
import websocket
from app.housing.config import get_housing_settings
from app.housing.lookup_tokens import issue_token,beijing_now


def verify(output):
    settings=get_housing_settings();secret=settings.lookup_signing_secret.get_secret_value()
    token=issue_token(secret);expired=issue_token(secret,beijing_now()-timedelta(days=1))
    base=settings.lookup_url.rsplit('/',1)[0]+'/'
    with httpx.Client(timeout=20) as client:
        res=client.post(settings.lookup_api_url+'/session',headers={'Authorization':'Bearer '+token,'Origin':base.split('/hzktest')[0]},json={})
        res.raise_for_status();expected_regions=len(res.json()['regions'])
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    ws=None;proc=None;checks=[]
    with tempfile.TemporaryDirectory(prefix='housing-signed-online-',ignore_cleanup_errors=True) as temp:
        try:
            profile=Path(temp)
            proc=subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless=new','--disable-gpu','--no-first-run','--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            limit=time.monotonic()+20
            while not (profile/'DevToolsActivePort').exists():
                if time.monotonic()>limit:raise RuntimeError('Chrome startup timeout')
                time.sleep(.05)
            port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
            with httpx.Client(trust_env=False) as client:tab=next(t for t in client.get(f'http://127.0.0.1:{port}/json/list').json() if t['type']=='page')
            ws=websocket.create_connection(tab['webSocketDebuggerUrl'],suppress_origin=True,timeout=30);seq=0;network=[]
            def call(method,params=None):
                nonlocal seq
                seq+=1;ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    message=json.loads(ws.recv())
                    if message.get('method')=='Network.loadingFailed':
                        p=message['params'];network.append({k:p[k] for k in ('errorText','blockedReason','corsErrorStatus') if k in p})
                    if message.get('method')=='Network.responseReceived':
                        r=message['params']['response'];network.append({'url':r['url'].split('?',1)[0],'status':r['status'],'mime':r.get('mimeType')})
                    if message.get('id')!=seq:continue
                    if 'error' in message:raise RuntimeError(str(message['error']))
                    return message.get('result',{})
            def evaluate(text):
                result=call('Runtime.evaluate',{'expression':text,'returnByValue':True,'awaitPromise':True})
                if result.get('exceptionDetails'):raise RuntimeError(str(result['exceptionDetails']))
                return result.get('result',{}).get('value')
            def wait_for(expression):
                limit=time.monotonic()+25
                while not evaluate(expression):
                    # CloudBase's own test-domain notice is a site confirmation,
                    # not a browser TLS/security interstitial. No credentials or agreement.
                    evaluate("if(document.body?.innerText.includes('页面访问提示')){const b=Array.from(document.querySelectorAll('button,a,input[type=button]')).find(e=>(e.textContent||e.value||'').trim()==='确定访问');if(b)b.click();}")
                    if time.monotonic()>limit:
                        diagnostic=str(evaluate("document.body?document.body.innerText:'empty page'"))[:1500].replace(token,'[bearer]').replace(expired,'[expired-bearer]')
                        (output/'online-browser-diagnostic.txt').write_text(diagnostic,encoding='utf-8')
                        (output/'online-network-diagnostic.json').write_text(json.dumps(network,ensure_ascii=False,indent=2),encoding='utf-8')
                        raise RuntimeError('Online mobile condition timeout: '+expression+'; '+diagnostic[:250])
                    time.sleep(.05)
            call('Page.enable');call('Network.enable');call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            call('Page.addScriptToEvaluateOnNewDocument',{'source':"const RealDate=Date;window.Date=class extends RealDate{constructor(...args){super(...(args.length?args:['2035-01-01T00:00:00Z']));}static now(){return 2051222400000;}};"})
            call('Page.navigate',{'url':base+'index.html?d='+beijing_now().strftime('%Y%m%d')})
            wait_for("document.getElementById('gate')?.textContent.includes('入口已升级')")
            checks.append('old_date_link_blocked_online')
            call('Page.navigate',{'url':base+'index.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            assert evaluate(f"document.querySelectorAll('a[data-region]').length==={expected_regions}&&Array.from(document.querySelectorAll('a[data-region]')).every(a=>a.href.includes('?t='))")
            checks.append('all_published_regions_preserve_daily_token_online_phone_time_2035')
            call('Page.navigate',{'url':base+'guangming.html?t='+token})
            wait_for("document.getElementById('app')&&!document.getElementById('app').hidden")
            evaluate("$('q').value='SQ2026BZ360002688';$('q-queue').value='第三队列';run()")
            wait_for("document.getElementById('out').textContent.includes('391')")
            assert evaluate("$('out').textContent.includes('50㎡')&&$('out').textContent.includes('547')&&Object.keys(DATA.rows).length===1&&DATA.batches.every(b=>!('R' in b))")
            if evaluate("DATA.batches.some(b=>b.qualification)"):
                assert evaluate("$('out').textContent.includes('公示合格')&&$('out').textContent.includes('77.7%')&&$('out').textContent.includes('4.5户／套')")
                assert evaluate("$('out').textContent.includes('本次名单未公开')&&DATA.batches.every(b=>Object.keys(b.review_rows||{}).length<=1)")
                checks.append('latest_publicity_and_single_household_review_online')
                if evaluate("DATA.batches.some(b=>b.review_statistics?.concrete_units?.length)"):
                    assert evaluate("$('out').textContent.includes('官方名单中的本人具体意向')&&$('out').textContent.includes('钟表基地公寓')&&$('out').textContent.includes('一房一厅')")
                    assert evaluate("$('out').textContent.includes('异议／复核')&&$('out').textContent.includes('仅受理所属队列认定异议')")
                    assert evaluate("Array.from(document.querySelectorAll('.handling-status')).some(e=>e.textContent==='已截止')")
                    assert evaluate("DATA.batches.some(b=>b.review_statistics?.concrete_units?.length===14)&&$('out').textContent.includes('本轮不合格原因概览')")
                    evaluate("window.originalHandlingNow=LookupAccess.now;LookupAccess.now=()=>Date.parse('2026-11-03T18:01:00+08:00');refreshHandlingStatuses()")
                    assert evaluate("Array.from(document.querySelectorAll('.handling-status')).some(e=>JSON.parse(e.dataset.window).kind==='withdrawal'&&e.textContent==='已截止')")
                    evaluate("LookupAccess.now=window.originalHandlingNow;refreshHandlingStatuses()")
                    checks.append('concrete_intention_and_separate_official_deadlines_online')
                if evaluate("DATA.batches.some(b=>b.access_context?.qualified_reference?.available)"):
                    assert evaluate("$('out').textContent.includes('118户')&&$('out').textContent.includes('第 119 个')&&$('out').textContent.includes('不是当前梯队')")
                    checks.append('qualified_119_reference_online_not_current_rank')
            evaluate("document.querySelector('input[name=project-0]').checked=true;$('people-0').value='3';$('children-0').value='1';runAnalysis(0)")
            wait_for("document.getElementById('scenario-0').textContent.includes('分析结果')")
            has_formal=evaluate("DATA.batches.some(b=>b.final_rows?.['SQ2026BZ360002688']?.scope_verified)")
            if has_formal:
                assert evaluate("$('out').textContent.includes('官方正式选房排序')&&$('out').textContent.includes('169')&&$('out').textContent.includes('最新选房名单记载队列')")
                checks.append('new_official_selection_rank_169_and_queue_online')
            if evaluate("DATA.batches.some(b=>b.qualification)") and not has_formal:
                assert evaluate("$('scenario-0').textContent.includes('最新完整队列')&&!$('scenario-0').textContent.includes('暂时排不到你')")
                checks.append('missing_current_queues_block_unsupported_default_prediction')
                if evaluate("!!$('review-filter-0')"):
                    evaluate("$('review-filter-0').checked=true;runAnalysis(0)")
                    assert evaluate("$('scenario-0').textContent.includes('原参考前序834户')&&$('scenario-0').textContent.includes('暂剔除101户')&&$('scenario-0').textContent.includes('733户')&&$('scenario-0').textContent.includes('390户未审核')&&$('scenario-0').textContent.includes('658户')&&$('scenario-0').textContent.includes('344户至少申报')")
                    checks.append('review_filtered_scenario_online_no_unreviewed_elimination')
            checks.append('live_query_cors_50sqm_ranks_and_browser_analysis')
            shot=call('Page.captureScreenshot',{'format':'png'});(output/'online-mobile-390.png').write_bytes(base64.b64decode(shot['data']))
            call('Page.navigate',{'url':base+'guangming.html?t='+expired})
            wait_for("document.getElementById('gate').textContent.includes('已过期')")
            assert evaluate("$('app').hidden&&DATA.batches.length===0")
            checks.append('yesterday_valid_signature_rejected_online')
            shot=call('Page.captureScreenshot',{'format':'png'});(output/'online-expired-390.png').write_bytes(base64.b64decode(shot['data']))
            result={'all_passed':True,'checks':checks,'cloud_deployed':True,'group_messages_sent':0}
            (output/'online-browser-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            call('Browser.close');ws.close();ws=None;proc.wait(timeout=10)
            return result
        finally:
            if ws:
                try:ws.close()
                except Exception:pass
            if proc and proc.poll() is None:proc.terminate();proc.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    print(json.dumps(verify(args.output),ensure_ascii=False))
