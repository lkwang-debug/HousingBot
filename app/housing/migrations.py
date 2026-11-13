"""Housing-only, repeatable SQLite migration. Never touches alembic_version."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.housing.db import HousingBase
from app.housing.models import (
    HousingNotice, HousingLotteryStat, HousingBatchUnit, HousingMeta,
    HousingDelivery, HousingOutbox, HousingOutboxEvent, utcnow,
)

ADDITIONS = {
    "housing_notices": {"detail_checked_at": "DATETIME", "published_unverified": "BOOLEAN NOT NULL DEFAULT 0",
        "business_version": "VARCHAR(64) NOT NULL DEFAULT ''", "event_at": "DATETIME",
        "attachment_version": "VARCHAR(64) NOT NULL DEFAULT ''",
        "batch_key_v2": "VARCHAR(128)", "batch_link_status": "VARCHAR(32) NOT NULL DEFAULT 'unresolved'"},
    "housing_lottery_stats": {"complete": "BOOLEAN NOT NULL DEFAULT 0"},
    "housing_batch_units": {"verified": "BOOLEAN NOT NULL DEFAULT 0"},
}
VERSION = "housing-v2"


def migrate_schema(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in ADDITIONS.items():
            if table not in tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))
    HousingBase.metadata.create_all(engine)


def migrate(engine, *, apply=False):
    if not apply:
        with tempfile.TemporaryDirectory(prefix="housing-migration-") as directory:
            target = Path(directory) / "rehearsal.db"
            if engine.url.database and Path(engine.url.database).exists():
                with closing(sqlite3.connect(f"file:{Path(engine.url.database).as_posix()}?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(target)) as dst:
                        src.backup(dst)
            rehearsal = create_engine(f"sqlite:///{target.as_posix()}")
            try:
                result = migrate(rehearsal, apply=True)
            finally:
                rehearsal.dispose()
            return {"dry_run": True, **result}
    migrate_schema(engine)
    from app.housing.events import business_version, instant_event
    from app.housing.batches import rebuild_links, batch_key
    from app.housing.groups import sync_groups
    with Session(engine, expire_on_commit=False) as session:
        if session.get(HousingMeta, VERSION):
            for job in session.scalars(select(HousingOutbox).where(
                HousingOutbox.content == "历史记录未保存正文，禁止自动补发", HousingOutbox.resolution.is_(None))):
                old = session.scalar(select(HousingDelivery).where(
                    HousingDelivery.created_at == job.created_at, HousingDelivery.title == job.title,
                    HousingDelivery.kind == job.kind).limit(1))
                if old:
                    job.resolution = f"legacy:{old.id}"
                    job.updated_at = old.resolved_at or old.submitted_at or old.created_at
            session.commit()
            return {"status": "already_migrated", "version": VERSION}
        now = utcnow()
        groups = sync_groups(session, now)
        notices = list(session.scalars(select(HousingNotice)))
        for notice in notices:
            if notice.published_at and notice.published_at.date() > now.date():
                notice.published_at, notice.published_unverified = None, True
                notice.needs_manual_review = True
            notice.business_version = business_version(notice)
            notice.event_at = notice.first_seen_at or now
            if notice.detail_status == "ok":
                # Recheck old details once; a legacy "ok" is not proof of freshness.
                notice.detail_checked_at = None
        links = rebuild_links(session)
        for stat in session.scalars(select(HousingLotteryStat)):
            notice = session.get(HousingNotice, stat.notice_id)
            stat.batch_key = batch_key(notice) or f"unresolved:{notice.id}"
            stat.complete = False  # Requires new parser integrity validation.
        for unit in session.scalars(select(HousingBatchUnit)):
            launch = session.get(HousingNotice, unit.source_notice_id) if unit.source_notice_id else None
            unit.batch_key = batch_key(launch) if launch else f"unresolved-unit:{unit.id}"
            unit.verified = False
        scope_ids = {value: g.identity for g in groups for value in (g.name, g.display, g.identity)}
        imported = ambiguous = 0
        for old in session.scalars(select(HousingDelivery).order_by(HousingDelivery.id)):
            parts = old.delivery_key.split(":")
            group_id = scope_ids.get(parts[1]) if len(parts) >= 3 else None
            status = "submitted" if group_id and old.status in {"submitted", "delivered"} else "needs_review"
            if not group_id:
                group_id = "legacy-unmapped"
                ambiguous += 1
            job = HousingOutbox(group_id=group_id, topic="", kind=old.kind, title=old.title,
                content="历史记录未保存正文，禁止自动补发", status=status, attempts=old.attempt_number,
                created_at=old.created_at, updated_at=old.resolved_at or old.submitted_at or old.created_at,
                resolution=f"legacy:{old.id}",
                error=None if status == "submitted" else "历史群归属或受理结果需核对")
            session.add(job); session.flush()
            event_keys = []
            if old.kind == "instant":
                event_keys = [(instant_event(session.get(HousingNotice, i)), i)
                              for i in old.notice_ids if session.get(HousingNotice, i)]
            elif old.kind == "digest" and old.submitted_at:
                event_keys = [(f"digest:{old.submitted_at:%Y-%m-%d}", None)]
            else:
                event_keys = [(f"legacy:{old.id}", None)]
            for key, notice_id in event_keys:
                exists = session.scalar(select(HousingOutboxEvent.id).where(
                    HousingOutboxEvent.group_id == group_id, HousingOutboxEvent.kind == old.kind,
                    HousingOutboxEvent.event_key == key))
                if not exists:
                    session.add(HousingOutboxEvent(group_id=group_id, kind=old.kind,
                        event_key=key, notice_id=notice_id, outbox_id=job.id)); session.flush()
            imported += 1
        session.add(HousingMeta(key=VERSION, value={"migrated_at": now.isoformat(), "legacy_imported": imported}))
        session.commit()
        return {"status": "migrated", "version": VERSION, "notices": len(notices),
                "links": links, "imported": imported, "legacy_needs_mapping": ambiguous}


def stabilize_group_config(path):
    from app.housing.groups import load_groups
    groups = load_groups(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload["groups"] if isinstance(payload, dict) else payload
    by_name = {g.name: g for g in groups}
    for row in rows:
        if isinstance(row, dict) and row.get("name") in by_name:
            row["id"] = by_name[row["name"]].identity
            row.setdefault("enabled", True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
