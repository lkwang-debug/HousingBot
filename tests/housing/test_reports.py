from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.housing.batches import rebuild_links
from app.housing.events import record_revision
from app.housing.groups import SubscribeGroup
from app.housing.models import HousingMeta, HousingNotice, HousingOutbox, HousingOutboxEvent
from app.housing.outbox import enqueue
from app.housing.report_render import render_report_parts
from app.housing.reporting import allocation_profile, build_snapshot
from app.housing.scheduled_reports import prepare_report, report_due, report_key, send_report

NOW = datetime(2026, 10, 21, 21, 0)


def notice(session, key, *, region="光明区", kind="baozu", audience="personal", published=None, **overrides):
    org = "深圳市住房保障署" if region == "深圳市" else region + "住房和建设局"
    label = "保障性租赁住房" if kind == "baozu" else "公共租赁住房"
    target = "面向个人" if audience == "personal" else "面向企事业单位" if audience == "enterprise" else ""
    payload = dict(canonical_key=key, canonical_url=f"https://example.gov.cn/{key}.html", url_hash=key,
        title=f"{org}关于2026年第一批次{target}配租{label}的通告（{key}项目）",
        title_norm="", region_code=region, level="city" if region == "深圳市" else "district",
        category="rent_launch", housing_types=[kind], is_target=True, is_push_candidate=True,
        published_at=published or NOW - timedelta(hours=2), first_seen_at=NOW - timedelta(hours=2),
        event_at=NOW - timedelta(hours=2), business_version="v1", projects=[], attachments=[],
        application_start=NOW - timedelta(days=3), application_end=NOW + timedelta(days=1),
        body_text="一、配租对象\n本批次住房面向个人配租。\n二、申请时间\n见原文。" if audience == "personal" else
                  "一、配租对象\n本次配租面向企事业单位，由单位统一申请。\n二、员工条件\n员工需签订有效劳动合同。" if audience == "enterprise" else "")
    payload.update(overrides)
    row = HousingNotice(**payload)
    session.add(row); session.flush()
    return row


def link(session, launch, node):
    rebuild_links(session)
    node.batch_key_v2 = launch.batch_key_v2
    node.batch_link_status = "manual"
    session.flush()


def test_four_views_overlap_but_total_and_detail_do_not(session):
    district = notice(session, "district")
    city = notice(session, "city", region="深圳市", audience="enterprise")
    public = notice(session, "public", kind="gongzu", region="坪山区")
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.totals.open_batches == 3
    assert snapshot.categories["district_baozu"].open_batches == 1
    assert snapshot.categories["personal_baozu"].open_batches == 1
    assert snapshot.categories["city_baozu"].open_batches == 1
    assert snapshot.categories["enterprise_baozu"].open_batches == 1
    assert snapshot.categories["gongzu"].open_batches == 1
    content = "\n".join(p.content for p in render_report_parts(snapshot))
    for row in (district, city, public):
        assert content.count(f"｜B{row.id}】") == 1
    assert content.index("### 一、市级配租") < content.index("### 二、各区配租")
    assert "四类不能直接相加" in content


def test_city_website_repost_does_not_change_organizer(session):
    row = notice(session, "repost", level="city", region_code="深圳市")
    profile = allocation_profile(row)
    assert profile.level == "district"
    assert profile.region == "光明区"


def test_city_project_in_district_is_not_repeated(session):
    row = notice(session, "city-location", region="深圳市", body_text="本次项目位于光明区。")
    profile = allocation_profile(row)
    assert profile.level == "city"
    assert profile.project_regions == ("光明区",)
    content = "\n".join(p.content for p in render_report_parts(build_snapshot(session, "digest", now=NOW)))
    assert content.count(f"｜B{row.id}】") == 1
    assert "项目所在地：光明区" in content


def test_unknown_and_conflicting_audience_is_not_assigned(session):
    row = notice(session, "unknown", audience="unknown", title="光明区住房和建设局关于某项目保租房配租的通告", body_text="工作人员联系企业办理资料。")
    assert allocation_profile(row).audience == "unknown"
    row.body_text = "一、配租对象\n本次配租面向企事业单位。\n二、流程\n见原文。"
    row.title = "光明区住房和建设局关于面向个人配租保租房的通告"
    profile = allocation_profile(row)
    assert profile.audience == "unknown"
    assert any("冲突" in issue for issue in profile.issues)


def test_qualification_keeps_negations_exceptions_and_no_navigation(session):
    row = notice(session, "negation", body_text="首页 政务服务 智能机器人\n一、配租对象\n本次配租面向个人。不限户籍，不要求社保满5年，但特殊项目除外。\n二、申请方式\n查看原文。")
    profile = allocation_profile(row)
    assert "不限户籍，不要求社保满5年，但特殊项目除外" in profile.qualifications
    assert not any("首页" in text for text in profile.object_text + profile.qualifications)
    assert profile.audience == "personal"


def test_missing_heading_still_excludes_site_chrome_from_qualifications(session):
    row = notice(session, "site-chrome", body_text="网站首页 智能机器人 无障碍浏览 分享到：根据轮候规定现开展配租。不限户籍，但特殊项目除外。")
    profile = allocation_profile(row)
    assert not any("网站首页" in s or "分享到" in s for s in profile.qualifications + profile.object_text)
    assert "不限户籍，但特殊项目除外" in profile.qualifications


def test_anchored_node_inherits_original_objects_and_full_history(session):
    old = notice(session, "old", published=NOW - timedelta(days=500), application_end=NOW - timedelta(days=400))
    review = notice(session, "review", category="process", title="光明区住房和建设局关于保租房资格核查结果的公示", published=NOW - timedelta(days=300), application_end=None)
    selection = notice(session, "selection", category="process", title="光明区住房和建设局关于本批选房通知", application_end=None)
    link(session, old, review); link(session, old, selection)
    snapshot = build_snapshot(session, "digest", now=NOW)
    assert len(snapshot.cards) == 1
    card = snapshot.cards[0]
    assert card.code == f"B{old.id}" and card.stage == "selection"
    assert card.profile.audience == "personal"
    assert {r.id for r in card.references} == {old.id, review.id, selection.id}
    assert any(label == "资格核查" and state == "已有官方公告" for label, state in card.timeline)
    assert not any(label == "摇号" for label, _ in card.timeline)


def test_closed_window_waits_for_next_official_step(session):
    notice(session, "ended", application_end=NOW - timedelta(days=1))
    card = build_snapshot(session, "digest", now=NOW).cards[0]
    assert not card.is_open and card.stage == "waiting"
    assert "认租已截止" in dict(card.timeline)["认租"]


def test_missing_window_does_not_claim_not_started(session):
    notice(session, "missing-window", application_start=None, application_end=None)
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.cards[0].stage == "unverified_window"
    assert not snapshot.cards[0].is_open


def test_future_date_node_does_not_advance_stage(session):
    root = notice(session, "root-future", application_end=NOW - timedelta(days=1))
    future = notice(session, "future", published=NOW + timedelta(days=1), category="process",
        title="光明区住房和建设局关于保租房选房通知", application_end=None)
    link(session, root, future)
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.cards[0].stage == "waiting"
    assert snapshot.totals.selection_batches == 0
    assert any("日期待核实" in issue for issue in snapshot.cards[0].profile.issues)


def test_preview_does_not_advance_flow_or_count_lottery_result(session):
    old = notice(session, "launch", application_end=NOW - timedelta(days=1))
    preview = notice(session, "preview", title="光明区住房和建设局关于后续摇号及选房工作预告", category="process", application_end=None)
    link(session, old, preview)
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.cards[0].stage == "waiting"
    assert snapshot.totals.lottery_batches == snapshot.totals.selection_batches == 0


def test_unlinked_node_stays_notice_not_fake_batch(session):
    row = notice(session, "orphan", category="process", title="光明区住房和建设局关于保租房选房通知", application_end=None)
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.cards[0].code == f"N{row.id}"
    assert snapshot.totals.selection_batches == 0
    assert snapshot.totals.new_notices == 1


def test_exact_duplicates_count_once_but_distinct_documents_do_not_merge(session):
    first = notice(session, "first", title="光明区住房和建设局关于保租房面向个人配租的通告（住保租〔2026〕001号）", doc_number="住保租〔2026〕001号")
    copy = notice(session, "copy", title=first.title, doc_number=first.doc_number, published=first.published_at + timedelta(seconds=30))
    assert build_snapshot(session, "evening", now=NOW).totals.open_batches == 1
    notice(session, "different", title=first.title.replace("001号", "002号"), doc_number="住保租〔2026〕002号")
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.totals.open_batches == 2 and snapshot.totals.new_notices == 2


def test_multiple_lottery_notices_count_one_batch(session):
    root = notice(session, "root", published=NOW - timedelta(days=10))
    first = notice(session, "lottery1", category="lottery_result", title="光明区住房和建设局关于第一份保租房公证摇号结果", application_end=None)
    second = notice(session, "lottery2", category="lottery_result", title="光明区住房和建设局关于第二份保租房公证摇号结果", application_end=None)
    link(session, root, first); link(session, root, second)
    snapshot = build_snapshot(session, "evening", now=NOW)
    assert snapshot.totals.lottery_batches == 1
    assert snapshot.totals.new_notices == 2
    content = "\n".join(p.content for p in render_report_parts(snapshot))
    assert first.canonical_url in content and second.canonical_url in content


def test_real_revision_counted_but_backfill_fetch_timestamp_not_counted(session):
    old = notice(session, "old", published=NOW - timedelta(days=15), first_seen_at=NOW - timedelta(days=10), event_at=NOW - timedelta(hours=1))
    new = notice(session, "backfill", published=NOW - timedelta(days=300), first_seen_at=NOW - timedelta(minutes=1), event_at=NOW - timedelta(minutes=1), application_end=NOW - timedelta(days=100))
    assert build_snapshot(session, "evening", now=NOW).totals.updated_notices == 0
    baseline=prepare_report(session,'evening',NOW-timedelta(hours=2))
    baseline['key']='evening:previous'
    old.business_version = "revised"
    old.application_end+=timedelta(days=1)
    record_revision(session, old, "v1", at=NOW - timedelta(hours=1), reason="official-detail-changed")
    session.flush()
    snap = build_snapshot(session, "evening", now=NOW,baseline=baseline)
    assert snap.totals.updated_notices == 1 and snap.totals.new_notices == 0
    assert f"B{new.id}" not in {c.code for c in snap.cards}


def test_morning_and_evening_windows_and_shanghai_timezone(session):
    now = datetime(2026, 10, 21, 8, 30)
    notice(session, "yesterday", published=datetime(2026, 10, 20, 12))
    notice(session, "today", published=datetime(2026, 10, 21, 6))
    assert build_snapshot(session, "digest", now=now).totals.new_notices == 2
    assert build_snapshot(session, "evening", now=now).totals.new_notices == 2  # First report uses 24h, then accepted-report deltas.
    utc = datetime(2026, 10, 21, 0, 30, tzinfo=timezone.utc)
    assert build_snapshot(session, "digest", now=utc).generated_at == now


def test_mixed_housing_only_in_total_not_both_subtotals(session):
    notice(session, "mixed", title="光明区住房和建设局关于保租房与公租房混合项目配租通告", housing_types=["baozu", "gongzu"])
    snap = build_snapshot(session, "evening", now=NOW)
    assert snap.totals.open_batches == 1
    assert snap.categories["district_baozu"].open_batches == snap.categories["gongzu"].open_batches == 0


@pytest.mark.parametrize("kind", ["digest", "evening"])
def test_daily_report_is_one_payload_without_losing_batches(session, kind):
    for i in range(25):
        notice(session, f"many{i}", body_text="一、配租对象\n本次配租面向个人。社保不要求满5年，但特殊项目除外。\n二、其他\n见原文。")
    snapshot = build_snapshot(session, kind, now=NOW)
    parts = render_report_parts(snapshot)
    assert snapshot.totals.open_batches == 25
    assert len(parts) == 1
    assert all(len(p.content) <= 18000 for p in parts)
    assert all(len(p.content.encode("utf-8")) <= 18000 for p in parts)
    joined = "\n".join(p.content for p in parts)
    for card in snapshot.cards:
        assert joined.count(f"｜{card.code}】") == 1
    assert "认租中25批" in joined and "已截断" not in joined
    assert "部分" not in joined and "（续）" not in joined
    assert set(parts[0].batch_codes) == {c.code for c in snapshot.cards}
    assert set(parts[0].notice_ids) == {i for c in snapshot.cards for i in c.notice_ids}


def test_full_export_preserves_complete_chinese_clauses_when_they_fit(session):
    from dataclasses import replace
    from app.housing.report_render import _single_daily_report
    row = notice(session, "long-card")
    snap = build_snapshot(session, "evening", now=NOW)
    clauses = tuple(f"条件{i}：" + "户籍社保例外条件" * 130 for i in range(3))
    snap.cards[0].profile = replace(snap.cards[0].profile, qualifications=clauses,
        object_text=("个人对象：" + "住房申请条件" * 200,))
    parts = [_single_daily_report(snap, max_chars=18000, max_bytes=18000)]
    assert len(parts) == 1
    assert all(len(p.content.encode("utf-8")) <= 18000 for p in parts)
    joined = "\n".join(p.content for p in parts)
    for clause in clauses: assert clause in joined
    assert joined.count(f"｜B{row.id}】") == 1


def test_long_qualification_uses_original_link_not_a_cut_negation(session):
    from dataclasses import replace
    row = notice(session, "long-qualification")
    snap = build_snapshot(session, "evening", now=NOW)
    clause = "不限户籍、不要求社保满五年，但特殊项目除外。" + "完整资格条件及例外" * 1000
    snap.cards[0].profile = replace(snap.cards[0].profile, qualifications=(clause,))
    part, = render_report_parts(snap)
    assert "完整条件及否定／例外见官方原文" in part.content
    assert clause not in part.content and "不限户籍" not in part.content
    assert row.canonical_url in part.content
    assert "对象概要：个人" in part.content
    assert clause == snap.audit_dict()["cards"][0]["profile"]["qualifications"][0]


def test_capacity_failure_never_sends_partial_or_split_daily_report(session, monkeypatch, tmp_path):
    for i in range(100):
        notice(session, f"capacity-{i}")
    session.commit()
    _, calls = sender_setup(session, monkeypatch, tmp_path)
    assert build_snapshot(session, "evening", now=NOW).totals.open_batches == 100
    with pytest.raises(ValueError, match="未截断、未拆条"):
        asyncio.run(send_report("evening", now=NOW))
    assert not calls
    with sessionmaker(bind=session.bind)() as check:
        assert not check.scalar(select(HousingOutbox))
        assert not check.scalar(select(HousingMeta).where(HousingMeta.key == "report:evening:2026-10-21"))


@pytest.mark.parametrize("kind", ["digest", "evening"])
def test_many_batches_make_one_api_request_per_group_and_leave_tomorrow_free(session, monkeypatch, tmp_path, kind):
    for i in range(25):
        # Morning replay must use announcements already published and observed.
        observed = NOW.replace(hour=8)
        notice(session, f"once-{i}", published=observed, first_seen_at=observed, event_at=observed)
    session.commit()
    groups = [SubscribeGroup(name=x, id=x, topic=x, tier="pro") for x in ("A", "B", "C")]
    _, calls = sender_setup(session, monkeypatch, tmp_path, groups)
    now = NOW.replace(hour=9) if kind == "digest" else NOW
    result = asyncio.run(send_report(kind, now=now))
    assert result["queued"] == 3 and result["parts"] == 1
    assert len(calls) == 3 and len({content for _, _, content in calls}) == 1
    for _, _, content in calls:
        assert all(f'B{n}' in content for n in range(1,26))
    with sessionmaker(bind=session.bind)() as check:
        assert not check.scalar(select(HousingOutboxEvent).where(HousingOutboxEvent.event_key == f"{kind}:2026-10-22"))


def test_snapshot_build_is_read_only(session):
    notice(session, "readonly"); session.commit()
    before = [(n.id, n.business_version, n.event_at, n.batch_key_v2) for n in session.scalars(select(HousingNotice))]
    snap = build_snapshot(session, "evening", now=NOW)
    render_report_parts(snap)
    assert not session.new and not session.dirty
    assert before == [(n.id, n.business_version, n.event_at, n.batch_key_v2) for n in session.scalars(select(HousingNotice))]
    assert list(session.scalars(select(HousingMeta))) == []


def sender_setup(session, monkeypatch, tmp_path, groups=None, outcomes=None):
    groups = groups or [SubscribeGroup(name="总群", topic="one", tier="pro", id="one")]
    calls = []
    async def submit(delivery, title, content, topic):
        calls.append((topic, title, content))
        delivery.status = (outcomes or {}).get(topic, "submitted")
        delivery.response_payload = {"retryable": delivery.status == "failed"}
    sender = SimpleNamespace(settings=SimpleNamespace(pushplus_token="fake"), _submit=submit, close=AsyncMock())
    monkeypatch.setattr("app.housing.scheduled_reports.SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False, autoflush=False))
    monkeypatch.setattr("app.housing.groups.load_groups", lambda: groups)
    monkeypatch.setattr("app.housing.delivery.HousingPushClient", lambda: sender)
    monkeypatch.setattr("app.housing.outbox.get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr("app.housing.outbox._account_rate_limit", AsyncMock())
    return groups, calls


def test_report_due_boundaries():
    assert not report_due("digest", NOW.replace(hour=8, minute=29))
    assert report_due("digest", NOW.replace(hour=8, minute=30))
    assert report_due("digest", NOW.replace(hour=12, minute=0))
    assert not report_due("digest", NOW.replace(hour=12, minute=1))
    assert not report_due("evening", NOW.replace(hour=20, minute=29))
    assert report_due("evening", NOW.replace(hour=20, minute=30))
    assert report_due("evening", NOW.replace(hour=23, minute=59, second=59))


def test_zero_news_evening_is_sent_once_per_day(session, monkeypatch, tmp_path):
    _, calls = sender_setup(session, monkeypatch, tmp_path)
    first = asyncio.run(send_report("evening", now=NOW))
    second = asyncio.run(send_report("evening", now=NOW + timedelta(minutes=5)))
    assert first["queued"] == 1 and second["queued"] == 0
    assert len(calls) == 1 and "未发现已核实" in calls[0][2]


def test_early_reminder_does_not_consume_evening_and_new_reminders_reserved(session, monkeypatch, tmp_path):
    row = notice(session, "deadline"); session.commit()
    groups, calls = sender_setup(session, monkeypatch, tmp_path)
    from app.housing.remind import collect_reminders
    reminder = collect_reminders(session, NOW)[0]
    prior = enqueue(session, groups[0], "deadline", "earlier", "earlier", [(reminder.base_key, row.id)])
    prior.status = "submitted"; session.commit()
    result = asyncio.run(send_report("evening", now=NOW))
    assert result["queued"] == 1 and len(calls) == 1
    assert "明天" in calls[0][2]
    assert session.scalar(select(HousingOutboxEvent).where(HousingOutboxEvent.kind == "evening", HousingOutboxEvent.event_key == report_key("evening", NOW)))


def test_new_evening_deadline_reserves_standalone_reminder_event(session, monkeypatch, tmp_path):
    row = notice(session, "deadline"); session.commit()
    sender_setup(session, monkeypatch, tmp_path)
    asyncio.run(send_report("evening", now=NOW))
    event = session.scalar(select(HousingOutboxEvent).where(HousingOutboxEvent.kind == "deadline"))
    assert event and event.notice_id == row.id
    assert session.get(HousingOutbox, event.outbox_id).kind == "evening"


def test_before_slot_skips_and_test_namespace_never_claims_formal_keys(session, monkeypatch, tmp_path):
    row = notice(session, "real"); session.commit()
    _, calls = sender_setup(session, monkeypatch, tmp_path)
    result = asyncio.run(send_report("evening", now=NOW.replace(hour=19)))
    assert result["status"] == "skipped" and not calls
    for kind in ("instant", "digest", "evening"):
        result = asyncio.run(send_report(kind, now=NOW, force=True, test_label="排版验收", test_notices=[row] if kind == "instant" else None))
        assert result["queued"] >= 1
    assert len(calls) == 3
    assert {event.kind for event in session.scalars(select(HousingOutboxEvent))} == {"test"}
    assert {job.kind for job in session.scalars(select(HousingOutbox))} == {"test"}
    assert not session.scalar(select(HousingMeta).where(HousingMeta.key == "report:evening:2026-10-22"))
    assert not session.scalar(select(HousingMeta).where(HousingMeta.key == "report:digest:2026-10-22"))


def test_frozen_snapshot_reused_for_multiple_groups_and_failed_retry(session, monkeypatch, tmp_path):
    groups = [SubscribeGroup(name=x, id=x, topic=x, tier="pro") for x in ("A", "B", "C")]
    outcomes = {"B": "failed"}
    _, calls = sender_setup(session, monkeypatch, tmp_path, groups, outcomes)
    first = asyncio.run(send_report("evening", now=NOW))
    assert first["queued"] == 3
    assert len({content for _, _, content in calls}) == 1
    failed = session.scalar(select(HousingOutbox).where(HousingOutbox.group_id == "B"))
    frozen = failed.content
    notice(session, "later", published=NOW + timedelta(minutes=1))
    failed.next_attempt_at = NOW + timedelta(minutes=1); session.commit()
    outcomes["B"] = "submitted"
    second = asyncio.run(send_report("evening", now=NOW + timedelta(minutes=2)))
    assert second["queued"] == 0
    assert len(calls) == 4 and calls[-1][0] == "B" and calls[-1][2] == frozen


def test_existing_schema_two_report_is_reused_without_compact_regeneration(session, monkeypatch, tmp_path):
    row = notice(session, "old-layout"); session.commit()
    _, calls = sender_setup(session, monkeypatch, tmp_path)
    frozen = {"schema": 2, "layout": "single_daily", "generated_at": NOW.isoformat(),
        "parts": [{"title": "旧版已冻结晚报", "content": "旧版完整正文，不得重写",
                   "notice_ids": [row.id], "batch_codes": [f"B{row.id}"]}], "audit": {}}
    key = "report:" + report_key("evening", NOW)
    session.add(HousingMeta(key=key, value=frozen)); session.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("A previously frozen report must never be regenerated")
    monkeypatch.setattr("app.housing.scheduled_reports.prepare_report", forbidden)
    first = asyncio.run(send_report("evening", now=NOW))
    second = asyncio.run(send_report("evening", now=NOW + timedelta(minutes=1)))
    assert first["queued"] == 1 and second["queued"] == 0
    assert calls == [("one", frozen["parts"][0]["title"], frozen["parts"][0]["content"])]
    with sessionmaker(bind=session.bind)() as check:
        assert check.get(HousingMeta, key).value == frozen


def test_compact_freeze_records_layout_decisions_and_all_notice_memberships(session):
    row = notice(session, "new-layout")
    report = prepare_report(session, "digest", NOW, test_label="验收")
    assert report["schema"] == 4 and report["layout"] == "daily_changes_v4"
    assert report['parts'][0]['wire_html']
    assert report["presentation"][0]["detail_level"] == "focus"
    assert report["presentation"][0]["code"] == f"B{row.id}"
    assert report["parts"][0]["notice_ids"] == [row.id]
    assert report["audit"]["cards"][0]["profile"]["object_text"]
    assert len(report["parts"][0]["content"].encode("utf-8")) <= 18000


def test_acceptance_send_never_flushes_or_cancels_tomorrows_formal_job(session, monkeypatch, tmp_path):
    groups, calls = sender_setup(session, monkeypatch, tmp_path)
    tomorrow = enqueue(session, groups[0], "digest", "明天正式早报", "do not touch",
                       [(report_key("digest", NOW + timedelta(days=1)), None)])
    tomorrow.created_at = NOW + timedelta(days=1)
    session.commit()
    before = [(e.kind, e.event_key, e.outbox_id) for e in session.scalars(select(HousingOutboxEvent))]
    asyncio.run(send_report("evening", now=NOW, force=True, test_label="排版验收"))
    session.refresh(tomorrow)
    assert tomorrow.status == "pending" and tomorrow.attempts == 0
    assert len(calls) == 1 and "排版验收" in calls[0][1]
    assert before == [(e.kind, e.event_key, e.outbox_id) for e in session.scalars(select(HousingOutboxEvent).where(HousingOutboxEvent.kind != "test"))]


def test_expired_evening_does_not_send_next_day(session, monkeypatch, tmp_path):
    groups, calls = sender_setup(session, monkeypatch, tmp_path)
    from app.housing.groups import sync_groups
    from app.housing.outbox import flush_outbox
    sync_groups(session, NOW)
    job = enqueue(session, groups[0], "evening", "old", "old", [("evening:yesterday", None)])
    job.created_at = NOW - timedelta(days=1); session.commit()
    asyncio.run(flush_outbox(session, now=NOW, rate_limit=False, lock_path=tmp_path / "send.lock"))
    session.refresh(job)
    assert job.status == "cancelled" and not calls
