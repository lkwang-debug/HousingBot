"""PushPlus transport and durable instant-message scheduling."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.housing.models import HousingNotice, HousingGroupState, HousingDelivery, utcnow
from app.housing.message_limits import MAX_CONTENT_CHARS, MAX_CONTENT_BYTES

SEND_URL = "https://www.pushplus.plus/send"
OK_STATUSES = {"submitted", "confirmed"}


class HousingPushClient:
    def __init__(self, client=None):
        self.settings = get_settings()
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owns_client = client is None

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def _submit(self, delivery, title, content, topic=None):
        if not self.settings.pushplus_token or not topic:
            delivery.status, delivery.error = "blocked", "缺少 PUSHPLUS_TOKEN 或群 topic"
            return
        if len(content) > MAX_CONTENT_CHARS or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            delivery.status, delivery.error = "failed", "消息超过单条字符或UTF-8安全预算，未发送、未截断"
            delivery.response_payload = {"retryable": False, "reason": "payload_too_large"}
            return
        payload = {"token": self.settings.pushplus_token, "title": title[:100], "content": content,
                   "template": "markdown", "channel": "wechat", "topic": topic}
        from app.housing.message_format import compact_html, MAX_HTML_CHARS, has_inventory_fold
        frozen=(delivery.response_payload or {}).get('_frozen_wire')
        if frozen or len(content.encode('utf-8')) > 18_000 or has_inventory_fold(content):
            try:html = frozen['content'] if frozen else compact_html(content)
            except ValueError:
                delivery.status,delivery.error='failed','消息排版校验失败，未调用推送接口'
                delivery.response_payload={'retryable':False,'reason':'invalid_rendering'}
                return
            if len(html.encode('utf-16-le')) // 2 > MAX_HTML_CHARS:
                delivery.status, delivery.error = 'failed', '完整排版超出服务商单条容量，未发送、未截断'
                delivery.response_payload = {'retryable': False, 'reason': 'rendered_payload_too_large'}
                return
            payload.update(template='html', content=html)
        try:
            response = await self.client.post(SEND_URL, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            delivery.status, delivery.error = "failed", "未建立发送连接"
            delivery.response_payload = {"retryable": True}
            return
        except (httpx.TimeoutException, httpx.TransportError):
            delivery.status, delivery.error = "needs_review", "发送状态不明，需核对后决定是否补发"
            return
        try:
            result = response.json()
        except ValueError:
            result = None
        if response.status_code in {429, 500, 502, 503, 504}:
            delivery.status, delivery.error = "failed", f"推送接口 HTTP {response.status_code}"
            delivery.response_payload = {"retryable": True}
        elif isinstance(result, dict) and response.is_success and result.get("code") == 200:
            delivery.status = "submitted"
            delivery.provider_message_id = str(result.get("data") or "") or None
            delivery.error = None
            delivery.response_payload = {"code": 200}
        elif isinstance(result, dict) and result.get("code") is not None:
            delivery.status = "failed"
            delivery.error = str(result.get("msg") or "推送接口拒绝请求")[:500]
            delivery.response_payload = {"code": result.get("code"), "retryable": False}
            if result.get('data') is not None:
                detail = str(result['data']).replace(self.settings.pushplus_token, '[redacted]')[:500]
                delivery.error = (delivery.error + '：' + detail)[:500]
                delivery.response_payload['validation_detail'] = detail
        elif response.status_code in {400, 401, 403, 404}:
            delivery.status, delivery.error = "failed", f"推送接口 HTTP {response.status_code}"
            delivery.response_payload = {"retryable": False}
        else:
            delivery.status, delivery.error = "needs_review", "响应无法确认受理状态"
        delivery.response_payload = dict(delivery.response_payload or {}, wire_template=payload['template'],
            wire_chars=len(payload['content']), wire_sha256=hashlib.sha256(payload['content'].encode('utf-8')).hexdigest())
        delivery.resolved_at = utcnow()

    async def send(self, session, *, delivery_key, kind, title, content, notice_ids,
                   section="instant", force=False, topic=None, mark=True):
        """Compatibility entrypoint; every caller uses the durable global queue."""
        from app.housing.groups import sync_groups
        from app.housing.outbox import enqueue, flush_outbox, scheduling_lock
        with scheduling_lock():
            groups = sync_groups(session)
            group = next((g for g in groups if g.topic == topic and g.active), None)
            if group is None:
                raise ValueError("发送目标必须是已配置且启用的群")
            key = delivery_key + (f":force:{utcnow().isoformat()}" if force else "")
            event = hashlib.sha256(key.encode()).hexdigest()
            job = enqueue(session, group, kind, title, content, [(event, None)])
            session.commit()
        await flush_outbox(session, client=self)
        from app.housing.models import HousingOutboxEvent
        job_id = job.id if job else session.scalar(select(HousingOutboxEvent.outbox_id).where(
            HousingOutboxEvent.group_id == group.identity, HousingOutboxEvent.kind == kind,
            HousingOutboxEvent.event_key == event))
        return session.scalar(select(HousingDelivery).where(HousingDelivery.delivery_key == f"outbox:{job_id}")
                              .order_by(HousingDelivery.attempt_number.desc()))


def _has_revision(session, group_id, notice, identities):
    from app.housing.models import HousingOutbox, HousingOutboxEvent
    from app.housing.events import instant_event
    if not notice.event_at or not notice.first_seen_at or notice.event_at <= notice.first_seen_at + timedelta(seconds=1):
        return False
    events = session.execute(select(HousingOutboxEvent.event_key, HousingOutbox.created_at)
        .join(HousingOutbox, HousingOutbox.id == HousingOutboxEvent.outbox_id)
        .where(HousingOutboxEvent.group_id == group_id, HousingOutboxEvent.kind == "instant",
            HousingOutboxEvent.notice_id.in_(identities), HousingOutbox.status != "cancelled")).all()
    return any(key != instant_event(notice) and stamp < notice.event_at for key, stamp in events)


async def push_pending(session, limit=None):
    from app.housing.config import get_housing_settings
    from app.housing.dedup import unique_notices, push_identity
    from app.housing.events import instant_event
    from app.housing.groups import sync_groups
    from app.housing.outbox import handled_notice_ids, enqueue, event_exists, flush_outbox, scheduling_lock
    from app.housing.render import render_instant_parts
    now = utcnow()
    settings = get_housing_settings()
    cache, queued = {}, 0
    with scheduling_lock():
        groups = sync_groups(session, now)
        cutoff = now - timedelta(days=settings.instant_max_age_days)
        notices = unique_notices(list(session.scalars(select(HousingNotice).where(
            HousingNotice.is_push_candidate.is_(True), HousingNotice.published_unverified.is_(False),
            HousingNotice.event_at >= cutoff,
        ).order_by(HousingNotice.priority.desc(), HousingNotice.event_at.desc()))))
        for group in groups:
            if not group.enabled:
                continue
            state = session.get(HousingGroupState, group.identity)
            handled = handled_notice_ids(session, group.identity, "instant")
            handled_identities = {}
            for notice_id in handled:
                previous = session.get(HousingNotice, notice_id)
                if previous:
                    handled_identities.setdefault(push_identity(previous), []).append(notice_id)
            scoped, revisions = [], set()
            for notice in notices:
                if not notice.event_at or notice.event_at < state.eligible_since or not group.covers(notice):
                    continue
                if notice.category == "rent_launch" and notice.application_end and notice.application_end < now:
                    continue
                if event_exists(session, group.identity, "instant", instant_event(notice)):
                    continue
                aliases = handled_identities.get(push_identity(notice), [])
                revision = _has_revision(session, group.identity, notice, aliases) if aliases else False
                if aliases and not revision:
                    continue
                published = notice.published_at or notice.first_seen_at
                if (not published or not cutoff <= published <= now) and not revision:
                    continue
                scoped.append(notice)
                if revision: revisions.add(notice.id)
            scoped = scoped[:limit or settings.instant_max_items]
            if not scoped:
                continue
            cache_key = (tuple(n.id for n in scoped), tuple(sorted(revisions)))
            if cache_key not in cache:
                cache[cache_key] = render_instant_parts(scoped, session=session, now=now, updated_notice_ids=revisions)
            parts = cache[cache_key]
            bundle = hashlib.sha256("|".join(sorted(instant_event(n) for n in scoped)).encode()).hexdigest()[:32]
            remaining = {n.id: n for n in scoped}
            for index, part in enumerate(parts):
                events = [(f"packet:{bundle}:{index + 1}", None)]
                members = [notice_id for notice_id in remaining if notice_id in part.notice_ids]
                for notice_id in members:
                    events.append((instant_event(remaining.pop(notice_id)), notice_id))
                job=enqueue(session, group, "instant", part.title, part.content, events)
                if job:
                    if part.wire_html:
                        from app.housing.frozen_wire import freeze_wire
                        freeze_wire(session,job,part.wire_html)
                    queued += 1
            if remaining:
                raise RuntimeError("即时消息分段未覆盖所有公告，整批入队已回滚")
        session.commit()
    result = await flush_outbox(session)
    result["queued"] = queued
    return result


def mark_notices(session, notice_ids, *, status, kind):
    # Compatibility audit fields, never the per-group eligibility source.
    if status not in OK_STATUSES:
        return
    for notice_id in notice_ids:
        notice = session.get(HousingNotice, notice_id)
        if notice is not None:
            if kind == "digest":
                notice.digest_pushed_at, notice.digest_push_status = utcnow(), status
            else:
                notice.instant_pushed_at, notice.instant_push_status = utcnow(), status


def instant_delivery_key(notices, scope=None):
    from app.housing.events import instant_event
    digest = hashlib.sha256("|".join(sorted(instant_event(n) for n in notices)).encode()).hexdigest()[:32]
    return f"instant:{scope or 'all'}:{digest}"
