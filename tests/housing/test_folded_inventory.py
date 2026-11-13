import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
from bs4 import BeautifulSoup

from app.housing.delivery import HousingPushClient
from app.housing.inventory import inventory_text
from app.housing.message_format import compact_html, has_inventory_fold
from app.housing.models import HousingDelivery
from app.housing.rule_briefs import rule_brief


def block(name):
    return f'**房源明细与官方费用**\n\n**房源项目：{name}**\n\n50㎡｜76套｜租金16.2元/㎡·月\n\n**申请条件与办理规则**\n\n**配租对象（概要）**\n\n不限户籍，例外见原文。'


def test_multiple_native_folds_preserve_all_fields_and_keep_rules_outside():
    content=block('钟表基地公寓')+'\n\n'+block('另一项目')
    assert has_inventory_fold(content) and '<details>' not in content
    soup=BeautifulSoup(compact_html(content),'html.parser')
    folds=soup.find_all('details')
    assert len(folds)==2
    for fold,name in zip(folds,['钟表基地公寓','另一项目']):
        assert not fold.has_attr('open') and fold.summary.get_text()=='房源明细与官方费用（点击展开）'
        assert name in fold.get_text() and '76套' in fold.get_text() and '16.2元/㎡·月' in fold.get_text()
        assert '配租对象' not in fold.get_text()
    assert soup.get_text().count('不限户籍')==2


def test_only_generated_complete_markdown_sections_can_add_native_html():
    content=block('正常项目')+'\n\n<script>evil()</script>\n\n<details open>不可信原文</details>'
    soup=BeautifulSoup(compact_html(content),'html.parser')
    assert len(soup.find_all('details'))==1 and soup.find('script') is None
    incomplete='**房源明细与官方费用**\n\n没有完整结束标记'
    assert not has_inventory_fold(incomplete) and '<details>' not in compact_html(incomplete)


def test_briefs_preserve_exceptions_and_unknown_rules_do_not_invent_gates():
    item={'heading':'一、本批次配租对象','text':'盐田区户籍；满足收入财产限额，或经民政部门认定的特困人员。'}
    assert '或属于' in rule_brief(item)
    item={'heading':'一、配租对象','text':'不限户籍，不要求社保满5年，其他例外见原文。'}
    result=rule_brief(item)
    assert '本区户籍' not in result and '社保满5年' not in result


def test_inventory_shortens_three_rule_sections_without_mutating_evidence():
    data={'projects':[],'rule':{},'issues':[],'sources':[{'kind':'detail','url':'https://example.gov.cn/rule'}],
        'evidence':[{'heading':'一、配租对象','text':'完整资格条件。'*100},
                    {'heading':'三、配租方式','text':'超过70平方米房源优先面向五人及以上家庭，按轮候排位分批办理。'*30},
                    {'heading':'五、选房排位规则','text':'特别规则细节。'*100}]}
    before=copy.deepcopy(data);text=inventory_text(data,'gongzu')
    assert data==before and '完整资格条件。'*10 not in text
    assert '配租对象（概要）' in text and '配租方式（概要）' in text and '选房排位规则（概要）' in text
    assert text.count('https://example.gov.cn/rule')>=3
    assert '70㎡以上房源优先面向五人及以上家庭' in text


def test_short_report_with_folds_uses_html_transport_too():
    content=block('钟表基地公寓');seen=[]
    def handler(request):
        seen.append(json.loads(request.read().decode('utf-8')))
        return httpx.Response(200,json={'code':200,'data':'mock-message'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            sender=HousingPushClient(http);sender.settings=SimpleNamespace(pushplus_token='test-token')
            delivery=HousingDelivery();await sender._submit(delivery,'早报',content,'test-topic')
            assert delivery.status=='submitted'
    asyncio.run(run())
    assert len(seen)==1 and seen[0]['template']=='html'
    assert '<details>' in seen[0]['content'] and '<summary>' in seen[0]['content']
