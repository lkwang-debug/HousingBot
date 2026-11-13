"""管家版的两个增量能力：截止前提醒、批次进度推进。

这两件事都是"广播即可"的——同一个群里的买家看的是同一批通告，
所以不需要按人区分，沿用群组推送。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.housing.batches import Batch, build_batches
from app.housing.dedup import unique_notices
from app.housing.delivery import OK_STATUSES, HousingPushClient
from app.housing.groups import load_groups
from app.housing.models import HousingDelivery, HousingNotice, utcnow
from app.housing.render import render_deadline, render_stage

DEADLINE_HORIZON_DAYS = 4
STAGE_LOOKBACK_DAYS = 7
PRO_STAGES = {"review", "lottery", "selection", "signing"}


@dataclass
class Reminder:
    base_key: str
    kind: str
    notice: HousingNotice
    days_left: int = 0
    batch: Batch | None = None


def collect_reminders(session: Session, now: datetime | None = None) -> list[Reminder]:
    now = now or utcnow()
    items = _deadline_items(session, now) + _stage_items(session, now)
    return items


def _deadline_items(session: Session, now: datetime) -> list[Reminder]:
    horizon = now + timedelta(days=DEADLINE_HORIZON_DAYS)
    rows = session.scalars(
        select(HousingNotice)
        .where(
            HousingNotice.is_target.is_(True),
            HousingNotice.category == "rent_launch",
            HousingNotice.application_end.is_not(None),
            HousingNotice.application_end > now,
            HousingNotice.application_end <= horizon,
        )
        .order_by(HousingNotice.application_end.asc(), HousingNotice.id.asc())
    ).all()
    # 同一份通告两个 URL 会落成两行，提醒只发一条；按 id 取靠前那行，键才稳定。
    rows = unique_notices(rows)
    items: list[Reminder] = []
    for notice in rows:
        days_left = (notice.application_end.date() - now.date()).days
        if days_left not in {0, 1, 2, 3}:
            continue
        kind = "1d" if days_left <= 1 else "3d"
        base = __import__("hashlib").sha256(notice.canonical_key.encode()).hexdigest()[:24] + ":" + notice.application_end.isoformat()
        items.append(
            Reminder(base_key=f"deadline:{base}:{kind}", kind="deadline", notice=notice, days_left=days_left)
        )
    return items


def _stage_items(session, now):
    # All process events now use the reliable instant outbox, including retries.
    return []


async def send_reminders(*, dry_run=False, deliver=False, now=None):
    from app.housing.groups import sync_groups
    from app.housing.outbox import enqueue, event_exists, flush_outbox, scheduling_lock
    now = now or utcnow()
    with SessionLocal() as session:
        items = collect_reminders(session, now)
        groups = [g for g in load_groups() if g.enabled and g.is_pro]
        preview = []
        for group in groups:
            for item in items:
                if group.covers(item.notice) and not event_exists(session, group.identity, item.kind, item.base_key):
                    preview.append({"group_id": group.identity, "kind": item.kind,
                                    "key": item.base_key, "title": item.notice.title})
        if dry_run or not deliver:
            return {"status": "dry_run", "items": preview}
        cache = {}
        with scheduling_lock():
            groups = sync_groups(session, now)
            queued = 0
            for group in groups:
                if not group.enabled or not group.is_pro:
                    continue
                for item in items:
                    if not group.covers(item.notice) or event_exists(session, group.identity, item.kind, item.base_key):
                        continue
                    if item.base_key not in cache:
                        cache[item.base_key] = render_deadline(item.notice, item.days_left, now=now, session=session)
                    title, content = cache[item.base_key]
                    if enqueue(session, group, item.kind, title, content, [(item.base_key, item.notice.id)]):
                        queued += 1
            session.commit()
        result = await flush_outbox(session)
        result["queued"] = queued
        return result
