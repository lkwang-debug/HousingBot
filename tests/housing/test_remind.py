from __future__ import annotations

from datetime import datetime, timedelta

from app.housing.models import HousingNotice
from app.housing.remind import collect_reminders
from app.housing.render import render_deadline, render_stage

NOW = datetime(2026, 10, 20, 9, 0)


def _notice(**overrides) -> HousingNotice:
    payload = dict(
        canonical_key=overrides.get("canonical_key", "k1"),
        canonical_url="https://e.gov.cn/1.html",
        url_hash="h1",
        title="南山区2026年度面向户籍在册轮候人配租住房的通告",
        title_norm="x",
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["gongzu"],
        region_code="南山区",
        published_at=NOW - timedelta(days=6),
        first_seen_at=NOW - timedelta(days=6),
        application_start=NOW - timedelta(days=6),
        application_end=NOW + timedelta(days=2),
        units_count=616,
        projects=["龙悦居三期"],
    )
    payload.update(overrides)
    return HousingNotice(**payload)


def test_deadline_reminder_within_three_days(session):
    notice = _notice()
    session.add(notice)
    session.flush()

    items = collect_reminders(session, NOW)
    assert [item.kind for item in items] == ["deadline"]
    assert items[0].days_left == 2
    assert items[0].base_key.endswith(":3d")


def test_deadline_reminder_turns_into_one_day(session):
    session.add(_notice(application_end=NOW + timedelta(hours=20)))
    session.flush()
    items = collect_reminders(session, NOW)
    assert items[0].base_key.endswith(":1d")


def test_far_deadline_is_not_reminded(session):
    session.add(_notice(application_end=NOW + timedelta(days=9)))
    session.flush()
    assert collect_reminders(session, NOW) == []


def test_expired_notice_is_not_reminded(session):
    session.add(_notice(application_end=NOW - timedelta(days=1)))
    session.flush()
    assert collect_reminders(session, NOW) == []


def test_stage_events_are_no_longer_a_separate_reminder(session):
    """阶段推进改成"该通告入库就按即时消息推"，不再由提醒任务另发一份。

    两套并行会产生两个去重键，同一件事进群两遍；所以这里固定住"提醒只负责截止"。
    """
    session.add(
        _notice(
            canonical_key="k2",
            url_hash="h2",
            category="process",
            title="南山区关于2026年度配租住房选房名单及选房签约的通告",
            published_at=NOW - timedelta(days=2),
            first_seen_at=NOW - timedelta(days=2),
            application_start=None,
            application_end=None,
        )
    )
    session.flush()
    from app.housing.batches import build_batches

    assert collect_reminders(session, NOW) == []
    # 但批次进度本身仍然算得出来
    assert build_batches(session, now=NOW)[0].stage == "selection"


def test_stage_render_shows_next_step():
    notice = _notice(category="process", title="南山区配租住房选房名单及选房签约的通告")
    from app.housing.batches import Batch

    batch = Batch(
        key="南山区|2026|1|gongzu",
        region="南山区",
        title="南山区2026年度配租住房的通告",
        stage="signing",
        stage_notice=notice,
        launch=_notice(),
    )
    title, content = render_stage(batch, notice, scope="南山区 · 管家版")
    assert "选房签约" in content
    assert "逾期视为放弃" in content
    assert "订阅范围：南山区 · 管家版" in content
    assert notice.canonical_url in content


def test_deadline_render_mentions_remaining_time():
    notice = _notice()
    title, content = render_deadline(notice, 2, now=NOW)
    assert "2 天后" in title
    assert "616 套" in content
    assert "10-22 09:00" in content


def test_deadline_title_does_not_repeat_the_region():
    # 原文标题以区名开头，标题前面再加一次就是「南山区 南山区2026年度…」。
    notice = _notice(title="深圳市南山区住房和建设局关于2026年度配租住房的通告")
    title, _ = render_deadline(notice, 1, now=NOW, scope="深圳 · 管家版")
    assert title.startswith("⏰ 明天截止 · 深圳市南山区住房和建设局")
    assert "南山区 深圳市南山区" not in title


def test_stage_title_does_not_repeat_the_region():
    from app.housing.batches import Batch

    notice = _notice(category="process", title="南山区配租住房选房签约的通告")
    batch = Batch(
        key="南山区|2026|1|gongzu",
        region="南山区",
        title="南山区2026年度配租住房的通告",
        stage="signing",
        stage_notice=notice,
        launch=_notice(),
    )
    title, _ = render_stage(batch, notice, scope="深圳 · 管家版", now=NOW)
    assert title.startswith("深圳 · 管家版 · 📌 【签约】")
    assert "南山区 【签约】南山区" not in title


def test_stage_title_adds_region_when_the_notice_omits_it():
    from app.housing.batches import Batch

    notice = _notice(category="process", title="保障性租赁住房选房签约有关事项的通告")
    batch = Batch(
        key="光明区|2026|1|baozu",
        region="光明区",
        title="光明区2026年第一批次配租通告",
        stage="selection",
        stage_notice=notice,
        launch=_notice(),
    )
    title, _ = render_stage(batch, notice, scope="深圳 · 管家版", now=NOW)
    assert "光明区" in title


def test_duplicate_urls_of_same_notice_remind_once(session):
    """同一份通告挂在两个栏目下会落成两行，提醒只能发一条。"""
    session.add(_notice(canonical_key="a", title_norm="坪山第三批次配租通告"))
    session.add(
        _notice(
            canonical_key="b",
            url_hash="h2",
            canonical_url="https://e.gov.cn/2.html",
            title_norm="坪山第三批次配租通告",
        )
    )
    session.flush()

    items = collect_reminders(session, NOW)
    assert len(items) == 1
    assert items[0].notice.id < 10**9
