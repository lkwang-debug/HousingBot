from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionLocal, sqlite_integrity_check
from app.housing.config import get_housing_settings
from app.housing.db import init_housing_db
from app.housing.models import (
    HousingDelivery,
    HousingDispatchItem,
    HousingExitRecord,
    HousingNotice,
    HousingNoticeOccurrence,
    HousingSource,
    HousingSourceState,
    HousingWatchRule,
    utcnow,
)

from app.housing.scan import pending_push_count, run_scan
from app.housing.sources import load_source_defs, sync_sources


def initialize() -> dict:
    init_housing_db()
    with SessionLocal() as session:
        sources = sync_sources(session)
        rule = session.scalar(select(HousingWatchRule).where(HousingWatchRule.name == "default"))
        if rule is None:
            settings = get_housing_settings()
            session.add(
                HousingWatchRule(
                    name="default",
                    regions=sorted(settings.region_filter),
                    housing_types=sorted(settings.type_filter),
                    categories=["rent_launch", "vacancy_exit"],
                )
            )
        session.commit()
        return {"sources": len(sources), "tables": len(HousingSource.metadata.tables)}


def doctor() -> dict:
    init_housing_db()
    settings = get_housing_settings()
    base = get_settings()
    with SessionLocal() as session:
        source_total = session.scalar(select(func.count(HousingSource.id))) or 0
        notice_total = session.scalar(select(func.count(HousingNotice.id))) or 0
        failed = session.execute(
            select(HousingSource.key, HousingSourceState.last_verify_status)
            .join(HousingSourceState, HousingSourceState.source_id == HousingSource.id)
            .where(
                HousingSourceState.last_verify_status.is_not(None),
                HousingSourceState.last_verify_status != "ok",
            )
        ).all()
        from app.housing.models import HousingGroupState, HousingOutbox, HousingAttachmentState, HousingMeta
        groups = []
        for group in session.scalars(select(HousingGroupState)):
            counts = dict(session.execute(select(HousingOutbox.status, func.count(HousingOutbox.id))
                .where(HousingOutbox.group_id == group.group_id).group_by(HousingOutbox.status)).all())
            last = session.scalar(select(HousingOutbox.updated_at).where(
                HousingOutbox.group_id == group.group_id, HousingOutbox.kind == "digest",
                HousingOutbox.status.in_(["submitted", "confirmed"]))
                .order_by(HousingOutbox.updated_at.desc()).limit(1))
            last_evening = session.scalar(select(HousingOutbox.updated_at).where(
                HousingOutbox.group_id == group.group_id, HousingOutbox.kind == "evening",
                HousingOutbox.status.in_(["submitted", "confirmed"]))
                .order_by(HousingOutbox.updated_at.desc()).limit(1))
            groups.append({"id": group.group_id, "name": group.name, "enabled": group.enabled,
                "activated_at": group.activated_at, "eligible_since": group.eligible_since,
                "outbox": counts, "last_digest_accepted": last, "last_evening_accepted": last_evening})
        parsing_errors = session.scalar(select(func.count(HousingAttachmentState.id)).where(
            HousingAttachmentState.status.in_(["failed", "partial"]))) or 0
        review_batches = session.scalar(select(func.count(HousingNotice.id)).where(
            HousingNotice.is_target.is_(True), HousingNotice.batch_link_status.in_(["ambiguous", "unresolved"]))) or 0
        missing_details = list(session.scalars(select(HousingNotice.id).where(
            HousingNotice.is_target.is_(True), HousingNotice.category == "rent_launch",
            HousingNotice.published_at >= utcnow() - timedelta(days=120),
            (HousingNotice.body_text.is_(None) | (HousingNotice.body_text == "") | (HousingNotice.detail_status != "ok")))))
        published = session.get(HousingMeta, "lookup-published")
        published_version = published.value.get("version") if published else None
        from app.housing.reporting import build_snapshot
        from app.housing.daily_delta import validate_totals
        daily=build_snapshot(session,'digest',now=utcnow())
        daily_tracking={'open_batches':daily.totals.open_batches,'tracked_batches':daily.totals.in_transit_batches,
            'baseline_key':daily.baseline_key,'totals_balanced':validate_totals(daily),
            'review_needed':[c.code for c in daily.cards if c.daily_status=='review_needed'],
            'history_counts':dict(__import__('collections').Counter(c['status'] for c in daily.archived_cards))}
    from app.housing.lookup import read_manifest
    manifest = read_manifest()
    return {
        "enabled": settings.enabled,
        "database_ok": sqlite_integrity_check(),
        "sources_registered": source_total,
        "sources_in_file": len(load_source_defs()),
        "notices": notice_total,
        "pending_push": _pending(),
        "failed_sources": [{"source": row[0], "status": row[1]} for row in failed],
        "pushplus_configured": bool(base.pushplus_token),
        "digest_at": f"{settings.digest_hour:02d}:{settings.digest_minute:02d}",
        "evening_at": "20:30",
        "quiet_windows": settings.quiet_windows,
        "groups": groups,
        "attachment_issues": parsing_errors,
        "unresolved_batch_notices": review_batches,
        "missing_in_flight_launch_details": missing_details,
        "lookup_built_version": manifest.get("version"),
        "lookup_published_version": published_version,
        "lookup_upload_pending": bool(manifest and manifest.get("version") != (published.value.get('source_version', published_version) if published else None)),
        "daily_tracking": daily_tracking,
    }


async def verify_sources(only: list[str] | None = None) -> list[dict]:
    results: list[dict] = []
    keys = only or [item.key for item in load_source_defs() if item.enabled]
    for key in keys:
        outcome = await run_scan(
            dry_run=True, only_sources=[key], max_pages=1, with_detail=False
        )
        degraded = [item for item in outcome.degraded if item["source"] == key]
        results.append(
            {
                "source": key,
                "status": "ok" if not degraded else "failed",
                "kind": degraded[0]["kind"] if degraded else None,
                "error": degraded[0]["error"] if degraded else None,
                "items": outcome.items_seen,
                "seconds": None,
            }
        )
    return results


def purge(days: int | None = None) -> dict:
    settings = get_housing_settings()
    days = days or settings.retention_days
    cutoff = utcnow() - timedelta(days=days)
    with SessionLocal() as session:
        from app.housing.models import HousingBatchAnchor, HousingOutboxEvent
        stale_ids = list(
            session.scalars(select(HousingNotice.id).where(HousingNotice.last_seen_at < cutoff,
                HousingNotice.id.not_in(select(HousingBatchAnchor.launch_id)),
                HousingNotice.id.not_in(select(HousingOutboxEvent.notice_id).where(HousingOutboxEvent.notice_id.is_not(None)))))
        )
        if stale_ids:
            session.execute(
                delete(HousingNoticeOccurrence).where(
                    HousingNoticeOccurrence.notice_id.in_(stale_ids)
                )
            )
            session.execute(
                delete(HousingDispatchItem).where(HousingDispatchItem.notice_id.in_(stale_ids))
            )
            session.execute(
                delete(HousingNotice).where(HousingNotice.id.in_(stale_ids))
            )
        session.execute(
            delete(HousingDelivery).where(HousingDelivery.created_at < cutoff)
        )
        session.commit()
    return {"deleted_notices": len(stale_ids), "cutoff": cutoff.isoformat()}


def reclassify() -> dict:
    """调整 classify / 抽取规则后，用已入库正文重跑，不必重新抓站。"""
    from app.housing.extract import _projects_from_text
    from app.housing.parsers.table import filter_projects
    from app.housing.store import _apply_classification

    changed = 0
    with SessionLocal() as session:
        for notice in session.scalars(select(HousingNotice)):
            before = (
                notice.category,
                notice.is_target,
                notice.is_push_candidate,
                list(notice.projects or []),
            )
            _apply_classification(notice, notice.published_at, "", reextract=False)
            # 详情页是表格的项目清单无法离线复现；正文能枚举出更多干净名字时才替换。
            if notice.body_text:
                existing = filter_projects(notice.projects)
                text_projects = _projects_from_text(notice.body_text)
                if text_projects and (not existing or len(text_projects) > len(existing)):
                    notice.projects = text_projects
                else:
                    notice.projects = existing
            after = (
                notice.category,
                notice.is_target,
                notice.is_push_candidate,
                list(notice.projects or []),
            )
            if before != after:
                changed += 1
        # 从 lottery_result 改成 process 的通告（比如"选房名单"），旧测算数据要一起清掉，
        # 否则统计表里会留下一批对不上分类的孤儿行。
        from app.housing.lottery import _prune_reclassified

        _prune_reclassified(session)
        session.commit()
    return {"changed": changed}


def dedupe() -> dict:
    """合并早期运行留下的重复通告：同 merge_key + 标题几乎相同。"""
    from app.housing.dedup import title_similarity

    merged = 0
    removed: list[int] = []
    with SessionLocal() as session:
        keys = [
            row[0]
            for row in session.execute(
                select(HousingNotice.merge_key)
                .where(HousingNotice.merge_key != "")
                .group_by(HousingNotice.merge_key)
                .having(func.count(HousingNotice.id) > 1)
            )
        ]
        for key in keys:
            group = list(
                session.scalars(
                    select(HousingNotice)
                    .where(HousingNotice.merge_key == key)
                    .order_by(HousingNotice.id)
                )
            )
            keep = group[0]
            for other in group[1:]:
                if keep.batch_key_v2 or other.batch_key_v2:
                    continue  # Never silently discard persisted batch/forecast provenance.
                if title_similarity(keep.title, other.title) < 0.99:
                    continue
                if keep.published_at and other.published_at:
                    if abs((keep.published_at - other.published_at).days) > 7:
                        continue
                session.execute(
                    delete(HousingDispatchItem).where(
                        HousingDispatchItem.notice_id == other.id
                    )
                )
                for occurrence in session.scalars(
                    select(HousingNoticeOccurrence).where(
                        HousingNoticeOccurrence.notice_id == other.id
                    )
                ):
                    clash = session.scalar(
                        select(HousingNoticeOccurrence.id).where(
                            HousingNoticeOccurrence.source_id == occurrence.source_id,
                            HousingNoticeOccurrence.url_hash == occurrence.url_hash,
                            HousingNoticeOccurrence.notice_id == keep.id,
                        )
                    )
                    if clash:
                        session.delete(occurrence)
                    else:
                        occurrence.notice_id = keep.id
                session.delete(other)
                removed.append(other.id)
                merged += 1
            session.flush()
            keep.occurrence_count = int(
                session.scalar(
                    select(func.count(HousingNoticeOccurrence.id)).where(
                        HousingNoticeOccurrence.notice_id == keep.id
                    )
                )
                or 1
            )
        session.commit()
    return {"merged": merged, "removed_ids": removed}


def _pending() -> int:
    with SessionLocal() as session:
        return pending_push_count(session)
