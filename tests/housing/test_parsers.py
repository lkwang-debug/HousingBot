from __future__ import annotations

import json
from datetime import datetime

from app.housing.parsers.dates import parse_date_text
from app.housing.parsers.list_gkmlpt import parse_gkmlpt_index, parse_gkmlpt_payload
from app.housing.parsers.list_nfcms import page_url_for, parse_nfcms_list

NFCMS_HTML = """
<div class="listcontent_right">
  <ul>
    <li><a href="/ztfw/zfbz/tzgg2017/content/post_12589448.html"
           title="深圳市住房和建设局关于面向大鹏新区户籍在册轮候人配租保障性租赁住房的通告">x</a>
        <span>2026-01-08</span></li>
    <li><a href="javascript:void(0)">无效链接</a></li>
    <li><a href="/content/post_12589449.html">关于我们</a><span>2026-01-09</span></li>
  </ul>
</div>
"""

PINGSHAN_HTML = """
<ul class="ftdt-list">
  <li><a href="/content/post_70000001.html">坪山区2026年第一批次配租通告 2026-07-20</a>
      <span>2026-07-20</span></li>
</ul>
"""

DP_HTML = """
<div><p><a href="/xxgk/content/post_80000001.html">大鹏新区2026年配租通告</a>
   <span>2026-09-01</span></p></div>
"""


def test_nfcms_list_keeps_only_articles():
    items = parse_nfcms_list(NFCMS_HTML, "https://zjj.sz.gov.cn/ztfw/zfbz/tzgg2017/")
    assert len(items) == 1
    assert items[0].title.startswith("深圳市住房和建设局")
    assert items[0].published_at == datetime(2026, 1, 8)


def test_nfcms_list_cleans_trailing_date():
    items = parse_nfcms_list(PINGSHAN_HTML, "https://www.szpsq.gov.cn/ztfw/zfbzfw/fptg/")
    assert items[0].title == "坪山区2026年第一批次配租通告"
    assert items[0].published_at == datetime(2026, 7, 20)


def test_nfcms_anchor_fallback_without_li():
    # 大鹏等站点列表项不是 li 结构
    items = parse_nfcms_list(DP_HTML, "https://www.dpxq.gov.cn/xxgk/zdly/bzxzf/tzgg/")
    assert len(items) == 1
    assert items[0].title == "大鹏新区2026年配租通告"


def test_page_url_for_uses_index_n():
    entry = "https://zjj.sz.gov.cn/ztfw/zfbz/tzgg2017/index.html"
    assert page_url_for(entry, 0) == entry
    assert page_url_for(entry, 1).endswith("index_2.html")


def test_parse_date_text_two_digit_year():
    assert parse_date_text("发布日期 26-10-20") == parse_date_text("2026-10-20")


GKMLPT_INDEX = """
<html><script>
var SID: '755331';
var TREE: [{"id": 12588, "name": "住房保障", "children": []}], other
</script></html>
"""


def test_parse_gkmlpt_index():
    sid, tree = parse_gkmlpt_index(GKMLPT_INDEX)
    assert sid == "755331"
    assert tree[0]["id"] == 12588


def test_gkmlpt_payload_reads_date_field():
    payload = {
        "articles": [
            {
                "title": "关于2026年度面向南山区户籍公共租赁住房在册轮候家庭配租的通告",
                "url": "/nsqzjj/gkmlpt/content/12/12957/post_12957099.html",
                "display_publish_time": 0,
                "publish_time": None,
                "date": 1792339200,
                "document_number": "住保租〔2026〕036号",
            }
        ]
    }
    items = parse_gkmlpt_payload(payload, "https://www.szns.gov.cn/nsqzjj/gkmlpt/api/all/22234")
    assert len(items) == 1
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2026
    assert items[0].url.startswith("https://www.szns.gov.cn/nsqzjj/gkmlpt/content/")
    assert items[0].doc_number == "住保租〔2026〕036号"


def test_gkmlpt_payload_tolerates_string_epoch():
    payload = {"articles": [{"title": "标题测试", "url": "/a/post_1.html", "date": "1792339200"}]}
    items = parse_gkmlpt_payload(payload, "https://example.gov.cn/api")
    assert items[0].published_at.year == 2026


def test_gkmlpt_payload_without_date():
    payload = {"articles": [{"title": "标题测试", "url": "/a/post_2.html"}]}
    items = parse_gkmlpt_payload(payload, "https://example.gov.cn/api")
    assert items[0].published_at is None


def test_payload_is_json_serialisable_roundtrip():
    payload = json.loads(json.dumps({"articles": []}))
    assert parse_gkmlpt_payload(payload, "https://example.gov.cn/api") == []
