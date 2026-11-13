from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.housing.models import HousingExitRecord, HousingNotice
from app.housing.render import render_digest, render_instant


def _notice(**overrides) -> HousingNotice:
    now = datetime(2026, 10, 20, 8, 30)
    payload = dict(
        canonical_key=overrides.get("canonical_key", "k1"),
        canonical_url="https://example.gov.cn/post_1.html",
        url_hash="h1",
        title="光明区2026年第一批次配租保障性租赁住房的通告",
        title_norm="光明区2026年第一批次配租保障性租赁住房的通告",
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["baozu"],
        region_code="光明区",
        published_at=now - timedelta(hours=2),
        first_seen_at=now - timedelta(hours=2),
        last_seen_at=now,
        application_start=now - timedelta(days=2),
        application_end=now + timedelta(days=2),
        units_count=1963,
        projects=["明皓府", "明汇府"],
    )
    payload.update(overrides)
    return HousingNotice(**payload)


def test_instant_render_shows_window_and_remaining(session):
    notice = _notice()
    session.add(notice)
    session.flush()
    title, content = render_instant([notice], now=datetime(2026, 10, 20, 8, 30))
    assert "新通告（1 条）" in content
    assert "认租 10-18 08:30 → 10-22 08:30（剩余 2 天）" in content
    assert "1963 套" in content
    # 标题必须是可点链接
    assert f"[{notice.title}]({notice.canonical_url})" in content
    assert len(title) <= 100


def test_digest_sections(session):
    now = datetime(2026, 10, 20, 8, 30)
    session.add(_notice(canonical_key="k1", url_hash="h1"))
    session.add(
        _notice(
            canonical_key="k2",
            url_hash="h2",
            canonical_url="https://example.gov.cn/post_2.html",
            title="罗湖区2026年第三季度保障性住房退出信息一览表",
            category="vacancy_exit",
            priority=80,
            region_code="罗湖区",
            application_start=None,
            application_end=None,
            published_at=now - timedelta(days=3),
        )
    )
    # 历史回填的旧通告不应出现在“近 24 小时新增”里
    session.add(
        _notice(
            canonical_key="k3",
            url_hash="h3",
            canonical_url="https://example.gov.cn/post_3.html",
            title="南山区2025年度配租通告",
            published_at=now - timedelta(days=90),
            first_seen_at=now - timedelta(minutes=5),
            application_start=None,
            application_end=None,
        )
    )
    session.flush()

    _, content = render_digest(session, now=now)
    assert "正在认租（1 批）" in content
    assert "近 24 小时新增（1 条）" in content
    assert "南山区2025年度配租通告" not in content
    assert "腾退与存量（近 30 天 1 条）" in content
    assert "数据源" in content
    # 早报里每条通告都要带原文链接
    assert "[光明区2026年第一批次配租保障性租赁住房的通告](https://example.gov.cn/post_1.html)" in content
    assert "[罗湖区2026年第三季度保障性住房退出信息一览表](https://example.gov.cn/post_2.html)" in content


def test_digest_reports_empty_state(session):
    _, content = render_digest(session, now=datetime(2026, 10, 20, 8, 30))
    assert "暂无正在接受认租的批次" in content
    assert "未发现腾退类通告" in content


def test_digest_exit_summary_grouped(session):
    now = datetime(2026, 10, 20, 8, 30)
    session.add(
        HousingExitRecord(
            source_key="lh_exit",
            community_name="龙悦居二期",
            housing_type="公租房",
            row_fingerprint="fp1",
        )
    )
    session.flush()
    _, content = render_digest(session, now=now)
    assert "龙悦居二期（公租房）：1 套/条" in content


def test_lottery_title_says_what_happened_not_the_stats(session):
    from app.housing.lottery import CategorySummary, LotterySummary

    notice = _notice(category="lottery_result", title="光明区公证摇号结果的通告")
    session.add(notice)
    session.flush()
    summary = {notice.id: LotterySummary(notice.id, [CategorySummary("40", "40㎡左右", 3744, units=111)])}
    scoped, scoped_body = render_instant([notice], scope="光明区 · 管家版", lottery=summary)
    bare, bare_body = render_instant([notice], lottery=summary)
    # 标题只交代"公布了什么"；多少人抢多少套放正文第一行。
    assert scoped == "光明区 · 管家版 · 公证摇号结果已公布"
    assert bare == "公证摇号结果已公布"
    assert "本批认租 **3,744 户** · 房源 **111 套**" in scoped_body
    assert "本批认租 **3,744 户** · 房源 **111 套**" in bare_body


def test_lottery_title_carries_batch_number(session):
    # 同一个区两个月内可能两次摇号，标题里没有批次就分不清是哪一批。
    notice = _notice(
        category="lottery_result",
        title="光明区2026年第一批次配租保障性租赁住房公证摇号结果的通告",
    )
    session.add(notice)
    session.flush()
    title, _ = render_instant([notice], scope="光明区 · 管家版")
    assert title == "光明区 · 管家版 · 2026第1批 公证摇号结果已公布"


def test_stage_push_shows_batch_timeline(session):
    notice = _notice(
        category="process",
        title="光明区2026年第一批次保障性租赁住房选房有关事项的通告",
    )
    session.add(notice)
    session.flush()
    _, content = render_instant([notice], scope="光明区 · 管家版")
    assert "批次进度：**选房**（第 4/5 步）" in content
    assert "认租✅ → 资格核查✅ → 摇号✅ → 选房🔵 → 签约⚪" in content
    assert "逾期视为放弃" in content


def test_gongzu_timeline_has_no_lottery_step(session):
    # 公租房按轮候排序选房，没有公证摇号；流程里不能凭空多一步。
    notice = _notice(
        category="process",
        title="光明区2026年度第二批次公共租赁住房选房签约有关事项的通告",
        housing_types=["gongzu"],
    )
    session.add(notice)
    session.flush()
    _, content = render_instant([notice], scope="光明区 · 管家版")
    assert "批次进度：**选房**（第 3/4 步）" in content
    assert "认租✅ → 资格核查✅ → 选房🔵 → 签约⚪" in content
    assert "摇号" not in content


def test_new_batch_tells_people_who_did_not_apply_to_ignore(session):
    # 广播里同时有没认租的人，必须给一句"忽略本条"，否则每条都像在催他。
    notice = _notice()
    session.add(notice)
    session.flush()
    _, content = render_instant([notice], scope="光明区 · 管家版")
    assert "**你现在要做的**" in content
    assert "截图保存**认租编号**" in content
    assert "忽略本条，不影响你现有的轮候排位" in content


def test_lottery_message_has_conditional_actions(session):
    notice = _notice(category="lottery_result", title="光明区公证摇号结果的通告")
    session.add(notice)
    session.flush()
    _, content = render_instant([notice], scope="光明区 · 管家版")
    assert "没认租 → 忽略本条" in content
    assert "已收到选房通知" in content


def test_lookup_link_hidden_for_gongzu_selection(session, lookup_published, monkeypatch):
    # 公租房没有公证摇号，查询页里没有这批数据，给了链接只会查不到。
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "https://lookup.example.com/")
    from app.housing.config import get_housing_settings
    from app.housing.render import _lookup_link

    get_housing_settings.cache_clear()
    try:
        baozu = _notice(category="lottery_result", title="光明区2026年第一批次配租保障性租赁住房公证摇号结果的通告")
        gongzu = _notice(
            canonical_key="k2",
            url_hash="h2",
            category="lottery_result",
            title="光明区2026年度第二批次公共租赁住房选房有关事项的通告",
            housing_types=["gongzu"],
        )
        session.add_all([gongzu, baozu])
        session.flush()
        lookup_published(baozu, gongzu)
        # 两批都已登记发布，差别只在房型。
        assert _lookup_link("selection", baozu, session=session).startswith("https://lookup.example.com/")
        assert _lookup_link("selection", gongzu, session=session) == ""
    finally:
        get_housing_settings.cache_clear()


def test_digest_tracked_batches_are_one_line_each(session, monkeypatch):
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "https://lookup.example.com/")
    from app.housing.config import get_housing_settings

    get_housing_settings.cache_clear()
    now = datetime(2026, 10, 20, 8, 30)
    session.add(
        _notice(
            published_at=now - timedelta(days=20),
            first_seen_at=now - timedelta(days=20),
            application_start=now - timedelta(days=20),
            application_end=now - timedelta(days=10),
        )
    )
    session.add(
        _notice(
            canonical_key="k2",
            url_hash="h2",
            canonical_url="https://example.gov.cn/post_2.html",
            category="process",
            title="光明区2026年第一批次保障性租赁住房选房有关事项的通告",
            published_at=now - timedelta(days=2),
            first_seen_at=now - timedelta(days=2),
            application_start=None,
            application_end=None,
        )
    )
    session.flush()
    try:
        _, content = render_digest(session, now=now, detail=True)
    finally:
        get_housing_settings.cache_clear()
    assert "在途批次（1 批）" in content
    # 一个总群里多个区同批号会撞车，批次行必须带区名。
    assert "[光明区 2026 第1批 保租房 对象待核实](https://example.gov.cn/post_2.html)｜选房｜" in content
    # 这批还没登记发布过查询页，待办只能是纯文字，不能挂死链。
    assert "lookup.example.com" not in content


def test_city_group_title_keeps_district(session):
    # 合并成一个总群后 scope 不再含区名，标题必须自己补上。
    notice = _notice(
        category="lottery_result",
        title="光明区2026年第一批次配租保障性租赁住房公证摇号结果的通告",
    )
    session.add(notice)
    session.flush()
    title, _ = render_instant([notice], scope="深圳 · 管家版", city_wide=True)
    # 区名和批次连写：「光明区2026第1批」是批次标识，分开写会像群名。
    assert title == "深圳 · 管家版 · 光明区2026第1批 公证摇号结果已公布"


def test_city_group_stage_title_keeps_district(session):
    notice = _notice(
        category="process",
        title="光明区2026年第一批次保障性租赁住房选房有关事项的通告",
    )
    session.add(notice)
    session.flush()
    title, _ = render_instant([notice], scope="深圳 · 管家版", city_wide=True)
    assert "光明区" in title
    assert "选房" in title


def test_digest_has_lookup_entry(session, lookup_published, monkeypatch):
    # 早报每天必发，是把“认租编号查询”送到用户眼前最稳的位置。
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "https://lookup.example.com/")
    from app.housing.config import get_housing_settings

    get_housing_settings.cache_clear()
    now = datetime(2026, 10, 20, 8, 30)
    notice = _notice(
        category="lottery_result",
        title="光明区2026年第一批次配租保障性租赁住房公证摇号结果的通告",
    )
    session.add(notice)
    session.flush()
    lookup_published(notice)
    try:
        _, content = render_digest(session, now=now)
    finally:
        get_housing_settings.cache_clear()
    # 链接带当天日期：转发出去的旧链接第二天会被页面判过期。
    assert "[点这里查询](https://lookup.example.com/?d=20261020)" in content
    assert "已登记发布可查：光明区 2026第1批" in content


def test_digest_lookup_entry_says_nothing_to_query(session, lookup_published, monkeypatch):
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "https://lookup.example.com/")
    from app.housing.config import get_housing_settings

    get_housing_settings.cache_clear()
    # 登记过一版页面，但当前没有任何批次可查。
    lookup_published()
    try:
        _, content = render_digest(session, now=datetime(2026, 10, 20, 8, 30))
    finally:
        get_housing_settings.cache_clear()
    assert "入口为最近一次登记发布的版本" in content


def test_digest_hides_lookup_when_nothing_published(session, monkeypatch):
    # 没人工确认上传之前，早报不能宣称"已经可以查了"。
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "https://lookup.example.com/")
    from app.housing.config import get_housing_settings

    get_housing_settings.cache_clear()
    session.add(
        _notice(
            category="lottery_result",
            title="光明区2026年第一批次配租保障性租赁住房公证摇号结果的通告",
        )
    )
    session.flush()
    try:
        _, content = render_digest(session, now=datetime(2026, 10, 20, 8, 30))
    finally:
        get_housing_settings.cache_clear()
    assert "lookup.example.com" not in content


def test_digest_has_no_lookup_entry_when_unset(session, monkeypatch):
    # 不能 delenv：.env 里配了值，pydantic 还会从文件读到；置空才是真的没配。
    monkeypatch.setenv("HOUSING_LOOKUP_URL", "")
    from app.housing.config import get_housing_settings

    get_housing_settings.cache_clear()
    try:
        _, content = render_digest(session, now=datetime(2026, 10, 20, 8, 30))
    finally:
        get_housing_settings.cache_clear()
    assert "认租编号查排位" not in content


def test_lookup_url_carries_the_day_and_keeps_other_params(monkeypatch):
    # 日期要跟着推送当天走；已有参数不能丢，旧的 d 要被覆盖掉。
    monkeypatch.setenv(
        "HOUSING_LOOKUP_URL", "https://lookup.example.com/index.html?from=push&d=20260101"
    )
    from app.housing.config import get_housing_settings
    from app.housing.render import lookup_url_for

    get_housing_settings.cache_clear()
    try:
        # 23:40 也必须还是当天（按北京时间算，不能退到 UTC 的前一天）。
        assert (
            lookup_url_for(datetime(2026, 10, 20, 23, 40))
            == "https://lookup.example.com/index.html?from=push&d=20261020"
        )
    finally:
        get_housing_settings.cache_clear()


def test_digest_only_shows_own_district(session):
    # 单区群：光明群收到宝安的通告，用户会觉得发错了群。
    now = datetime(2026, 10, 20, 8, 30)
    session.add(_notice(canonical_key="gm", url_hash="h1", region_code="光明区"))
    session.add(
        _notice(
            canonical_key="ba",
            url_hash="h2",
            canonical_url="https://example.gov.cn/post_2.html",
            title="宝安区2026年第一批次配租保障性租赁住房的通告",
            region_code="宝安区",
        )
    )
    session.flush()
    _, content = render_digest(session, now=now, scope="光明区 · 管家版", regions=("光明区",))
    assert "光明区2026年第一批次" in content
    assert "宝安区2026年第一批次" not in content


def test_digest_keeps_city_wide_notice_for_every_district(session):
    # 市级批次面向全市，单区群也必须收到，否则只订一个区的人会漏掉。
    now = datetime(2026, 10, 20, 8, 30)
    session.add(
        _notice(
            canonical_key="city",
            url_hash="h3",
            title="深圳市2026年第一批次配租保障性租赁住房的通告",
            region_code="深圳市",
            level="city",
        )
    )
    session.flush()
    _, content = render_digest(session, now=now, scope="光明区 · 管家版", regions=("光明区",))
    assert "深圳市2026年第一批次" in content


def test_long_content_is_capped(session):
    notices = [
        _notice(canonical_key=f"k{i}", url_hash=f"h{i}", canonical_url=f"https://e.gov.cn/{i}.html")
        for i in range(60)
    ]
    for item in notices:
        session.add(item)
    session.flush()
    _, content = render_instant(notices, now=datetime(2026, 10, 20, 8, 30))
    assert len(content) <= 18_000
