"""Isolated headless Chrome checks for local report artifacts (no push requests)."""
from __future__ import annotations
import argparse
import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import websocket


def verify(directory,instant=False):
    directory=Path(directory).resolve();screens=directory/'mobile';screens.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='housing-daily-qa-',ignore_cleanup_errors=True) as temporary:
        profile=Path(temporary)
        proc=subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless=new','--disable-gpu',
            '--no-first-run','--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        ws=None
        try:
            deadline=time.monotonic()+20
            while not (profile/'DevToolsActivePort').exists():
                if time.monotonic()>deadline:raise RuntimeError('Chrome startup timeout')
                time.sleep(.05)
            port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
            with httpx.Client(trust_env=False) as client:
                tab=next(t for t in client.get(f'http://127.0.0.1:{port}/json/list').json() if t['type']=='page')
            ws=websocket.create_connection(tab['webSocketDebuggerUrl'],suppress_origin=True,timeout=30)
            seq=0
            def call(method,params=None):
                nonlocal seq
                seq+=1;ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    data=json.loads(ws.recv())
                    if data.get('id')!=seq:continue
                    if 'error' in data:raise RuntimeError(str(data['error']))
                    return data.get('result',{})
            def evaluate(expression):
                result=call('Runtime.evaluate',{'expression':expression,'returnByValue':True})
                if result.get('exceptionDetails'):raise RuntimeError(str(result['exceptionDetails']))
                return result.get('result',{}).get('value')
            results=[];call('Page.enable')
            for width in (320,375,390):
                call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':True})
                names=('notice-2424-1','notice-2249-1','notice-1058-1') if instant else ('20261025-digest','20261024-evening','layout-B4','layout-B2249','layout-B2270','layout-B2379')
                for name in names:
                    call('Page.navigate',{'url':(directory/(name+'.html')).as_uri()})
                    deadline=time.monotonic()+10
                    while not evaluate("document.body&&/深圳住房(?:管家|通告)/.test(document.body.textContent)"):
                        if time.monotonic()>deadline:raise RuntimeError('report load timeout')
                        time.sleep(.05)
                    assert evaluate("!['B147','B501','B803'].some(id=>document.body.textContent.includes(id))")
                    assert evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                    visible=evaluate('document.body.innerText.length')
                    if instant:
                        assert evaluate("document.body.textContent.includes('本次涉及1批')&&!document.body.textContent.includes('HOUSINGFOLD')&&!document.body.textContent.includes('全市去重')")
                        assert evaluate("Array.from(document.querySelectorAll('details')).every(d=>!d.open)")
                        shot=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                        (screens/f'{name}-collapsed-{width}.png').write_bytes(base64.b64decode(shot['data']))
                    if name.startswith('2026'):
                        assert evaluate("document.body.textContent.includes('认租中3批')&&document.body.textContent.includes('公租房认租2批')&&document.body.textContent.includes('混合房源认租1批')")
                        shot=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                        (screens/f'{name}-{width}.png').write_bytes(base64.b64decode(shot['data']))
                    evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
                    assert evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                    assert evaluate("Array.from(document.querySelectorAll('td')).every(td=>parseFloat(getComputedStyle(td).fontSize)>=14)")
                    if instant:
                        if name=='notice-2424-1':
                            assert evaluate("['289套','冠铭花园','众悦家园','长源京基御领公馆','山海逸居','龙瑞佳园','硅谷公寓','龙海家园','2.25','22.5','1. 一人及以上家庭','3. 三人及以上家庭'].every(s=>document.body.textContent.includes(s))")
                        elif name=='notice-2249-1':
                            assert evaluate("document.body.textContent.includes('894套')&&document.body.textContent.includes('租金口径1')&&!document.body.textContent.includes(\"'options':\")")
                        else:
                            assert evaluate("document.body.textContent.includes('3746')&&document.body.textContent.includes('33.7')")
                        evaluate("document.querySelector('table').scrollIntoView()")
                        shot=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                        (screens/f'{name}-expanded-{width}.png').write_bytes(base64.b64decode(shot['data']))
                    if name=='layout-B4':
                        assert evaluate("document.body.textContent.includes('33.7')&&document.body.textContent.includes('20.1')&&document.body.textContent.includes('3746')")
                        evaluate("Array.from(document.querySelectorAll('h4')).find(e=>e.textContent.includes('摇号房源竞争概览')).scrollIntoView()")
                    elif name=='layout-B2249':
                        assert evaluate("document.body.textContent.includes('894套')&&document.body.textContent.includes('租金口径1')")
                        evaluate("Array.from(document.querySelectorAll('summary')).find(e=>e.textContent.includes('房源明细')).scrollIntoView()")
                    elif name=='layout-B2270':
                        assert evaluate("document.body.textContent.includes('311套')")
                        evaluate("Array.from(document.querySelectorAll('h4')).find(e=>e.textContent.includes('配租面积')).scrollIntoView()")
                    if name.startswith('layout') and width==390:
                        shot=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                        (screens/f'{name}-{width}.png').write_bytes(base64.b64decode(shot['data']))
                    results.append({'name':name,'width':width,'default_visible_chars':visible,'no_horizontal_overflow':True,'minimum_table_font':14})
            output={'chrome_mobile':True,'checks':results,'all_passed':True,'no_external_pushes':True}
            (directory/'browser-verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'chrome_mobile':True,'widths':[320,375,390],'pages_checked':len(results),'all_passed':True},ensure_ascii=False))
            return output
        finally:
            if ws:
                try:ws.send(json.dumps({'id':99999,'method':'Browser.close'}));ws.close()
                except Exception:pass
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--instant',action='store_true')
    args=parser.parse_args();verify(args.directory,instant=args.instant)
