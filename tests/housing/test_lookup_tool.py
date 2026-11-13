from __future__ import annotations

import json

from tools.build_lottery_lookup import (
    _build_index,
    _category_label,
    _category_signature,
    _queue_sort_key,
    _render_html,
)


def test_category_label_drops_queue_words():
    # 队列是页面上单独选的东西，不能混进户型名，否则同一个户型会出现三行。
    label = "附件1：华章新筑一房户型第一队列摇号结果.pdf"
    assert _category_label(label, "50") == "华章新筑一房户型"
    assert _category_signature(label) == "一房"


def test_category_signature_separates_sizes_category_key_misses():
    # 35㎡/70㎡/90㎡ 的 category_key 都回 "all"，靠面积区分，否则三个户型的排位会混在一起。
    assert _category_signature("附件1：建筑面积35㎡左右的房源（意向一）的公证摇号结果.pdf") == "35"
    assert _category_signature("附件3：建筑面积70㎡左右的房源（意向三）的公证摇号结果.pdf") == "70"
    assert _category_signature("附件4：建筑面积85、95㎡左右的房源（意向四）的公证摇号结果.pdf") == "85、95"


def test_queue_order_follows_rule_not_pdf_order():
    # 名单 PDF 物理顺序不可信：必须按 第一→第二→第三 重排。
    names = ["第三队列", "第一队列", "第二队列", "优先-第一队列", "未标注"]
    assert sorted(names, key=_queue_sort_key) == [
        "优先-第一队列",
        "第一队列",
        "第二队列",
        "第三队列",
        "未标注",
    ]


def test_lookup_html_embeds_data_without_personal_info():
    payload = {
        "district": "光明区",
        "batches": [
            {
                "t": "光明区2026年第一批次公证摇号结果",
                "u": "https://example.gov.cn/post_1.html",
                "d": "2026-10-16",
                "Q": ["第一队列"],
                "c": [{"l": "40㎡", "n": 10, "u": 3, "q": {"第一队列": 10}}],
            }
        ],
        "rows": {"SQ2026BZ000000001": [[0, 0, 0, 1, 1]]},
    }
    html = _render_html(payload)
    assert "const DATA = " in html
    assert "光明区 保障房摇号排位查询" in html
    embedded = html.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    # 内嵌数据里只允许「编号 → 排位」，姓名/证件号一概不进页面。
    assert "姓名" not in embedded
    assert "身份证" not in embedded
    assert json.loads(embedded.replace("<\\/", "</"))["rows"]["SQ2026BZ000000001"] == [
        [0, 0, 0, 1, 1]
    ]


def test_lookup_page_keeps_official_queue_separate_from_scenarios():
    html = _render_html(
        {
            "district": "光明区",
            "batches": [
                {
                    "t": "光明区2026年第一批次公证摇号结果",
                    "u": "https://example.gov.cn/post_1.html",
                    "d": "2026-10-16",
                    "Q": ["第一队列", "第二队列", "第三队列"],
                    "c": [{"l": "50㎡", "n": 10, "u": 3, "q": {"第一队列": 5, "第二队列": 3, "第三队列": 2}}],
                }
            ],
            "rows": {"SQ2026BZ000000001": [[0, 0, 1, 547, 94]]},
        }
    )
    assert "function runAnalysis(index)" in html
    assert "HITS=found.map(m=>m.slice())" in html
    assert 'id="q-queue"' in html
    assert 'Opportunity.analyzeSimple' in html
    assert '本人申报的具体户型' not in html
    assert "function pickQueue" not in html


def test_lookup_page_is_gated_by_seven_calendar_days():
    # 推送里的链接带 ?d=YYYYMMDD；含标注当天共7个北京时间自然日。
    html = _render_html({"district": "光明区", "batches": [], "rows": {}})
    assert '<div id="app" hidden>' in html
    assert 'id="gate" hidden' in html
    assert "本链接已过期" in html
    assert "Intl.DateTimeFormat('sv-SE'" in html
    # 不许被搜索引擎收成公开入口。
    assert '<meta name="robots" content="noindex,nofollow">' in html
    assert '<meta name="referrer" content="no-referrer">' in html


def test_index_page_lists_generated_districts(tmp_path):
    (tmp_path / "guangming.html").write_text(
        "<html><head><title>光明区 保障房摇号排位查询</title></head></html>", encoding="utf-8"
    )
    (tmp_path / "baoan.html").write_text(
        "<html><head><title>宝安区 保障房摇号排位查询</title></head></html>", encoding="utf-8"
    )

    result = _build_index(tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert [page["file"] for page in result["pages"]] == ["baoan.html", "guangming.html"]
    assert 'href="guangming.html"' in index
    assert 'href="baoan.html"' in index
    assert "不含姓名和证件号" in index
    # 落地页透传原始日期，不因用户跨区跳转而续期。
    assert 'id="gate" hidden' in index
    assert "a.href += (a.href.indexOf('?') >= 0 ? '&' : '?') + 'd=' + day" in index
    # 落地页不内嵌名单，体积只有几 KB。
    assert (tmp_path / "index.html").stat().st_size < 8_000


def test_index_normalizes_short_district_names(tmp_path):
    # 命令行里常写 --district 光明，页面标题会是"光明"；落地页要显示"光明区"。
    (tmp_path / "gm.html").write_text(
        "<title>光明 保障房摇号排位查询</title>", encoding="utf-8"
    )
    result = _build_index(tmp_path)
    assert result["pages"][0]["district"] == "光明区"


def test_index_skips_itself_on_regeneration(tmp_path):
    (tmp_path / "guangming.html").write_text(
        "<title>光明区 保障房摇号排位查询</title>", encoding="utf-8"
    )
    _build_index(tmp_path)
    again = _build_index(tmp_path)
    assert [page["file"] for page in again["pages"]] == ["guangming.html"]
