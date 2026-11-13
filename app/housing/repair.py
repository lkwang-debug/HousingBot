from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.housing.batches import rebuild_links
from app.housing.events import business_version
from app.housing.fetch import HousingHttpClient
from app.housing.models import HousingNotice, HousingBatchUnit, HousingBatchAnchor, utcnow
from app.housing.lock import scan_lock


async def repair_data(*, days=900, baseline=False):
    from app.housing.lottery import ingest_lottery, ingest_units
    from app.housing.lookup import build_changed
    from app.housing.operations import backup_database
    backup_database(force=True)
    started = utcnow()
    with scan_lock(), SessionLocal() as session:
        previous_events = {n.id: n.event_at for n in session.scalars(select(HousingNotice))}
        links = rebuild_links(session)
        lotteries = list(session.scalars(select(HousingNotice).where(
            HousingNotice.category == "lottery_result", HousingNotice.published_at >= started - timedelta(days=days))))
        launch_ids = set(session.scalars(select(HousingBatchUnit.source_notice_id)))
        keys = {n.batch_key_v2 for n in lotteries if n.batch_key_v2}
        launch_ids.update(session.scalars(select(HousingBatchAnchor.launch_id).where(HousingBatchAnchor.key.in_(keys))))
        launch_ids.update(session.scalars(select(HousingNotice.id).where(
            HousingNotice.category == "rent_launch", HousingNotice.application_end >= started)))
        session.commit()
        results = []
        async with HousingHttpClient() as client:
            from app.housing.models import HousingSource
            from app.housing.scan import _hydrate_detail
            for notice in session.scalars(select(HousingNotice).where(HousingNotice.published_unverified.is_(True))):
                source = session.get(HousingSource, notice.source_id) if notice.source_id else None
                if source:
                    await _hydrate_detail(session, client, source, notice, notice.canonical_url)
                    results.append({"notice_id": notice.id, "kind": "date", "verified": not notice.published_unverified})
                    session.commit()
            for launch_id in sorted(i for i in launch_ids if i):
                notice = session.get(HousingNotice, launch_id)
                notice.detail_checked_at = None
                rows = await ingest_units(session, client, notice, force=True)
                results.append({"notice_id": launch_id, "kind": "units", "rows": len(rows)})
                session.commit()
            for notice in lotteries:
                notice.detail_checked_at = None
                rows = await ingest_lottery(session, client, notice, force=True)
                results.append({"notice_id": notice.id, "kind": "lottery", "rows": len(rows),
                                "complete": all(r.complete for r in rows) if rows else False})
                session.commit()
        for notice in session.scalars(select(HousingNotice)):
            version = business_version(notice)
            if notice.business_version != version:
                notice.business_version = version
                notice.event_at = utcnow()
            if baseline:
                notice.event_at = previous_events.get(notice.id, notice.first_seen_at or started)
        session.commit()
    try:
        pages = await build_changed()
    except Exception:
        __import__('logging').getLogger(__name__).exception("repair lookup build failed")
        pages = {"status": "degraded", "reason": "构建失败，保留原页面，详见日志"}
    return {"links": links, "attachments": results, "lookup": pages, "baseline": baseline}
