"""Regressions from the official city talent / Luohu third-allocation pages."""
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.housing.batches import parse_batch_seq, rebuild_links
from app.housing.extract import extract_rent_window, extract_review_info
from app.housing.models import HousingNotice
from app.housing.report_render import render_report_parts
from app.housing.reporting import allocation_profile, build_snapshot
from app.housing.scan import _hydrate_detail, _should_fetch_detail, detail_refresh_candidates

NOW = datetime(2026, 10, 22, 9, 30)
SAMPLES = json.loads((Path(__file__).parent / "fixtures/accuracy_official_samples.json").read_text(encoding="utf-8"))
LUOHU_LAUNCH = "罗湖区住房和建设局关于2026年度第三批面向符合收入财产限额标准的辖区户籍在册轮候家庭配租公共租赁住房有关事项的通告（住保租〔2026〕035号）"


def row(session, key, title, *, published=None, **values):
    data = dict(canonical_key=key, canonical_url=f"https://example.gov.cn/{key}.html", url_hash=key,
        title=title, title_norm="", category="rent_launch", region_code="罗湖区", housing_types=["gongzu"],
        is_target=True, is_push_candidate=True, detail_status="ok", body_text="本次面向在册轮候家庭配租。",
        published_at=published or datetime(2026, 9, 24), first_seen_at=NOW,
        application_start=datetime(2026, 9, 24, 10), application_end=datetime(2026, 10, 13, 18))
    data.update(values)
    result = HousingNotice(**data)
    session.add(result); session.flush()
    return result


@pytest.mark.parametrize("text,number", [("2026年度第三批公共租赁住房", 3), ("2026年第三批次", 3), ("第十二批", 12)])
def test_batch_ordinal_with_and_without_ci(text, number):
    assert parse_batch_seq(text) == number


def test_official_city_august_window_is_not_missing_or_april():
    page = SAMPLES["1316"]
    window = extract_rent_window(page["body_text"], datetime(2026, 8, 19))
    assert (window.start, window.end, window.inferred) == (
        datetime(2026, 8, 19, 16), datetime(2026, 8, 31, 18), False)


def test_official_luohu_quotes_launch_and_has_separate_review_period(session):
    launch = row(session, "luohu-launch", LUOHU_LAUNCH, doc_number="住保租〔2026〕035号")
    sample = SAMPLES["2265"]
    followup = row(session, "luohu-review", sample["title"], published=datetime(2026, 10, 21),
        body_text=sample["body_text"], category="process", application_start=None, application_end=None)
    rebuild_links(session)
    assert followup.batch_key_v2 == launch.batch_key_v2
    assert followup.batch_link_status == "title-reference"
    snapshot = build_snapshot(session, "digest", now=NOW)
    card, = snapshot.cards
    assert card.code == f"B{launch.id}" and card.stage == "review"
    assert {launch.id, followup.id} == set(card.notice_ids)
    assert card.end == datetime(2026, 10, 13, 18) and not card.is_open
    assert (card.review.start, card.review.end) == (date(2026, 10, 22), date(2026, 10, 28))
    assert (card.review.eligible, card.review.ineligible) == (403, 35)
    assert card.review.notice_id == followup.id
    assert dict(card.timeline)["资格核查"] == "当前节点，已有公告"
    part, = render_report_parts(snapshot)
    assert "初审公示中" in part.content and "2026-10-22—2026-10-28" in part.content
    assert "合格403户／不合格35户" in part.content
    assert f"｜N{followup.id}】" not in part.content
    assert extract_rent_window(sample["body_text"], datetime(2026, 10, 21)).end is None
    ended, = render_report_parts(build_snapshot(session, "digest", now=NOW + timedelta(days=7)))
    assert "初审公示已结束，待后续公告" in ended.content


@pytest.mark.parametrize("with_date", [False, True])
def test_repeated_launch_titles_require_explicit_reference_date(session, with_date):
    title = "罗湖区住房和建设局关于面向个人配租公共租赁住房的通告"
    april = row(session, "april", title, published=datetime(2026, 4, 24))
    august = row(session, "august", title, published=datetime(2026, 8, 19))
    prefix = "于2026年8月19日发布了" if with_date else "此前发布了"
    node = row(session, "node", "罗湖区公共租赁住房初审结果", category="process",
        published=datetime(2026, 10, 21), body_text=prefix + "《" + title + "》。", application_start=None, application_end=None)
    rebuild_links(session)
    assert april.batch_key_v2 != august.batch_key_v2
    if with_date:
        assert node.batch_key_v2 == august.batch_key_v2 and node.batch_link_status == "title-reference"
    else:
        assert node.batch_key_v2 is None and node.batch_link_status == "ambiguous"


def test_title_reference_does_not_cross_audience(session):
    title = "罗湖区住房和建设局关于面向企事业单位配租公共租赁住房的通告"
    company = row(session, "company", title)
    personal = row(session, "personal-node", "罗湖区面向个人公共租赁住房初审结果", category="process",
        published=datetime(2026, 10, 21), body_text="此前发布了《" + title + "》。")
    rebuild_links(session)
    assert personal.batch_key_v2 is None and company.batch_key_v2


def test_identical_titles_published_months_apart_stay_different_batches(session):
    title = SAMPLES["1316"]["title"]
    old = row(session, "old-city", title, region_code="深圳市", housing_types=["baozu"],
        published=datetime(2026, 8, 19), application_start=datetime(2026, 8, 19, 16), application_end=datetime(2026, 8, 31, 18))
    new = row(session, "new-city", title, region_code="深圳市", housing_types=["baozu"],
        published=datetime(2026, 9, 19), application_start=datetime(2026, 9, 19, 16), application_end=datetime(2026, 9, 30, 18))
    rebuild_links(session)
    snap = build_snapshot(session, "digest", now=NOW)
    cards = {c.batch_id: c for c in snap.cards}
    assert set(cards) == {old.id, new.id}
    assert cards[old.id].end == old.application_end and cards[new.id].end == new.application_end
    assert cards[old.id].notice_ids == [old.id]


def test_old_missing_launch_is_refreshed_and_source_filter_precedes_limit(session, source, city_source, monkeypatch):
    monkeypatch.setattr("app.housing.scan.utcnow", lambda: NOW)
    missing = row(session, "old-missing", SAMPLES["1316"]["title"], published=datetime(2026, 8, 19),
        body_text=None, detail_status="pending", source_id=city_source.id, application_start=None, application_end=None)
    for i in range(35):
        row(session, f"other-{i}", LUOHU_LAUNCH + str(i), source_id=source.id,
            published=NOW - timedelta(days=1), body_text=None, detail_status="pending")
    due = detail_refresh_candidates(session, {city_source.id}, now=NOW, limit=1)
    assert [n.id for n in due] == [missing.id]
    assert _should_fetch_detail(missing, SimpleNamespace(published_at=missing.published_at), NOW - timedelta(days=45))
    missing.detail_checked_at = NOW
    session.flush()
    assert not detail_refresh_candidates(session, {city_source.id}, now=NOW)


def test_empty_detail_response_is_not_success(session, source):
    notice = row(session, "empty", LUOHU_LAUNCH, body_text=None, detail_status="pending", source_id=source.id)
    class Client:
        async def get(self, url, **kwargs):
            return SimpleNamespace(text="<html><body></body></html>", url=url)
    assert asyncio.run(_hydrate_detail(session, Client(), source, notice, notice.canonical_url)) is None
    assert notice.detail_status != "ok" and notice.body_text is None


def test_conflicting_publicity_periods_are_not_arbitrarily_selected():
    text = "公示期为2026年10月22日至2026年10月28日。公示时间为2026年10月23日至2026年10月29日。"
    info = extract_review_info(text, datetime(2026, 10, 21))
    assert info.start is None and info.end is None


def test_missing_details_are_visible_in_profile_not_hidden_as_a_verified_window(session):
    row(session, "unfetched", SAMPLES["1316"]["title"], body_text=None, detail_status="pending",
        application_start=None, application_end=None)
    card, = build_snapshot(session, "digest", now=NOW).cards
    assert any("详情尚未采集" in issue for issue in card.profile.issues)
    assert card.stage == "unverified_window" and not card.is_open


@pytest.mark.parametrize("heading", ["一、本次配租对象", "一、本批次配租对象"])
def test_object_section_excludes_intro_projects_and_later_exceptions(session, heading):
    body = ("网站首页 智能机器人\n本次认租时间为2026年9月7日至18日。\n" + heading + "\n"
        "本次配租对象需同时满足以下两项条件：\n（一）2026年9月4日前在深圳市保障性住房轮候系统登记为宝安区户籍的在册轮候人。\n"
        "（二）符合收入财产限额，但低保家庭除外。\n二、房源概况\n本次面向在册轮候人供应100套房源，位于某小区。")
    n = row(session, "objects", LUOHU_LAUNCH, body_text=body)
    profile = allocation_profile(n)
    assert all("认租时间" not in x and "某小区" not in x and "网站首页" not in x for x in profile.object_text)
    assert any("宝安区户籍" in x for x in profile.object_text)
    assert "（二）符合收入财产限额，但低保家庭除外" in profile.qualifications


def test_explicit_enterprise_target_is_distinct_from_employee_household_condition(session):
    n = row(session, "company-object", "光明区住房和建设局关于面向光明区企事业单位配租保障性租赁住房的通告",
        housing_types=["baozu"], body_text="三、配租对象及顺序\n本批次住房配租对象为符合政府组织配租的保障性租赁住房条件的企事业单位。\n四、员工条件\n申请人组建家庭的，应当以家庭为单位申请。")
    assert allocation_profile(n).audience == "enterprise"


def test_city_talent_is_personal_only_with_explicit_household_application_evidence(session):
    page = SAMPLES["1316"]
    n = row(session, "talent", page["title"], body_text=page["body_text"], housing_types=["baozu"], region_code="深圳市")
    assert allocation_profile(n).audience == "personal"


def test_selection_round_alone_cannot_become_allocation_batch_two(session):
    assert parse_batch_seq("2025年面向个人配租住房第二批次选房结果") is None
    assert parse_batch_seq("2025年第二批面向个人配租住房第三批次选房结果") == 2
    other = row(session, "allocation-two", "罗湖区2025年第二批面向个人配租公共租赁住房的通告", published=datetime(2025, 8, 1))
    node = row(session, "round-two", "罗湖区2025年面向个人配租公共租赁住房第二批次选房结果", category="process", published=datetime(2025, 8, 30))
    rebuild_links(session)
    assert other.batch_key_v2 and node.batch_key_v2 is None


def test_reparsing_does_not_drop_the_official_wps_authorization_attachment():
    from app.housing.parsers.detail import parse_detail
    result = parse_detail('<article><h1>初审结果</h1><p>附件：</p><a href="/approval.pdf">初审名单.pdf</a><a href="/authorization.wps">授权委托书模板.wps</a></article>', 'https://example.gov.cn/notice.html')
    assert {a["url"] for a in result.attachments} == {"https://example.gov.cn/approval.pdf", "https://example.gov.cn/authorization.wps"}


def test_conflicting_reference_cannot_fall_back_to_a_personal_batch(session):
    personal = row(session, "individual-batch", "罗湖区2026年第一批面向个人配租公共租赁住房的通告")
    company = row(session, "company-batch", "罗湖区2026年第一批面向企事业单位配租公共租赁住房的通告")
    node = row(session, "conflicting-node", "罗湖区2026年第一批面向个人配租公共租赁住房初审结果", category="process",
        published=datetime(2026, 10, 21), body_text="依据《" + company.title + "》办理。")
    rebuild_links(session)
    assert personal.batch_key_v2 and node.batch_key_v2 is None and node.batch_link_status == "ambiguous"


def test_missing_start_date_cannot_be_counted_as_verified_open_applications(session):
    row(session, "missing-start", LUOHU_LAUNCH, application_start=None, application_end=NOW + timedelta(days=5))
    snapshot = build_snapshot(session, "digest", now=NOW)
    card, = snapshot.cards
    assert not card.is_open and card.stage == "unverified_window"
    assert card.in_transit and snapshot.totals.open_batches == 0


def test_resolved_link_and_existing_lottery_stat_move_together(session):
    from app.housing.models import HousingLotteryStat
    launch = row(session, "stat-launch", LUOHU_LAUNCH)
    sample = SAMPLES["2265"]
    node = row(session, "stat-node", sample["title"], category="process", body_text=sample["body_text"], published=datetime(2026, 10, 21))
    stat = HousingLotteryStat(notice_id=node.id, batch_key="", category_key="all")
    session.add(stat); session.flush()
    rebuild_links(session)
    session.refresh(stat)
    assert stat.batch_key == node.batch_key_v2 == launch.batch_key_v2
