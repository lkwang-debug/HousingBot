"""Durable per-group event reservations and immutable message envelopes."""
from __future__ import annotations

import asyncio
from datetime import time, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.housing.lock import process_lock
from app.housing.models import (
    HousingDelivery, HousingGroupState, HousingMeta, HousingOutbox,
    HousingOutboxEvent, utcnow,
)

ACCEPTED = {"submitted", "confirmed"}
# 即时消息的补发窗口：超过就不再当成"新通告"发。
INSTANT_TTL = timedelta(hours=24)


def _windows_closed(session, job, now) -> bool:
    """这条即时消息挂着的通告全都过了认租截止时间。

    只对写了截止时间的通告生效；摇号/选房这类没有认租窗口的通知不受影响。
    """
    from app.housing.models import HousingNotice

    ends = []
    for event in session.scalars(select(HousingOutboxEvent).where(HousingOutboxEvent.outbox_id == job.id)):
        notice = session.get(HousingNotice, event.notice_id) if event.notice_id else None
        if notice is not None:
            if notice.category != "rent_launch" or notice.application_end is None:
                return False
            ends.append(notice.application_end)
    return bool(ends) and all(end < now for end in ends)


def enqueue(session, group, kind, title, content, events):
    """Caller serializes scheduling. A unique event reservation survives packet changes."""
    events = [(key, notice_id) for key, notice_id in events if not event_exists(session, group.identity, kind, key)]
    if not events:
        return None
    job = HousingOutbox(group_id=group.identity, topic=group.topic, kind=kind,
        title=title[:100], content=content, status="pending")
    session.add(job); session.flush()
    for key, notice_id in events:
        session.add(HousingOutboxEvent(group_id=group.identity, kind=kind,
            event_key=key, notice_id=notice_id, outbox_id=job.id))
    session.flush()
    return job


def event_exists(session, group_id, kind, key):
    return session.scalar(select(HousingOutboxEvent.id).where(
        HousingOutboxEvent.group_id == group_id, HousingOutboxEvent.kind == kind,
        HousingOutboxEvent.event_key == key)) is not None


def handled_notice_ids(session, group_id, kind):
    """这个群已经为哪些通告排过队。

    event_key 里带 business_version，重新解析正文会让 key 变化，同一条通告就会被
    再当成"新通告"发一次。这里按通告本身去重：已经发过、正在发、或者卡在
    needs_review 等人核对的，都算处理过了，不再重复入队。只有 cancelled
    （闸门判定不该发）之后才允许重新入队。
    """
    return set(session.scalars(
        select(HousingOutboxEvent.notice_id)
        .join(HousingOutbox, HousingOutbox.id == HousingOutboxEvent.outbox_id)
        .where(HousingOutboxEvent.group_id == group_id, HousingOutboxEvent.kind == kind,
               HousingOutbox.status != "cancelled", HousingOutboxEvent.notice_id.is_not(None))))


def scheduling_lock():
    return process_lock(get_settings().data_dir / "housing.enqueue.lock")


async def _account_rate_limit(session):
    while True:
        now = utcnow()
        record = session.get(HousingMeta, "send-rate")
        times = [float(t) for t in (record.value.get("times", []) if record else [])
                 if now.timestamp() - float(t) < 60]
        if len(times) < 4:
            times.append(now.timestamp())
            if record is None:
                record = HousingMeta(key="send-rate"); session.add(record)
            record.value = {"times": times}
            session.commit()
            return
        delay = max(.05, 60 - (now.timestamp() - min(times)) + .05)
        session.commit()  # Never hold a read/write transaction while waiting.
        await asyncio.sleep(delay)


async def flush_outbox(session, *, client=None, now=None, rate_limit=True, lock_path=None, job_ids=None):
    from app.housing.delivery import HousingPushClient
    now = now or utcnow()
    results = []
    lock_path = lock_path or get_settings().data_dir / "housing.send.lock"
    with process_lock(lock_path):
        # Holding the OS lock means no other live sender owns a "sending" record.
        stale_query = select(HousingOutbox).where(HousingOutbox.status == "sending")
        if job_ids is not None:
            stale_query = stale_query.where(HousingOutbox.id.in_(job_ids))
        for stale in session.scalars(stale_query):
            stale.status, stale.error = "needs_review", "上次发送进程中断，受理状态待核对"
            stale.updated_at = now
        session.commit()
        own_client = client is None
        sender = client or HousingPushClient()
        try:
            jobs_query = select(HousingOutbox).where(
                HousingOutbox.status.in_(["pending", "blocked", "failed"]),
                (HousingOutbox.next_attempt_at.is_(None) | (HousingOutbox.next_attempt_at <= now)),
            ).order_by(HousingOutbox.id)
            if job_ids is not None:
                jobs_query = jobs_query.where(HousingOutbox.id.in_(job_ids))
            jobs = list(session.scalars(jobs_query))
            ids = [job.id for job in jobs]
            session.commit()
            for job_id in ids:
                job = session.get(HousingOutbox, job_id)
                group = session.get(HousingGroupState, job.group_id)
                if not group or not group.enabled:
                    session.commit()
                    continue
                if job.kind == "digest" and (now.time() > time(12, 0) or job.created_at.date() != now.date()):
                    # Pending morning digests must not suddenly arrive the next day.
                    job.status, job.error = "cancelled", "已超早报补发时间窗"
                    session.commit(); continue
                if job.kind == "evening" and (now.time() < time(20, 30) or job.created_at.date() != now.date()):
                    job.status, job.error = "cancelled", "晚报不提前发送，也不跨天补发旧报表"
                    session.commit(); continue
                if job.kind == "instant":
                    # 「新通告」只在当天成立。token 挂了几天再补发，用户收到的是一条
                    # 打着"新"字、认租早就截止的消息，比漏发更糟。
                    if job.created_at and job.created_at < now - INSTANT_TTL:
                        job.status, job.error = "cancelled", "超过即时消息有效期，不再补发"
                        session.commit(); continue
                    if _windows_closed(session, job, now):
                        job.status, job.error = "cancelled", "该批认租已截止，不再补发新通告"
                        session.commit(); continue
                if job.kind == "deadline":
                    from app.housing.models import HousingNotice
                    expired = False
                    for event in session.scalars(select(HousingOutboxEvent).where(HousingOutboxEvent.outbox_id == job.id)):
                        notice = session.get(HousingNotice, event.notice_id) if event.notice_id else None
                        if not notice or not notice.application_end or notice.application_end <= now:
                            expired = True
                        elif notice.application_end.isoformat() not in event.event_key:
                            expired = True
                        elif event.event_key.endswith(":3d") and (notice.application_end.date() - now.date()).days <= 1:
                            expired = True
                    if expired:
                        job.status, job.error = "cancelled", "截止时间或提醒档位已变化，不补发旧提醒"
                        session.commit(); continue
                if job.topic and group.topic and job.topic != group.topic:
                    job.status, job.error = "needs_review", "群 topic 已变更，需核对旧消息接收目标"
                    session.commit(); continue
                if not group.topic or not sender.settings.pushplus_token:
                    job.status, job.error = "blocked", "缺少群 topic 或 PUSHPLUS_TOKEN"
                    job.updated_at = now
                    results.append({"id": job.id, "group_id": job.group_id, "status": job.status})
                    session.commit(); continue
                if job.status == "failed" and (job.next_attempt_at is None or job.attempts >= 3):
                    session.commit(); continue
                job.topic = group.topic
                title, content, topic = job.title, job.content, job.topic
                group_id = job.group_id
                session.commit()
                if rate_limit:
                    await _account_rate_limit(session)
                job = session.get(HousingOutbox, job_id)
                job.status, job.attempts, job.updated_at = "sending", job.attempts + 1, utcnow()
                attempt = HousingDelivery(delivery_key=f"outbox:{job.id}", attempt_number=job.attempts,
                    kind=job.kind, title=title, content_hash=__import__('hashlib').sha256(content.encode()).hexdigest(),
                    notice_ids=list(session.scalars(select(HousingOutboxEvent.notice_id).where(
                        HousingOutboxEvent.outbox_id == job_id, HousingOutboxEvent.notice_id.is_not(None)))),
                    submitted_at=utcnow(), status="sending")
                session.add(attempt); session.flush()
                from app.housing.frozen_wire import prepared_wire
                wire=prepared_wire(session,job)
                if wire:attempt.response_payload={'_frozen_wire':wire}
                attempt_id = attempt.id
                session.commit()
                session.refresh(attempt)
                session.expunge(attempt)
                session.commit()
                # Network operates on a detached attempt, with no database transaction.
                try:
                    await sender._submit(attempt, title, content, topic)
                except Exception:
                    __import__('logging').getLogger(__name__).exception("unexpected transport failure")
                    attempt.status, attempt.error = "needs_review", "发送时出现未分类异常，需核对受理状态"
                session.merge(attempt)
                job = session.get(HousingOutbox, job_id)
                job.status = attempt.status
                job.error = attempt.error
                job.updated_at = utcnow()
                retryable = bool((attempt.response_payload or {}).get("retryable"))
                job.next_attempt_at = (utcnow() + timedelta(minutes=5 if job.attempts == 1 else 30)
                    if job.status == "failed" and retryable and job.attempts < 3 else None)
                session.commit()
                results.append({"id": job_id, "group_id": group_id, "status": attempt.status,
                                "attempt_id": attempt_id})
        finally:
            if own_client:
                await sender.close()
    problem_query = select(HousingOutbox.id).join(
        HousingGroupState, HousingGroupState.group_id == HousingOutbox.group_id).where(
        HousingGroupState.enabled.is_(True),
        HousingOutbox.status.in_(["blocked", "failed", "needs_review"]))
    if job_ids is not None:
        problem_query = problem_query.where(HousingOutbox.id.in_(job_ids))
    problems = session.scalar(problem_query.limit(1))
    session.commit()
    pending_problem = bool(problems)
    return {"status": "degraded" if pending_problem else "ok" if results else "empty", "results": results}


def resolve(session, job_id, action, *, group_id=None, note=""):
    job = session.get(HousingOutbox, job_id)
    if not job:
        raise ValueError("消息不存在")
    if job.status == "sending":
        raise ValueError("发送中的消息不能人工修改")
    if action == "map":
        group = session.get(HousingGroupState, group_id)
        if not group:
            raise ValueError("目标群不存在")
        for event in session.scalars(select(HousingOutboxEvent).where(HousingOutboxEvent.outbox_id == job_id)):
            if event_exists(session, group_id, event.kind, event.event_key):
                raise ValueError("目标群已有相同事件，需先人工核对")
            event.group_id = group_id
        job.group_id, job.topic = group_id, group.topic
    elif action == "received":
        job.status = "confirmed"
    elif action == "cancel":
        job.status = "cancelled"
    elif action == "retry":
        if job.content == "历史记录未保存正文，禁止自动补发":
            raise ValueError("历史消息没有原始正文，不能自动补发")
        job.status, job.next_attempt_at = "pending", None
    else:
        raise ValueError("action 必须为 received、retry、cancel 或 map")
    audit = session.get(HousingMeta, f"resolution:{job_id}")
    if audit is None:
        audit = HousingMeta(key=f"resolution:{job_id}", value={"actions": []}); session.add(audit)
    actions = list(audit.value.get("actions", []))
    actions.append({"at": utcnow().isoformat(), "action": action, "group_id": group_id, "note": note})
    audit.value = {"actions": actions}
    job.resolution, job.updated_at = note or action, utcnow()
    session.flush()
    return {"id": job.id, "status": job.status, "group_id": job.group_id}
