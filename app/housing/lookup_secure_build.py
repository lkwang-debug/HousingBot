"""Build public shells and a separate private function package; no secret exports."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def secure_page(region, api, version, pages):
    if not api.startswith('https://') and not api.startswith('http://127.0.0.1:'):
        raise ValueError('查询API必须为HTTPS；仅本地验收允许127.0.0.1')
    config = _json({'region': region, 'api': api.rstrip('/'), 'version': version}).replace('</', '<\\/')
    access = (HERE / 'lookup_access.js').read_text(encoding='utf-8')
    if region:
        template = (HERE / 'lookup_template.html').read_text(encoding='utf-8')
        return (template.replace('__DISTRICT__', escape(pages[region]['district']))
                .replace('__DATA__', '{"batches":[],"rows":{}}')
                .replace('__OPPORTUNITY_JS__', (HERE / 'opportunity.js').read_text(encoding='utf-8'))
                .replace('__LOOKUP_UI_JS__', (HERE / 'lookup_ui.js').read_text(encoding='utf-8'))
                .replace('__GATE_SCRIPT__', '<script>const ACCESS_CONFIG='+config+';\n'+access+'\nLookupAccess.start();</script>'))
    buttons = ''.join('<a class="btn" data-region="'+escape(slug)+'" href="#">'+escape(info['district'])+' →</a>' for slug, info in pages.items())
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow"><title>深圳保障房 · 认租编号自助查询</title><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#f6f7f9;padding:16px}.card{max-width:760px;background:white;padding:20px;margin:12px auto;border-radius:12px}.btn{display:block;background:#0b6bcb;color:white;padding:12px;margin:12px 0;border-radius:8px;text-decoration:none}h1{font-size:22px}</style></head><body><div class="card" id="gate" hidden></div><div id="app" hidden><div class="card"><h1>深圳保障房 · 认租编号自助查询</h1><p id="link-validity"></p><p>选择地区，输入认租编号及本人队列查询。完整名单不在页面中公开。</p>'+buttons+'<p>数据来自官方公开名单，以最新资格核查及正式选房安排为准。</p></div></div><script>const ACCESS_CONFIG='+config+';\n'+access+'\nLookupAccess.start();</script></body></html>'


def collect_payloads(directory):
    """Legacy pages are private input, never included in signed static artifacts."""
    from app.housing.opportunity import load_payload
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    payloads = {}
    for name, entry in manifest['pages'].items():
        if name == 'index.html':
            continue
        if Path(name).name != name or not name.endswith('.html'):
            raise ValueError('非法查询页名称')
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('查询页与清单不一致，不能打包')
        payloads[path.stem] = load_payload(path)
    return payloads, manifest


def build_release(directory, output, api, site_origin):
    """Create an isolated, immutable release; never mutate the current online receipt."""
    from app.housing.lookup import validate_payload
    payloads, source = collect_payloads(directory)
    if urlsplit(site_origin).scheme != 'https' or urlsplit(site_origin).path not in ('', '/'):
        raise ValueError('站点来源应为HTTPS域名，不带路径')
    for data in payloads.values():
        validate_payload(data)
    if not payloads:
        raise ValueError('没有有效名单')
    # Version binds every byte of roster and public runtime; no signing key involved.
    runtime_files = ('lookup_secure_build.py','lookup_http.py','lookup_service.py','lookup_tokens.py','lookup_access.js','lookup_ui.js','lookup_template.html','opportunity.js','opportunity.py','handling.py')
    runtime = {name: hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in runtime_files}
    version = hashlib.sha256(_json([payloads, runtime, api, site_origin]).encode()).hexdigest()
    regions = {slug: {'district': data['district'], 'file': slug+'.html',
                      'queues': list(dict.fromkeys(q for b in data['batches'] for q in b['Q']))}
               for slug, data in payloads.items()}
    metadata = {'version': version, 'regions': regions,'built_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    output = Path(output)
    if output.exists():
        raise ValueError('输出目录已存在，请使用新版本目录，避免覆盖成品')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='housing-secure-', dir=output.parent) as temp:
        staging = Path(temp); public=staging/'static'; private=staging/'function'
        public.mkdir();private.mkdir()
        for slug in ['', *regions]:
            (public/(slug+'.html' if slug else 'index.html')).write_text(secure_page(slug,api,version,regions),encoding='utf-8')
        (private/'data.json').write_text(_json(payloads),encoding='utf-8')
        (private/'metadata.json').write_text(_json(metadata),encoding='utf-8')
        for name in ('lookup_tokens.py','lookup_service.py','opportunity.py','lookup_http.py','handling.py'):
            shutil.copy2(HERE/name,private/name)
        (private/'scf_bootstrap').write_text('#!/bin/bash\ncd /var/user\nexec /var/lang/python39/bin/python3.9 lookup_http.py\n',encoding='utf-8',newline='\n')
        (private/'index.py').write_text('''import json,os\nfrom pathlib import Path\nfrom lookup_service import LookupService\n_SERVICE=None\ndef main_handler(event,context):\n    global _SERVICE\n    if _SERVICE is None:\n        root=Path(__file__).parent\n        _SERVICE=LookupService(json.loads((root/"data.json").read_text(encoding="utf-8")),json.loads((root/"metadata.json").read_text(encoding="utf-8")),os.environ.get("HOUSING_LOOKUP_SIGNING_SECRET",""),os.environ.get("HOUSING_LOOKUP_SITE_ORIGIN",""))\n    return _SERVICE.handle(event)\n''',encoding='utf-8')
        info = {'schema': 'signed-lookup-v1', 'version': version, 'source_version': source['version'],
                'built_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                'api': api, 'site_origin': site_origin, 'regions': regions,
                'notices': {k:v for entry in source['pages'].values() for k,v in entry.get('notices',{}).items()},
                'static_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in public.iterdir()},
                'function_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in private.iterdir()}}
        (public/'manifest.json').write_text(_json({'schema':info['schema'],'version':version,'regions':regions}),encoding='utf-8')
        (staging/'release.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(staging/'cloud-function.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for path in private.iterdir():
                entry=zipfile.ZipInfo(path.name);entry.create_system=3
                entry.external_attr=(0o100755 if path.name=='scf_bootstrap' else 0o100644)<<16
                entry.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(entry,path.read_bytes())
        shutil.copytree(staging, output)
    return info
