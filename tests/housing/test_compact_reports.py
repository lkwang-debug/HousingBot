import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from app.housing.extract import ReviewInfo
from app.housing.reporting import AllocationProfile, BatchCard, CategorySummary, NoticeReference, ReportSnapshot
from app.housing.report_compact import (
    _current, _root, display_title, focus_reasons, object_summary,
    present_cards, render_compact_report,
)
from app.housing.report_render import _single_daily_report, render_daily_plain, render_report_parts


def load_snapshot(kind="digest"):
    data = json.loads((Path(__file__).parent / "fixtures/daily_compact_snapshot.json").read_text(encoding="utf-8"))
    cards = []
    for value in data["cards"]:
        value["profile"] = AllocationProfile(**value["profile"])
        value["references"] = [NoticeReference(**{**r, "published_at": datetime.fromisoformat(r["published_at"]) if r["published_at"] else None})
                               for r in value["references"]]
        for key in ("start", "end"):
            value[key] = datetime.fromisoformat(value[key]) if value[key] else None
        if value["review"]:
            review = value["review"]
            value["review"] = ReviewInfo(**{**review,
                "start": date.fromisoformat(review["start"]) if review["start"] else None,
                "end": date.fromisoformat(review["end"]) if review["end"] else None})
        cards.append(BatchCard(**value))
    return ReportSnapshot(**{**data, "kind": kind, "cards": cards,
        "generated_at": datetime.fromisoformat(data["generated_at"]),
        "window_start": datetime.fromisoformat(data["window_start"]),
        "source_updated_at": datetime.fromisoformat(data["source_updated_at"]) if data["source_updated_at"] else None,
        "totals": CategorySummary(**data["totals"]),
        "categories": {k: CategorySummary(**v) for k, v in data["categories"].items()}})


def visible(text):
    return BeautifulSoup(MarkdownIt("commonmark").render(text), "html.parser").get_text(" ", strip=True)


@pytest.mark.parametrize("kind", ["digest", "evening"])
def test_real_snapshot_keeps_all_batches_metadata_and_current_originals(kind):
    snap = load_snapshot(kind)
    before = snap.audit_dict()
    part = render_compact_report(snap)
    displays = present_cards(snap)
    assert len(displays) == 21
    assert sum(d.detail_level == "focus" for d in displays) == 7
    assert sum(d.detail_level == "brief" for d in displays) == 14
    assert len(part.batch_codes) == 21 and set(part.batch_codes) == {c.code for c in snap.cards}
    assert set(part.notice_ids) == {n for c in snap.cards for n in c.notice_ids}
    for d in displays:
        assert part.content.count(f"｜{d.card.code}】") == 1
        current = _current(d.card, snap)
        assert current.url in part.content
        if d.detail_level == "focus":
            assert _root(d.card).url in part.content
            assert all(r.url in part.content for r in d.card.references if r.is_new or r.is_update)
    assert snap.audit_dict() == before
    assert len(part.content.encode()) <= 18000 and "部分）" not in part.content


@pytest.mark.parametrize("kind", ["digest", "evening"])
def test_real_snapshot_reading_length_drops_at_least_a_third(kind):
    snap = load_snapshot(kind)
    old = _single_daily_report(snap, max_chars=18000, max_bytes=18000)
    new = render_compact_report(snap)
    reduction = 1 - len(visible(new.content)) / len(visible(old.content))
    # The target is approximate; a shorter result is valid when every required
    # field and batch is still covered by the preservation checks above.
    assert reduction >= .35, reduction


def test_four_summaries_and_focus_fields_are_distinct_markdown_paragraphs():
    snap = load_snapshot()
    soup = BeautifulSoup(MarkdownIt().render(render_compact_report(snap).content), "html.parser")
    labels = ("市级保租房", "区级保租房", "面向个人配租", "面向单位定向配租")
    paragraphs = [soup.find("strong", string=label).parent for label in labels]
    assert len({id(p) for p in paragraphs}) == 4
    for p in paragraphs:
        assert "认租中" in p.get_text() and "在途" in p.get_text()
    title = soup.find("strong", string="【公租房｜个人｜B34】").parent
    fields = list(title.find_next_siblings("p", limit=3))
    assert len(fields) == 3
    assert "初审公示中" in fields[0].get_text() and "合格403户／不合格35户" in fields[0].get_text()
    assert fields[1].get_text().startswith("对象概要：")
    assert fields[2].get_text().startswith("办理提示：") and len(fields[2].find_all("a")) == 2


def test_zero_change_summary_keeps_audience_split_only_where_meaningful():
    content = render_compact_report(load_snapshot()).content
    for label in ("市级保租房", "区级保租房", "公租房"):
        paragraph = next(p for p in content.split("\n\n") if p.startswith(f"**{label}**"))
        assert "个人" in paragraph and "单位" in paragraph and "待核实" in paragraph
    for label in ("面向个人配租", "面向单位定向配租"):
        paragraph = next(p for p in content.split("\n\n") if p.startswith(f"**{label}**"))
        assert "本期无变化" in paragraph and "／单位" not in paragraph
    assert "腾退0条" not in content and "摇号公布0批" not in content
    assert "未列出表示0" in content and "四类不能直接相加" in content


def test_summary_keeps_each_nonzero_event_metric():
    snap = load_snapshot()
    value = replace(snap.categories["district_baozu"], new_notices=2, updated_notices=1, vacancy_notices=3,
                    lottery_batches=1, selection_batches=2)
    snap.categories["district_baozu"] = value
    paragraph = next(p for p in render_compact_report(snap).content.split("\n\n") if p.startswith("**区级保租房**"))
    for metric in ("新增2条", "更新1条", "腾退3条", "摇号公布1批", "选房通知2批"):
        assert metric in paragraph


@pytest.mark.parametrize("delta,expected", [(timedelta(days=3), True), (timedelta(days=3, seconds=1), False)])
def test_upcoming_window_focus_boundary(delta, expected):
    snap = load_snapshot()
    card = replace(snap.cards[0], stage="announced", is_open=False, start=snap.generated_at + delta)
    assert ("三天内开始认租" in focus_reasons(card, snap)) == expected


@pytest.mark.parametrize("age,expected", [(timedelta(days=7), True), (timedelta(days=7, seconds=1), False)])
def test_unknown_handling_window_focus_seven_day_boundary(age, expected):
    snap = load_snapshot()
    ref = replace(snap.cards[0].references[0], stage="selection", published_at=snap.generated_at - age)
    card = replace(snap.cards[0], stage="selection", is_open=False, review=None, references=[ref])
    assert ("近期节点办理期限待核实" in focus_reasons(card, snap)) == expected


def test_publicity_is_focus_inclusive_of_end_date_but_not_after():
    snap = load_snapshot()
    card = next(c for c in snap.cards if c.code == "B34")
    assert "公示进行中" in focus_reasons(card, replace(snap, generated_at=datetime(2026, 10, 28, 23, 59)))
    assert not focus_reasons(card, replace(snap, generated_at=datetime(2026, 10, 29)))


def test_changed_expired_batch_stays_focus_and_keeps_all_change_urls():
    snap = load_snapshot()
    card = next(c for c in snap.cards if c.code == "B1316")
    changed = replace(card.references[0], id=99999, url="https://example.gov.cn/changed", is_update=True)
    card.references.append(changed)
    assert "本期公告变化" in focus_reasons(card, snap)
    content = render_compact_report(snap).content
    assert changed.url in content and card.references[0].url in content


@pytest.mark.parametrize("text", ["面向个人，不限户籍，不要求社保满5年，但特殊项目除外", "本次对象为南山区户籍或符合另列条件的在册轮候人", "不要求属于光明区户籍在册轮候人", "不受南山区户籍限制，在册轮候人均可申请"])
def test_object_summary_does_not_invert_negations_or_remove_alternatives(text):
    card = load_snapshot().cards[0]
    card.profile = replace(card.profile, object_text=(text,), audience="personal")
    assert object_summary(card) == "对象见原文"


def test_unknown_and_conflicting_objects_never_get_assumed_eligibility():
    card = load_snapshot().cards[0]
    card.profile = replace(card.profile, audience="unknown", object_text=("面向个人及企业",), issues=("配租对象证据冲突",))
    assert object_summary(card) == "对象见原文"


def test_enterprise_registry_and_talent_credential_remain_in_short_objects():
    cards = {c.code: c for c in load_snapshot().cards}
    assert object_summary(cards["B16"]) == "住房需求库内企事业单位"
    assert "才享光明“人才码”" in object_summary(cards["B1131"])
    assert object_summary(cards["B4"]) == "个人"


@pytest.mark.parametrize("text", ["本次配租对象为未纳入住房需求库的企事业单位", "本次配租对象为不在住房需求库内的企事业单位"])
def test_registry_exclusion_cannot_turn_into_registry_membership(text):
    card = next(c for c in load_snapshot().cards if c.code == "B16")
    card.profile = replace(card.profile, object_text=(text,))
    assert object_summary(card) == "对象见原文"


def test_generic_titles_shorten_but_project_and_incomplete_titles_remain():
    cards = {c.code: c for c in load_snapshot().cards}
    assert display_title(cards["B34"]) == "罗湖区2026年第3批配租"
    assert display_title(cards["B15"]) == "光明区2026年第3批配租"
    assert "鹏溪苑等十个" in display_title(cards["B1272"])
    assert display_title(cards["B972"]).endswith("公...")
    project = replace(cards["B34"], title="罗湖区住房和建设局关于面向个人的鹏溪苑项目配租住房的通告")
    assert "鹏溪苑项目" in display_title(project)


def test_brief_drops_full_expired_window_but_retains_date_and_phase_original():
    snap = load_snapshot()
    content = render_compact_report(snap).content
    paragraph = next(p for p in content.split("\n\n") if "｜B1316】" in p)
    assert "08-31认租已截止" in paragraph and "最近公告08-19" in paragraph
    assert "16:00" not in paragraph and "18:00" not in paragraph
    lottery = next(p for p in content.split("\n\n") if "｜B4】" in p)
    assert "摇号结果已公布" in lottery and "10-16公布" in lottery and "post_13024633" in lottery


def test_source_faults_unknown_classification_and_mixed_housing_stay_visible():
    snap = load_snapshot()
    snap.source_status = "已配置 25 个来源，异常 2 个，未检查 1 个；异常来源：测试源"
    c = snap.cards[0]
    c.profile = replace(c.profile, level="unknown", region="", housing_type="mixed", audience="unknown", issues=("组织机构证据冲突",))
    content = render_compact_report(snap).content
    assert "覆盖可能不全" in content and "测试源" in content
    assert "全市无新增" not in content and "已采集范围内近24小时无新增" in content
    assert content.count(f"｜{c.code}】") == 1 and "混合房源" in content and "组织机构证据冲突" in content


def test_full_exports_keep_detail_and_instant_uses_shared_native_layout():
    snap = load_snapshot()
    full = render_daily_plain(snap, keep_links=True)
    compact = render_compact_report(snap).content
    assert "流程：" in full and "流程：" not in compact
    import copy
    card=copy.deepcopy(snap.cards[0]);card.references[0].is_new=True
    instant = replace(snap, kind="instant",cards=[card])
    part, = render_report_parts(instant)
    assert '配租对象概要：' in part.content and part.wire_html
    assert '本次涉及1批' in part.content and 'HOUSINGFOLD' not in part.wire_html


def test_fixed_region_order_and_focus_precedence_inside_each_region():
    snap = load_snapshot()
    content = render_compact_report(snap).content
    markers = [f"### 📍{r}" for r in ("福田区", "罗湖区", "南山区", "盐田区", "宝安区", "龙岗区", "龙华区", "坪山区", "光明区", "大鹏新区")]
    assert [content.index(m) for m in markers] == sorted(content.index(m) for m in markers)
    displays = present_cards(snap)
    for region in {d.card.profile.region for d in displays}:
        focused = [d for d in displays if d.card.profile.region == region and d.detail_level == "focus"]
        brief = [d for d in displays if d.card.profile.region == region and d.detail_level == "brief"]
        if focused and brief:
            assert max(content.index(f"｜{d.card.code}】") for d in focused) < min(content.index(f"｜{d.card.code}】") for d in brief)


def test_tighter_transport_budget_only_condenses_labels_and_regional_repetition():
    snap = load_snapshot()
    normal = render_compact_report(snap)
    budget = len(normal.content.encode()) - 60
    tight = render_compact_report(snap, max_bytes=budget)
    assert len(tight.content.encode()) <= budget < len(normal.content.encode())
    assert normal.batch_codes == tight.batch_codes and normal.notice_ids == tight.notice_ids
    assert "对象：南山区户籍在册轮候人" in tight.content
    assert "初审公示期：2026-10-22—2026-10-28" in tight.content
    assert "合格403户／不合格35户" in tight.content
    assert "项目所在地：龙华区" in tight.content and "配租对象待核实" in tight.content
    assert "### 📍罗湖区" in tight.content and "机构：住房和建设局" in tight.content
    for d in present_cards(snap):
        assert tight.content.count(f"｜{d.card.code}】") == 1
        assert _current(d.card, snap).url in tight.content
        if d.focus_reasons: assert _root(d.card).url in tight.content
    assert "四类不能直接相加" in tight.content and "完整条件及否定／例外见官方原文" in tight.content
