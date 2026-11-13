from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.housing.delivery import HousingPushClient, instant_delivery_key, push_pending
from app.housing.groups import SubscribeGroup
from app.housing.models import (
    HousingDelivery,
    HousingGroupState,
    HousingNotice,
    HousingOutbox,
    utcnow,
)

TOPIC = "topic-a"


def _fake_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _notice(session, key: str = "k1") -> HousingNotice:
    notice = HousingNotice(
        canonical_key=key,
        canonical_url=f"https://e.gov.cn/{key}.html",
        url_hash=key,
        title="光明区2026年第一批次配租通告",
        title_norm="光明区2026年第一批次配租通告",
        merge_key=key,
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["baozu"],
        region_code="光明区",
        published_at=None,
        event_at=utcnow(),
    )
    session.add(notice)
    session.flush()
    return notice


def _use_token(monkeypatch, token: str = "test-token") -> None:
    monkeypatch.setattr(
        "app.housing.delivery.get_settings", lambda: SimpleNamespace(pushplus_token=token)
    )


def _use_groups(monkeypatch, session=None) -> SubscribeGroup:
    group = SubscribeGroup(name="全市", topic=TOPIC)
    monkeypatch.setattr("app.housing.groups.load_groups", lambda: [group])
    if session is not None:
        stamp = utcnow() - timedelta(days=1)
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
    return group


def _submit(session, client, key="instant:test", notice_ids=None):
    push = HousingPushClient(client=client)
    return asyncio.run(
        push.send(
            session,
            delivery_key=key,
            kind="instant",
            title="住房通告 1 条",
            content="## 新通告\n",
            notice_ids=notice_ids or [],
            topic=TOPIC,
        )
    )


def test_submit_success_marks_submitted(session, monkeypatch):
    _use_token(monkeypatch)
    _use_groups(monkeypatch)
    notice = _notice(session)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"code": 200, "msg": "ok", "data": "msg-1"})

    delivery = _submit(session, _fake_client(handler), notice_ids=[notice.id])

    assert delivery.status == "submitted"
    assert delivery.provider_message_id == "msg-1"
    assert seen[0]["token"] == "test-token"
    assert seen[0]["topic"] == TOPIC
    # 受理只是"接口收下了"，不冒充微信最终送达
    assert session.scalar(select(HousingOutbox)).status == "submitted"


@pytest.mark.parametrize("content", ["房" * 18_000, "房源信息\n" * 2_000], ids=['chinese-boundary','chinese-report'])
def test_full_chinese_daily_payload_reaches_real_transport_without_truncation(monkeypatch,content):
    _use_token(monkeypatch)
    seen=[]
    def handler(request):
        seen.append(json.loads(request.read().decode('utf-8')))
        return httpx.Response(200,json={"code":200,"data":"accepted-long-report"})
    async def run():
        async with _fake_client(handler) as http:
            sender=HousingPushClient(client=http)
            delivery=HousingDelivery()
            await sender._submit(delivery,'真实晚报',content,TOPIC)
            assert delivery.status=='submitted' and delivery.provider_message_id=='accepted-long-report'
    asyncio.run(run())
    from bs4 import BeautifulSoup
    assert len(content.encode('utf-8'))>18_000 and len(seen)==1 and seen[0]['template']=='html'
    assert ''.join(BeautifulSoup(seen[0]['content'],'html.parser').get_text().split())==''.join(content.split())


def test_compact_html_keeps_batch_text_links_negations_and_paragraph_order():
    from bs4 import BeautifulSoup
    from app.housing.message_format import compact_html
    text='**【B34｜公租房】**\n\n不限户籍、不要求社保满5年；例外以原文为准。\n\n认租：10-22 09:00—11-09 18:00\n\n[配租原文](https://example.gov.cn/notice)'
    soup=BeautifulSoup(compact_html(text),'html.parser')
    assert soup.get_text()==('【B34｜公租房】不限户籍、不要求社保满5年；例外以原文为准。认租：10-22 09:00—11-09 18:00配租原文')
    assert soup.a['href']=='https://example.gov.cn/notice' and len(soup.find_all('p'))==4


@pytest.mark.parametrize("content", ['a' * 18_001, '🏠' * 13_501, '房源信息\n' * 3_000], ids=['character-overflow','utf8-overflow','rendered-overflow'])
def test_oversized_payload_is_rejected_before_network_without_truncation(monkeypatch,content):
    _use_token(monkeypatch)
    def handler(request):raise AssertionError('Oversized content must not reach network')
    async def run():
        async with _fake_client(handler) as http:
            delivery=HousingDelivery()
            await HousingPushClient(client=http)._submit(delivery,'过长内容',content,TOPIC)
            assert delivery.status=='failed' and delivery.response_payload['retryable'] is False
    asyncio.run(run())


def test_duplicate_delivery_key_is_not_resent(session, monkeypatch):
    _use_token(monkeypatch)
    _use_groups(monkeypatch)
    notice = _notice(session)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 200, "data": "msg-1"})

    client = _fake_client(handler)
    _submit(session, client, key="instant:same", notice_ids=[notice.id])
    _submit(session, client, key="instant:same", notice_ids=[notice.id])

    assert calls["n"] == 1


def test_timeout_needs_review_and_is_not_retried(session, monkeypatch):
    _use_token(monkeypatch)
    _use_groups(monkeypatch)
    _submit(session, _fake_client(lambda request: (_ for _ in ()).throw(
        httpx.ReadTimeout("timeout", request=request))))

    job = session.scalar(select(HousingOutbox))
    assert job.status == "needs_review"
    assert "核对" in (job.error or "")

    # 超时的消息可能已经发出去了，不自动重发；后续轮次也不再领取
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls["n"] += 1
        return httpx.Response(200, json={"code": 200})

    monkeypatch.setattr("app.housing.delivery.HousingPushClient",
                        lambda *a, **k: HousingPushClient(client=_fake_client(handler)))
    asyncio.run(push_pending(session))
    assert calls["n"] == 0


def test_missing_token_blocks_without_posting(session, monkeypatch):
    _use_token(monkeypatch, token="")
    _use_groups(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不应该发起请求")

    delivery = _submit(session, _fake_client(handler))
    # 没有 token 时一条发送尝试都不会建，只有一条 blocked 的待发消息
    assert delivery is None
    job = session.scalar(select(HousingOutbox))
    assert job.status == "blocked"
    assert "PUSHPLUS_TOKEN" in (job.error or "")


def test_server_error_is_retryable(session, monkeypatch):
    _use_token(monkeypatch)
    _use_groups(monkeypatch)
    _submit(session, _fake_client(lambda request: httpx.Response(503, text="busy")))

    job = session.scalar(select(HousingOutbox))
    assert job.status == "failed"
    assert job.next_attempt_at is not None  # 留了重试窗口


def test_push_pending_sends_once_then_empty(session, monkeypatch):
    _use_token(monkeypatch)
    _use_groups(monkeypatch, session)
    _notice(session)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 200, "data": "msg-2"})

    class _Stub(HousingPushClient):
        def __init__(self):  # noqa: D401 - 让 push_pending 用假传输
            super().__init__(client=_fake_client(handler))

    monkeypatch.setattr("app.housing.delivery.HousingPushClient", _Stub)

    first = asyncio.run(push_pending(session))
    assert first["queued"] == 1
    assert calls["n"] == 1

    second = asyncio.run(push_pending(session))
    assert second["status"] == "empty"
    assert calls["n"] == 1


def test_instant_delivery_key_is_order_independent(session):
    left = _notice(session, "a")
    right = _notice(session, "b")
    assert instant_delivery_key([left, right]) == instant_delivery_key([right, left])
    assert instant_delivery_key([left, right]) != instant_delivery_key([left])


def _stale_job(session, monkeypatch, tmp_path, *, age, end_after=None):
    """造一条卡住的即时消息，模拟 token 挂了很久之后才恢复。"""
    _use_token(monkeypatch)
    # 必须落一行 HousingGroupState：flush_outbox 拿不到群状态就跳过，任务永远停在 pending。
    group = _use_groups(monkeypatch, session)
    notice = _notice(session)
    notice.application_end = end_after
    from app.housing.outbox import enqueue

    job = enqueue(session, group, "instant", "新通告", "## 新通告\n", [("ev-" + str(age), notice.id)])
    job.created_at = utcnow() - age
    session.commit()
    return job


def _flush(session, tmp_path, calls):
    from app.housing.outbox import flush_outbox

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 200, "data": "ok"})

    client = HousingPushClient(client=_fake_client(handler))
    return asyncio.run(flush_outbox(session, client=client, lock_path=tmp_path / "send.lock"))


def test_stale_instant_job_is_cancelled_instead_of_sent(session, monkeypatch, tmp_path):
    # token 恢复后不能把两天前的"新通告"补发出去。
    job = _stale_job(session, monkeypatch, tmp_path, age=timedelta(hours=25))
    calls = {"n": 0}
    _flush(session, tmp_path, calls)

    assert calls["n"] == 0
    assert session.get(HousingOutbox, job.id).status == "cancelled"


def test_fresh_instant_job_still_sends(session, monkeypatch, tmp_path):
    job = _stale_job(session, monkeypatch, tmp_path, age=timedelta(hours=2))
    calls = {"n": 0}
    _flush(session, tmp_path, calls)

    assert calls["n"] == 1
    assert session.get(HousingOutbox, job.id).status == "submitted"


def test_instant_job_with_closed_window_is_cancelled(session, monkeypatch, tmp_path):
    # 认租已经截止了，再补发"快去认租"就是误导。
    job = _stale_job(
        session,
        monkeypatch,
        tmp_path,
        age=timedelta(hours=2),
        end_after=utcnow() - timedelta(hours=1),
    )
    calls = {"n": 0}
    _flush(session, tmp_path, calls)

    assert calls["n"] == 0
    assert session.get(HousingOutbox, job.id).status == "cancelled"
