import json
import re
import subprocess

import pytest

from tools.build_lottery_lookup import _gate_script


def gate(day, now):
    script=re.sub(r'^\s*<script>|</script>\s*$','',_gate_script())
    harness=r"""const vm=require('vm'),fs=require('fs'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const nodes={gate:{hidden:true,innerHTML:''},app:{hidden:true},'link-validity':{textContent:''}};
const buttons=[{href:'guangming.html'},{href:'baoan.html?source=index'}];
class FixedDate extends Date{constructor(...args){super(...(args.length?args:[input.now]));}}
const context=vm.createContext({Date:FixedDate,Intl,URLSearchParams,location:{search:input.day===null?'':'?d='+encodeURIComponent(input.day)},document:{getElementById:id=>nodes[id],querySelectorAll:()=>buttons}});
vm.runInContext(input.script,context);process.stdout.write(JSON.stringify({app:nodes.app.hidden,gate:nodes.gate.hidden,message:nodes.gate.innerHTML,validity:nodes['link-validity'].textContent,buttons}));"""
    result=subprocess.run(['node','-e',harness],input=json.dumps({'day':day,'now':now,'script':script}),text=True,encoding='utf-8',capture_output=True,check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('now', ['2026-10-22T16:00:00Z','2026-10-28T16:00:00Z','2026-10-29T15:59:59Z'])
def test_link_works_from_first_through_seventh_beijing_day_and_keeps_original_date(now):
    result=gate('20261023',now)
    assert result['app'] is False and result['gate'] is True
    assert '2026-10-23至2026-10-29' in result['validity']
    assert result['buttons'][0]['href']=='guangming.html?d=20261023'
    assert result['buttons'][1]['href']=='baoan.html?source=index&d=20261023'


def test_eighth_day_midnight_expires_in_beijing_not_machine_timezone():
    result=gate('20261023','2026-10-29T16:00:00Z')
    assert result['app'] is True and '本链接已过期' in result['message']
    assert '2026-10-23至2026-10-29' in result['message']


@pytest.mark.parametrize('day', [None,'','20260230','20261301','2026102','202610230','<img src=x>'])
def test_missing_or_invalid_dates_do_not_open_page_or_inject_html(day):
    result=gate(day,'2026-10-23T08:00:00Z')
    assert result['app'] is True and result['gate'] is False
    assert '<img' not in result['message'] and '?d=' not in result['buttons'][0]['href']


def test_future_link_is_not_called_expired_or_opened_early():
    result=gate('20261024','2026-10-23T08:00:00Z')
    assert result['app'] is True and '尚未生效' in result['message']


def test_valid_leap_day_and_cross_month_window():
    result=gate('20280229','2028-03-06T15:59:59Z')
    assert result['app'] is False and '2028-02-29至2028-03-06' in result['validity']
