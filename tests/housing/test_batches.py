from __future__ import annotations

from datetime import datetime, timedelta

from app.housing.batches import batch_key, build_batches, detect_stage, parse_batch_seq, rebuild_links
from app.housing.models import HousingNotice

NOW = datetime(2026, 10, 20, 9, 0)


def _notice(**overrides) -> HousingNotice:
    payload = dict(
        canonical_key=overrides.get("canonical_key", "k1"),
        canonical_url="https://e.gov.cn/1.html",
        url_hash="h1",
        title="光明区住房和建设局关于2026年第一批次面向个人配租保障性租赁住房的通告",
        title_norm="x",
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["baozu"],
        region_code="光明区",
        level="district",
        published_at=NOW - timedelta(days=20),
        first_seen_at=NOW - timedelta(days=20),
        application_start=NOW - timedelta(days=22),
        application_end=NOW - timedelta(days=8),
    )
    payload.update(overrides)
    return HousingNotice(**payload)


def test_parse_batch_seq():
    assert parse_batch_seq("2026年第一批次配租") == 1
    assert parse_batch_seq("2026年度第三批次") == 3
    assert parse_batch_seq("第11批次") == 11
    assert parse_batch_seq("第十二批次") == 12
    assert parse_batch_seq("配租通告") is None


def test_launch_is_its_own_anchor():
    launch = _notice()
    key = batch_key(launch)
    assert key and key.startswith("v2:")


def test_unlinked_notice_has_no_key():
    # 批次只认落库的关联结论，不再靠"区＋年＋批号"现场猜一个出来。
    notice = _notice(category="process", title="光明区关于2026年第一批次配租保障性租赁住房公证摇号结果的通告")
    assert batch_key(notice) is None


def test_same_batch_across_stages_shares_key(session):
    launch = _notice()
    lottery = _notice(
        canonical_key="k2",
        url_hash="h2",
        category="process",
        title="光明区关于2026年第一批次面向个人配租保障性租赁住房公证摇号结果的通告",
        published_at=NOW - timedelta(days=4),
        first_seen_at=NOW - timedelta(days=4),
    )
    session.add_all([launch, lottery])
    session.flush()

    rebuild_links(session)
    assert lottery.batch_key_v2 == launch.batch_key_v2
    assert lottery.batch_link_status == "signature"


def test_different_districts_or_years_do_not_merge(session):
    guangming = _notice()
    nanshan = _notice(canonical_key="k2", url_hash="h2", region_code="南山区")
    session.add_all([guangming, nanshan])
    session.flush()
    rebuild_links(session)
    assert guangming.batch_key_v2 != nanshan.batch_key_v2


def test_enterprise_and_personal_same_slot_stay_apart(session):
    # 同区、同年、同批次、同房型，只有受众不同——曾经会被并成一个批次，
    # 导致个人摇号结果套用企业房源数。
    personal = _notice()
    enterprise = _notice(
        canonical_key="k2",
        url_hash="h2",
        title="光明区住房和建设局关于2026年第一批次面向企事业单位配租保障性租赁住房的通告",
    )
    lottery = _notice(
        canonical_key="k3",
        url_hash="h3",
        category="process",
        title="光明区关于2026年第一批次面向个人配租保障性租赁住房公证摇号结果的通告",
        published_at=NOW - timedelta(days=4),
        first_seen_at=NOW - timedelta(days=4),
    )
    session.add_all([personal, enterprise, lottery])
    session.flush()

    rebuild_links(session)
    assert personal.batch_key_v2 != enterprise.batch_key_v2
    assert lottery.batch_key_v2 == personal.batch_key_v2


def test_notice_without_candidate_stays_unresolved(session):
    solo = _notice(
        canonical_key="k1",
        url_hash="h1",
        category="process",
        title="光明区关于2026年第一批次配租保障性租赁住房公证摇号结果的通告",
    )
    session.add(solo)
    session.flush()
    rebuild_links(session)
    assert solo.batch_key_v2 is None
    assert solo.batch_link_status == "unresolved"


def test_detect_stage_order():
    # 认租窗口过了不代表批次结束，后面还有核查/选房/签约
    assert detect_stage(_notice(title="2026年第一批次配租保障性租赁住房的通告"), NOW) == "applying"
    assert detect_stage(_notice(category="process", title="第一批次认租资格核查结果的公示"), NOW) == "review"
    # "选房名单及选房签约"当下是选房，签约是后续安排。
    assert detect_stage(_notice(category="process", title="第一批次选房名单及选房签约的通告"), NOW) == "selection"
    assert detect_stage(_notice(category="process", title="第一批次公证摇号结果的通告"), NOW) == "lottery"
    assert detect_stage(_notice(category="process", title="第一批次选房有关事项的通告"), NOW) == "selection"
    assert detect_stage(_notice(category="vacancy_exit", title="退出信息一览表"), NOW) is None


def test_build_batches_picks_highest_stage(session):
    session.add(_notice(canonical_key="k1", url_hash="h1"))
    session.add(
        _notice(
            canonical_key="k2",
            url_hash="h2",
            category="process",
            title="光明区关于2026年第一批次面向个人配租保障性租赁住房公证摇号结果的通告",
            published_at=NOW - timedelta(days=4),
            first_seen_at=NOW - timedelta(days=4),
            application_start=None,
            application_end=None,
        )
    )
    session.flush()
    rebuild_links(session)

    batches = build_batches(session, now=NOW)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.stage == "lottery"
    assert batch.stage_label == "摇号结果"
    assert batch.launch is not None and batch.launch.category == "rent_launch"
    assert len(batch.notices) == 2


def test_unresolved_notice_is_visible_but_separate(session):
    # 关联不上的公告仍然要出现在早报里，只是不能借用别批的房源数。
    session.add(_notice(canonical_key="k1", url_hash="h1"))
    session.add(
        _notice(
            canonical_key="k2",
            url_hash="h2",
            category="process",
            title="光明区关于2026年第一批次配租保障性租赁住房公证摇号结果的通告",
            published_at=NOW - timedelta(days=4),
            first_seen_at=NOW - timedelta(days=4),
            application_start=None,
            application_end=None,
        )
    )
    session.flush()
    # 不跑 rebuild_links：两条公告都没有批次键。

    batches = build_batches(session, now=NOW)
    assert len(batches) == 2
    # 配租通告自己是锚点；摇号通告没关联上，单独成组，不能并进配租批。
    assert sum(batch.key.startswith("unresolved:") for batch in batches) == 1
