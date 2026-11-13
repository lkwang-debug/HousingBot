from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from app.housing.delivery import HousingPushClient, instant_delivery_key, push_pending
from app.housing.groups import SubscribeGroup, load_groups
from app.housing.models import HousingGroupState, HousingNotice, utcnow


def _group(name: str, topic: str = "t", regions: tuple[str, ...] = ()) -> SubscribeGroup:
    return SubscribeGroup(name=name, topic=topic, regions=regions)


def _notice(session, key: str, region: str, level: str = "district") -> HousingNotice:
    notice = HousingNotice(
        canonical_key=key,
        canonical_url=f"https://e.gov.cn/{key}.html",
        url_hash=key,
        title=f"{region}配租通告",
        title_norm=f"{region}配租通告",
        merge_key=key,
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["baozu"],
        region_code=region,
        level=level,
        event_at=utcnow(),
    )
    session.add(notice)
    session.flush()
    return notice


def test_empty_regions_means_everything(session):
    notice = _notice(session, "k1", "光明区")
    assert _group("全市").covers(notice) is True


def test_district_group_only_covers_its_own(session):
    guangming = _notice(session, "k1", "光明区")
    nanshan = _notice(session, "k2", "南山区")
    group = _group("光明区", regions=("光明区",))
    assert group.covers(guangming) is True
    assert group.covers(nanshan) is False


def test_city_level_notice_reaches_every_district_group(session):
    """市级批次面向全市，只订一个区的人也不能漏。"""
    city = _notice(session, "k1", "深圳市", level="city")
    assert _group("光明区", regions=("光明区",)).covers(city) is True


def test_inactive_group_is_skipped(session):
    assert _group("光明区", topic="", regions=("光明区",)).active is False


def test_load_groups_reads_config(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text(
        json.dumps(
            {
                "groups": [
                    {"name": "全市", "topic": "abc", "regions": []},
                    {"name": "光明区", "topic": "", "regions": ["光明区"]},
                    {"name": "", "topic": "ignored"},
                ]
            }
        ),
        encoding="utf-8",
    )
    groups = load_groups(path)
    assert [group.name for group in groups] == ["全市", "光明区"]
    assert groups[0].active is True and groups[1].active is False
    assert groups[1].regions == ("光明区",)


def test_delivery_key_differs_per_group(session):
    notice = _notice(session, "k1", "光明区")
    assert instant_delivery_key([notice], "光明区") != instant_delivery_key([notice], "全市")


def _mock_groups(monkeypatch, groups, session=None, since=None):
    monkeypatch.setattr("app.housing.groups.load_groups", lambda: groups)
    # 群早就启用过：eligible_since 早于公告时间，才轮得到这一批公告。
    if session is not None:
        stamp = since or utcnow() - timedelta(days=1)
        for group in groups:
            session.add(
                HousingGroupState(
                    group_id=group.identity,
                    name=group.name,
                    topic=group.topic,
                    enabled=True,
                    activated_at=stamp,
                    eligible_since=stamp,
                )
            )
        session.flush()


def test_dispatch_sends_one_message_per_group(session, monkeypatch):
    _notice(session, "k1", "光明区")
    _notice(session, "k2", "南山区")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"code": 200, "data": "msg-1"})

    class _Stub(HousingPushClient):
        def __init__(self):
            super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)
    monkeypatch.setattr("app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token="t"))
    _mock_groups(monkeypatch, [_group("全市", "topic-all"), _group("光明区", "topic-gm", ("光明区",))], session)

    result = asyncio.run(push_pending(session))

    assert len(result["results"]) == 2
    assert {item["status"] for item in result["results"]} == {"submitted"}
    assert len(sent) == 2

    payloads = {item["topic"]: item for item in sent}
    assert set(payloads) == {"topic-all", "topic-gm"}
    assert "新增2条" in payloads["topic-all"]["content"]
    assert "新增1条" in payloads["topic-gm"]["content"]
    assert all(p['template']=='html' and 'HOUSINGFOLD' not in p['content'] for p in payloads.values())

    # 每个群各自记事件，第二轮不再重复投递
    assert asyncio.run(push_pending(session))["status"] == "empty"


def test_failed_group_keeps_its_own_copy_pending(session, monkeypatch):
    """一个群失败时另一个群照常收到；失败那个群的公告不能让成功群连带重复。"""
    _notice(session, "k1", "光明区")
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        attempts.append(payload["topic"])
        if payload["topic"] == "topic-gm":
            return httpx.Response(500, json={"code": 500, "msg": "boom"})
        return httpx.Response(200, json={"code": 200, "data": "ok"})

    class _Stub(HousingPushClient):
        def __init__(self):
            super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)
    monkeypatch.setattr("app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token="t"))
    _mock_groups(monkeypatch, [_group("全市", "topic-all"), _group("光明区", "topic-gm", ("光明区",))], session)

    result = asyncio.run(push_pending(session))

    by_group = {item["group_id"]: item["status"] for item in result["results"]}
    assert by_group[_group("全市", "topic-all").identity] == "submitted"
    assert by_group[_group("光明区", "topic-gm").identity] == "failed"
    # 失败群的消息包留在库里等重试，成功群已经核销
    from app.housing.models import HousingOutbox

    statuses = sorted(row.status for row in session.scalars(select(HousingOutbox)))
    assert statuses == ["failed", "submitted"]


def test_new_group_does_not_replay_history(session, monkeypatch):
    """新群从启用时刻开始收，不补历史即时消息。"""
    _notice(session, "k1", "光明区")

    class _Stub(HousingPushClient):
        def __init__(self):
            super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"code": 200, "data": "ok"}))))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)
    monkeypatch.setattr("app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token="t"))
    _mock_groups(monkeypatch, [_group("全市", "topic-all")], session)

    asyncio.run(push_pending(session))

    # 事后加一个群：它没有历史，只从启用时间往后收
    late = _group("二群", "topic-2")
    _mock_groups(monkeypatch, [_group("全市", "topic-all"), late])
    assert asyncio.run(push_pending(session))["status"] == "empty"


def test_old_notice_is_never_pushed_as_new(session, monkeypatch):
    """迁移后全量重扫会把老通告的 event_at 刷成"现在"。

    2025 年的批次就是这样冒充过一次"新通告"。发布时间超窗的一律只进早报。
    """
    old = _notice(session, "old", "光明区")
    old.published_at = utcnow() - timedelta(days=400)
    old.event_at = utcnow()
    session.flush()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        sent.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"code": 200, "data": "ok"})

    class _Stub(HousingPushClient):
        def __init__(self):
            super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)
    monkeypatch.setattr("app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token="t"))
    _mock_groups(monkeypatch, [_group("全市", "topic-all")], session)

    assert asyncio.run(push_pending(session))["status"] == "empty"
    assert sent == []


def test_scan_collect_pending_push_skips_old_notices(session, monkeypatch):
    # 扫描那条路径也得挡住，否则定时任务照样会把老通告塞进队列。
    old = _notice(session, "old", "光明区")
    old.published_at = utcnow() - timedelta(days=400)
    old.event_at = utcnow()
    fresh = _notice(session, "new", "光明区")
    fresh.published_at = utcnow()
    fresh.event_at = utcnow()
    session.flush()
    _mock_groups(monkeypatch, [_group("全市", "topic-all")], session)

    from app.housing.scan import collect_pending_push

    pending = collect_pending_push(session, 100)
    assert [item.canonical_key for item in pending] == ["new"]


def test_reparsed_notice_is_not_pushed_twice(session, monkeypatch):
    """重扫改了业务版本（解析结果变化），同一条通告不能再当"新通告"发一次。"""
    notice = _notice(session, "k1", "光明区")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"code": 200, "data": "ok"})

    class _Stub(HousingPushClient):
        def __init__(self):
            super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)
    monkeypatch.setattr("app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token="t"))
    _mock_groups(monkeypatch, [_group("全市", "topic-all")], session)

    asyncio.run(push_pending(session))
    assert len(sent) == 1

    notice.business_version = "changed-by-rescan"
    session.flush()

    assert asyncio.run(push_pending(session))["status"] == "empty"
    assert len(sent) == 1


def test_duplicate_rows_are_pushed_once(session, monkeypatch):
    """同一份通告两个 URL = 两行，同一条消息里不能出现两遍。"""
    first = _notice(session, "a", "光明区")
    second = _notice(session, "b", "光明区")
    second.title_norm = first.title_norm
    second.application_end = first.application_end
    session.flush()
    _mock_groups(monkeypatch, [_group("全市", "topic-all")], session)

    from app.housing.scan import collect_pending_push

    pending = collect_pending_push(session, 100)
    assert len(pending) == 1
