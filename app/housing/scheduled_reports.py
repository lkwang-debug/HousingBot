"""One frozen city-wide report per day/slot, independently delivered to each group."""
from __future__ import annotations

import json
from datetime import time

from app.db import SessionLocal
from app.housing.config import get_housing_settings
from app.housing.groups import sync_groups
from app.housing.models import HousingMeta, HousingOutboxEvent, utcnow
from app.housing.outbox import enqueue, event_exists, flush_outbox, scheduling_lock
from app.housing.reporting import build_snapshot, local_time
from app.housing.report_render import MAX_CONTENT_BYTES, MAX_CONTENT_CHARS, render_report_parts


def report_due(kind, now):
    now = local_time(now)
    if kind == "digest":
        settings = get_housing_settings()
        return time(settings.digest_hour, settings.digest_minute) <= now.time() <= time(12, 0)
    if kind == "evening":
        return now.time() >= time(20, 30)
    raise ValueError("定时报表类型必须是 digest 或 evening")


def report_key(kind, now):
    return f"{kind}:{now:%Y-%m-%d}"


def _json_ready(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda item: item.isoformat()))


def prepare_report(session, kind, now, *, notices=None, test_label=None):
    snapshot = build_snapshot(session, kind, now=now, notices=notices)
    prefix = "> 真实数据排版验收，非正式定时发送；不会改变明天的自动推送安排。\n\n" if test_label else ""
    parts = render_report_parts(snapshot, max_chars=MAX_CONTENT_CHARS - len(prefix),
                                max_bytes=MAX_CONTENT_BYTES - len(prefix.encode("utf-8")))
    if kind in {"digest", "evening"} and len(parts) != 1:
        raise ValueError("早晚报必须完整生成一条消息，拒绝分多次推送")
    deadline_events = []
    if kind == "evening":
        from app.housing.remind import collect_reminders
        for reminder in collect_reminders(session, now):
            for index, part in enumerate(parts):
                if reminder.notice.id in (part.deadline_notice_ids if part.wire_html else part.notice_ids):
                    deadline_events.append({"key": reminder.base_key, "notice_id": reminder.notice.id, "part": index})
                    break
    rendered = [_json_ready(part.__dict__) for part in parts]
    if test_label:
        kind_name = {"instant": "即时通知", "digest": "早报", "evening": "晚报"}[kind]
        for part in rendered:
            part["title"] = f"【{test_label}·{kind_name}】" + part["title"]
            part["content"] = prefix + part["content"]
            if part.get('wire_html'):
                from html import escape
                part['wire_html']='<p>'+escape(prefix.strip())+'</p>'+part['wire_html']
    display = {}
    if kind in {"digest", "evening"}:
        from app.housing.daily_render import LAYOUT
        from app.housing.report_compact import display_title,object_summary
        display = {"presentation": [{'code':c.code,'detail_level':'focus' if c.changed else 'opportunity' if c.is_open else 'directory',
            'focus_reasons':c.delta_reasons,'display_title':display_title(c),'object_summary':object_summary(c),
            'daily_status':c.daily_status,'daily_reason':c.daily_reason} for c in snapshot.cards]}
        schema, layout = 4, LAYOUT
    else:
        from app.housing.instant_render import LAYOUT
        schema, layout = 4, LAYOUT
    return {"schema": schema, "layout": layout,
        "kind": kind, "generated_at": now.isoformat(),
        "parts": rendered,
        "audit": _json_ready(snapshot.audit_dict()), "deadline_events": deadline_events, **_json_ready(display)}


async def send_report(kind, *, dry_run=False, if_due=False, force=False, now=None, test_notices=None, test_label=None):
    now = local_time(now or utcnow())
    # A manual test must never claim the real report's daily event key.
    if not dry_run and not force and not report_due(kind, now):
        return {"status": "skipped", "reason": "不在早报补发时间窗" if kind == "digest" else "晚报20:30后发送，不提前占用正式日键"}
    with SessionLocal() as session:
        if dry_run:
            report = prepare_report(session, kind, now, notices=test_notices, test_label=test_label)
            if not report['parts']:return {'status':'skipped','reason':'没有本次公告，不生成空通告','queued':0,'parts':0}
            return {"status": "dry_run", "title": report["parts"][0]["title"],
                "content": "\n\n".join(part["content"] for part in report["parts"]), **report}
        with scheduling_lock():
            groups = sync_groups(session, now)
            enabled = [group for group in groups if group.enabled]
            if any(group.regions for group in enabled):
                raise ValueError("分类早晚报仅支持 regions=[] 的全市群")
            if not enabled:
                session.commit()
                return {"status": "skipped", "reason": "没有启用的总群"}
            key = report_key(kind, now)
            send_kind = kind
            if force:
                key = f"report-test:{kind}:{now:%Y%m%d%H%M%S%f}"
                send_kind = "test"
            meta_key = f"report:{key}"
            record = session.get(HousingMeta, meta_key)
            if record is None:
                report = prepare_report(session, kind, now, notices=test_notices, test_label=test_label)
                if not report['parts']:
                    session.commit()
                    return {'status':'skipped','reason':'没有本次公告，不生成空通告','queued':0,'parts':0}
                record = HousingMeta(key=meta_key, value=report)
                session.add(record)
            else:
                report = record.value
            queued, queued_ids = 0, []
            for group in enabled:
                if event_exists(session, group.identity, send_kind, key):
                    continue
                for index, part in enumerate(report["parts"]):
                    events = [(key if index == 0 else f"{key}:part:{index + 1}", None)]
                    # Report notice membership is audited separately from the daily root event.
                    events += [(f"{key}:p{index + 1}:n{notice_id}", notice_id) for notice_id in part["notice_ids"]]
                    title = ("【测试】" if force and not test_label else "") + part["title"]
                    job = enqueue(session, group, send_kind, title, part["content"], events)
                    if job is None:
                        raise RuntimeError("分段日键冲突，整份报表入队已回滚")
                    job.created_at = now
                    if part.get('wire_html'):
                        from app.housing.frozen_wire import freeze_wire
                        freeze_wire(session,job,part['wire_html'])
                    if kind == "evening" and not force:
                        for event in report.get("deadline_events", []):
                            if event["part"] == index and not event_exists(session, group.identity, "deadline", event["key"]):
                                session.add(HousingOutboxEvent(group_id=group.identity, kind="deadline",
                                    event_key=event["key"], notice_id=event["notice_id"], outbox_id=job.id))
                    queued += 1
                    queued_ids.append(job.id)
            session.commit()
        result = await flush_outbox(session, now=now, job_ids=queued_ids if force else None)
        result.update(queued=queued, report_key=key, parts=len(report["parts"]), generated_at=report["generated_at"])
        return result


async def send_test_report(kind, *, deliver=False, now=None):
    """Real collected data, test-only keys and no production deadline reservations."""
    if kind not in {"instant", "digest", "evening"}:
        raise ValueError("kind 必须是 instant、digest 或 evening")
    from datetime import timedelta
    from sqlalchemy import select
    from app.housing.dedup import unique_notices
    from app.housing.models import HousingNotice
    now = local_time(now or utcnow())
    notices = None
    if kind == "instant":
        with SessionLocal() as session:
            candidates = session.scalars(select(HousingNotice).where(
                HousingNotice.is_target.is_(True), HousingNotice.is_push_candidate.is_(True),
                HousingNotice.published_unverified.is_(False),
                HousingNotice.published_at >= now - timedelta(days=get_housing_settings().instant_max_age_days),
                HousingNotice.published_at <= now,
            ).order_by(HousingNotice.published_at.desc(), HousingNotice.priority.desc(), HousingNotice.id))
            notices = unique_notices(list(candidates))[:get_housing_settings().instant_max_items]
    return await send_report(kind, dry_run=not deliver, force=True, now=now,
                             test_notices=notices, test_label="排版验收")
