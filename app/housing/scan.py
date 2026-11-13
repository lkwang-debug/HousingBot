from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.housing.config import HousingSettings, get_housing_settings
from app.housing.fetch import FetchError, HousingHttpClient
from app.housing.models import (
    HousingNotice,
    HousingRun,
    HousingSource,
    HousingSourceState,
    utcnow,
)
from app.housing.parsers import (
    parse_detail,
    parse_gkmlpt_index,
    parse_gkmlpt_payload,
    parse_nfcms_list,
    page_url_for,
)
from app.housing.parsers.list_gkmlpt import select_classify_ids
from app.housing.sources import SourceDef, get_source_def, sync_sources
from app.housing.store import store_notice

logger = logging.getLogger(__name__)

DETAIL_WINDOW_DAYS = 45
IN_FLIGHT_WINDOW_DAYS = 120
MAX_DETAIL_ATTEMPTS = 3
GKMLPT_PAGE_SIZE = 100


@dataclass
class ScanResult:
    run_id: int | None = None
    status: str = "ok"
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    new_pushable_ids: list[int] = field(default_factory=list)
    degraded: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _SourceOutcome:
    seen: int = 0
    created: int = 0
    updated: int = 0
    error: str | None = None
    error_kind: str | None = None
    etag: str | None = None
    last_modified: str | None = None


async def run_scan(
    *,
    dry_run: bool = False,
    only_sources: list[str] | None = None,
    max_pages: int | None = None,
    with_detail: bool = True,
    settings: HousingSettings | None = None,
    client: HousingHttpClient | None = None,
) -> ScanResult:
    settings = settings or get_housing_settings()
    result = ScanResult()
    if not settings.enabled:
        result.status = "skipped"
        return result

    session = SessionLocal()
    session.info["housing_dry_run"] = dry_run
    run: HousingRun | None = None
    try:
        from app.housing.groups import sync_groups
        from app.housing.outbox import scheduling_lock
        if not dry_run:
            with scheduling_lock():
                sync_groups(session)
                session.commit()
        source_rows = sync_sources(session)
        session.flush()
        # 首次运行只做基线，不把历史整页通告当“新消息”一次性推出去。
        baseline = (session.scalar(select(func.count(HousingNotice.id))) or 0) == 0
        selected = [
            row
            for row in source_rows
            if row.enabled and (not only_sources or row.key in only_sources)
        ]
        result.sources_total = len(selected)
        run = HousingRun(
            idempotency_key=f"scan:{utcnow():%Y%m%d%H%M%S}",
            run_type="scan",
            status="running",
            dry_run=dry_run,
            sources_total=len(selected),
        )
        if not dry_run:
            session.add(run)
            session.flush()
            session.commit()

        pages = max_pages or settings.max_pages
        created_ids: list[int] = []
        async with AsyncExitStack() as stack:
            active_client = client or await stack.enter_async_context(HousingHttpClient(settings))
            for source_row in selected:
                definition = get_source_def(source_row.key)
                if definition is None:
                    continue
                try:
                    outcome = await _scan_source(
                        session,
                        active_client,
                        source_row,
                        definition,
                        settings,
                        pages,
                        with_detail,
                        created_ids,
                    )
                except Exception as exc:  # 单源失败不能拖垮整轮
                    logger.exception("housing source failed", extra={"source_name": source_row.key})
                    outcome = _SourceOutcome(
                        error=f"{type(exc).__name__}: {exc}", error_kind="unexpected"
                    )
                _record_outcome(session, source_row, outcome)
                result.items_seen += outcome.seen
                result.items_new += outcome.created
                result.items_updated += outcome.updated
                if outcome.error:
                    result.sources_failed += 1
                    result.degraded.append(
                        {"source": source_row.key, "kind": outcome.error_kind, "error": outcome.error}
                    )
                else:
                    result.sources_ok += 1
                if not dry_run:
                    session.commit()

            # A 304 list response must not prevent daily checks of in-flight details.
            if with_detail:
                selected_ids = {s.id for s in selected}
                due = detail_refresh_candidates(session, selected_ids, now=utcnow())
                for notice in due:
                    if notice.source_id not in selected_ids:
                        continue
                    source = session.get(HousingSource, notice.source_id)
                    updated = await _hydrate_detail(session, active_client, source, notice, notice.canonical_url)
                    if updated:
                        await _ingest_attachments(session, active_client, updated)
                if not dry_run:
                    session.commit()

                # Retry review attachments independently of the 24h detail refresh.
                # A failed PDF must not wait for the next detail fetch to recover.
                from app.housing.qualification import review_files, ingest_review
                reviews = list(session.scalars(select(HousingNotice).where(
                    HousingNotice.is_target.is_(True),
                    HousingNotice.published_at >= utcnow()-timedelta(days=120),
                    HousingNotice.source_id.in_(selected_ids))))
                for notice in reviews:
                    if review_files(notice):
                        await ingest_review(session, active_client, notice)
                # Dynamic selection results change without a new announcement.
                # Check them independently from the 24h notice hydration cache.
                from app.housing.selection_progress import collect_notice,source_candidates
                from app.housing.reporting import allocation_profile
                checked=set()
                for notice in sorted(reviews,key=lambda n:n.id):
                    urls=source_candidates(notice)
                    identity=(notice.batch_key_v2,tuple(sorted(urls)))
                    if urls and identity not in checked and allocation_profile(notice).housing_type=='baozu':
                        checked.add(identity)
                        await collect_notice(session,active_client,notice)
                if not dry_run:
                    session.commit()

        session.flush()
        from app.housing.batches import rebuild_links
        rebuild_links(session)
        if baseline:
            session.execute(
                update(HousingNotice)
                .where(
                    HousingNotice.is_push_candidate.is_(True),
                    HousingNotice.instant_pushed_at.is_(None),
                )
                .values(instant_pushed_at=utcnow(), instant_push_status="baseline")
            )
            session.flush()
            from app.housing.models import HousingGroupState
            for state in session.scalars(select(HousingGroupState)):
                state.eligible_since = utcnow()
        _mark_stale_candidates(session, settings)
        result.new_pushable_ids = [notice.id for notice in collect_pending_push(session, 100000, settings)]
        result.run_id = run.id if run is not None else None
        result.status = "degraded" if result.degraded else "ok"
        if run is not None:
            run.status = result.status
            run.finished_at = utcnow()
            run.sources_ok = result.sources_ok
            run.sources_failed = result.sources_failed
            run.items_seen = result.items_seen
            run.items_new = result.items_new
            run.items_updated = result.items_updated
            run.degraded_reasons = result.degraded
        if dry_run:
            session.rollback()
        else:
            session.commit()
    except Exception as exc:
        session.rollback()
        result.status = "failed"
        result.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        session.close()
    return result


async def _scan_source(
    session: Session,
    client: HousingHttpClient,
    source_row: HousingSource,
    definition: SourceDef,
    settings: HousingSettings,
    max_pages: int,
    with_detail: bool,
    created_ids: list[int],
) -> _SourceOutcome:
    state = _state_for(session, source_row)
    now = utcnow()
    if state.circuit_open_until and state.circuit_open_until > now:
        return _SourceOutcome(error="熔断中", error_kind="circuit_open")
    if not session.info.get("housing_dry_run"):
        session.commit()

    items = []
    etag = None
    last_modified = None
    if definition.adapter == "gkmlpt":
        items, etag, last_modified = await _fetch_gkmlpt(
            client, definition, source_row, session, state, max_pages
        )
    else:
        for page in range(max_pages):
            url = page_url_for(definition.entry_url, page)
            try:
                response = await client.get(
                    url,
                    etag=state.etag if page == 0 else None,
                    last_modified=state.last_modified if page == 0 else None,
                )
            except FetchError as exc:
                if page == 0:
                    raise
                break  # 后续分页 404 属于正常结尾
            if page == 0:
                etag, last_modified = response.etag, response.last_modified
                if response.not_modified:
                    return _SourceOutcome(etag=etag, last_modified=last_modified)
            page_items = parse_nfcms_list(response.text, response.url)
            if not page_items:
                if page == 0:
                    raise FetchError("parse_empty", f"列表页 0 条: {url}")
                break
            items.extend(page_items)
            # 增量：整页都是已见过的条目就说明翻到底了，不必再往后翻。
            if page == 0 and _all_known(session, page_items):
                break

    if not items:
        raise FetchError("parse_empty", f"{definition.key} 未解析到任何条目")

    outcome = _SourceOutcome(etag=etag, last_modified=last_modified)
    cutoff = utcnow() - timedelta(days=DETAIL_WINDOW_DAYS)
    for item in items:
        notice, created = store_notice(
            session,
            source=source_row,
            title=item.title,
            url=item.url,
            published_at=item.published_at,
            doc_number=item.doc_number,
            attachments=item.attachments,
        )
        outcome.seen += 1
        outcome.created += int(created)
        outcome.updated += int(not created)
        if created:
            created_ids.append(notice.id)
        if not session.info.get("housing_dry_run"):
            session.commit()
        if not with_detail:
            continue
        if not _should_fetch_detail(notice, item, cutoff):
            # 详情早就抓过，但新加的附件统计可能还没做（老库升级后第一次扫描）。
            if notice.category in {"lottery_result", "rent_launch"}:
                await _ingest_attachments(session, client, notice)
            continue
        updated = await _hydrate_detail(session, client, source_row, notice, item.url)
        if updated is not None:
            await _ingest_attachments(session, client, updated)
    return outcome


async def _ingest_attachments(
    session: Session, client: HousingHttpClient, notice: HousingNotice
) -> None:
    """摇号结果 → 人数统计；配租通告 → 每档房源数。失败只记日志，不影响扫描结果。"""
    from app.housing.lottery import ingest_lottery, ingest_units
    if notice.category == "lottery_result" and notice.batch_link_status != "manual":
        from app.housing.batches import resolve_launch
        launches = list(session.scalars(select(HousingNotice).where(
            HousingNotice.category == "rent_launch", HousingNotice.region_code == notice.region_code)))
        notice.batch_key_v2, notice.batch_link_status = resolve_launch(notice, launches)

    try:
        if notice.category == "lottery_result":
            await ingest_lottery(session, client, notice)
        elif notice.category == "rent_launch":
            await ingest_units(session, client, notice)
            from app.housing.inventory import enrich_inventory
            await enrich_inventory(session, client, notice)
        elif notice.category == 'process':
            from app.housing.qualification import ingest_review
            await ingest_review(session, client, notice)
            from app.housing.selection_sync import ingest_selection
            await ingest_selection(session,client,notice)
    except Exception:  # noqa: BLE001 附件解析是增强功能，不能拖垮抓取
        logger.exception("housing attachment ingest failed", extra={"notice_id": notice.id})


async def _fetch_gkmlpt(
    client: HousingHttpClient,
    definition: SourceDef,
    source_row: HousingSource,
    session: Session,
    state: HousingSourceState,
    max_pages: int,
):
    items = []
    etag = None
    last_modified = None
    if definition.api_template:
        for page in range(max_pages):
            url = definition.api_template.format(page=page)
            response = await client.get(url, referer=definition.entry_url)
            if page == 0:
                etag, last_modified = response.etag, response.last_modified
            payload = _loads(response.text, url)
            page_items = parse_gkmlpt_payload(payload, response.url)
            items.extend(page_items)
            if len(page_items) < GKMLPT_PAGE_SIZE:
                break
            if page == 0 and _all_known(session, page_items):
                break
        return items, etag, last_modified

    index = await client.get(definition.entry_url)
    sid, tree = parse_gkmlpt_index(index.text)
    classify_ids = definition.classify_ids or select_classify_ids(tree)
    if not classify_ids:
        raise FetchError("parse_empty", f"{definition.key} 未找到可用的信息公开分类")
    if not sid:
        raise FetchError("parse_empty", f"{definition.key} 页面缺少 SID")

    origin = definition.origin
    org_path = urlsplit(definition.entry_url).path.split("/gkmlpt/")[0].strip("/")
    base = f"{origin}/{org_path}/gkmlpt/api/all"
    for classify_id in classify_ids:
        for page in range(max_pages):
            url = f"{base}/{classify_id}?page={page}&sid={sid}"
            response = await client.get(url, referer=definition.entry_url)
            if page == 0:
                etag = etag or response.etag
                last_modified = last_modified or response.last_modified
            payload = _loads(response.text, url)
            page_items = parse_gkmlpt_payload(payload, response.url)
            items.extend(page_items)
            if len(page_items) < GKMLPT_PAGE_SIZE:
                break
            if page == 0 and _all_known(session, page_items):
                break
    return items, etag, last_modified


def _all_known(session: Session, items) -> bool:
    """整页 URL 都已在库里时，后续分页只会有更旧的条目。"""
    from app.housing.dedup import canonical_key

    keys = {canonical_key(item.url) for item in items}
    if not keys:
        return True
    known = session.scalar(
        select(func.count(HousingNotice.id)).where(HousingNotice.canonical_key.in_(keys))
    )
    return int(known or 0) >= len(keys)


def _loads(text: str, url: str) -> dict:
    import json

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise FetchError("bad_json", f"响应不是合法 JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise FetchError("bad_json", f"响应结构异常: {url}")
    return payload


def _should_fetch_detail(notice: HousingNotice, item, cutoff: datetime) -> bool:
    now = utcnow()
    if notice.detail_checked_at and notice.detail_checked_at > now - timedelta(hours=24):
        return False
    missing_in_flight = bool((not notice.body_text or notice.detail_status != "ok")
                            and notice.published_at and notice.published_at >= now - timedelta(days=IN_FLIGHT_WINDOW_DAYS))
    if item.published_at and item.published_at < cutoff and not missing_in_flight and not notice.published_unverified and not (
        notice.application_end and notice.application_end >= now
    ):
        return False
    if notice.category in {"procurement", "excluded"}:
        return False
    return notice.is_push_candidate or notice.category in {"process", "other_housing", "policy"}


def detail_refresh_candidates(session, source_ids, *, now, limit=30):
    """Fill missing in-flight anchors even after the normal 45-day refresh window."""
    from app.housing.models import HousingBatchAnchor
    cutoff = now - timedelta(days=DETAIL_WINDOW_DAYS)
    missing = or_(HousingNotice.body_text.is_(None), HousingNotice.body_text == "", HousingNotice.detail_status != "ok")
    recent_keys = select(HousingNotice.batch_key_v2).where(
        HousingNotice.is_target.is_(True), HousingNotice.published_at >= cutoff,
        HousingNotice.batch_key_v2.is_not(None))
    launch_ids = select(HousingBatchAnchor.launch_id).where(HousingBatchAnchor.key.in_(recent_keys))
    return list(session.scalars(select(HousingNotice).where(
        HousingNotice.source_id.in_(source_ids), HousingNotice.is_target.is_(True),
        or_(HousingNotice.published_unverified.is_(True), HousingNotice.published_at >= cutoff,
            HousingNotice.application_end >= now,
            and_(missing, or_(HousingNotice.published_at >= now - timedelta(days=IN_FLIGHT_WINDOW_DAYS),
                             HousingNotice.id.in_(launch_ids)))),
        or_(HousingNotice.detail_checked_at.is_(None), HousingNotice.detail_checked_at <= now - timedelta(hours=24)),
    ).order_by(HousingNotice.published_unverified.desc(), missing.desc(), HousingNotice.published_at.desc()).limit(limit)))


async def _hydrate_detail(
    session: Session,
    client: HousingHttpClient,
    source_row: HousingSource,
    notice: HousingNotice,
    url: str,
) -> HousingNotice | None:
    try:
        if not session.info.get("housing_dry_run"):
            session.commit()
        response = await client.get(url, referer=source_row.entry_url)
        detail = parse_detail(response.text, response.url)
        if not detail.body_text.strip():
            raise FetchError("parse_empty", "详情正文为空，不能标记采集成功")
    except Exception as exc:
        notice.detail_checked_at = utcnow()
        notice.detail_attempts += 1
        if notice.detail_attempts >= MAX_DETAIL_ATTEMPTS:
            notice.detail_status = "failed"
        logger.info("housing detail failed: %s (%s)", url, exc)
        return None
    updated, _ = store_notice(
        session,
        source=source_row,
        title=detail.title or notice.title,
        url=url,
        published_at=detail.published_at or notice.published_at,
        body_text=detail.body_text,
        content_html=detail.content_html,
        attachments=detail.attachments or notice.attachments,
        detail_status="ok",
    )
    updated.detail_attempts = notice.detail_attempts + 1
    return updated


def _state_for(session: Session, source: HousingSource) -> HousingSourceState:
    state = session.scalar(
        select(HousingSourceState).where(HousingSourceState.source_id == source.id)
    )
    if state is None:
        state = HousingSourceState(source_id=source.id)
        session.add(state)
        session.flush()
    return state


def _record_outcome(session: Session, source: HousingSource, outcome: _SourceOutcome) -> None:
    state = _state_for(session, source)
    state.last_checked_at = utcnow()
    if outcome.error:
        state.consecutive_failures += 1
        state.last_error = outcome.error[:1000]
        state.last_verify_status = outcome.error_kind
        if state.consecutive_failures >= 5:
            state.circuit_open_until = utcnow() + timedelta(minutes=60)
    else:
        state.consecutive_failures = 0
        state.last_error = None
        state.last_verify_status = "ok"
        state.circuit_open_until = None
        if outcome.etag:
            state.etag = outcome.etag
        if outcome.last_modified:
            state.last_modified = outcome.last_modified


def _published_or_seen():
    return func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)


def _instant_cutoff(settings: HousingSettings) -> datetime:
    return utcnow() - timedelta(days=settings.instant_max_age_days)


def _mark_stale_candidates(session: Session, settings: HousingSettings) -> None:
    """历史补抓进来的旧通告不即时推送，只进早报，避免一次性刷屏。"""
    session.execute(
        update(HousingNotice)
        .where(
            HousingNotice.is_push_candidate.is_(True),
            HousingNotice.instant_pushed_at.is_(None),
            _published_or_seen() < _instant_cutoff(settings),
        )
        .values(instant_pushed_at=utcnow(), instant_push_status="stale")
    )
    session.flush()


def collect_pending_push(session, limit, settings=None):
    from app.housing.events import instant_event
    from app.housing.groups import load_groups
    from app.housing.models import HousingGroupState
    from app.housing.dedup import unique_notices
    from app.housing.outbox import handled_notice_ids, event_exists
    settings = settings or get_housing_settings()
    notices = list(session.scalars(select(HousingNotice).where(
        HousingNotice.is_push_candidate.is_(True), HousingNotice.published_unverified.is_(False),
        HousingNotice.event_at >= _instant_cutoff(settings),
        # event_at 会在业务版本变化时被刷成"现在"，老通告重扫一次就会被当成新事件。
        # 即时推送只认发布时间：超窗的只进早报，永远不补发"新通告"。
        _published_or_seen() >= _instant_cutoff(settings),
    ).order_by(HousingNotice.priority.desc(), HousingNotice.event_at.desc())))
    # 同一份通告从两个栏目各抓到一个 URL 会落成两行，推送里只认一份。
    notices = unique_notices(notices)
    pending = {}
    for group in load_groups():
        if not group.enabled:
            continue
        state = session.get(HousingGroupState, group.identity)
        if not state:
            continue
        handled = handled_notice_ids(session, group.identity, "instant")
        for notice in notices:
            if notice.id in handled:
                continue
            if notice.event_at and notice.event_at >= state.eligible_since and group.covers(notice) and not event_exists(
                    session, group.identity, "instant", instant_event(notice)):
                pending[notice.id] = notice
    return list(pending.values())[:limit]


def pending_push_count(session, settings=None):
    from app.housing.models import HousingOutbox
    queued = session.scalar(select(func.count(HousingOutbox.id)).where(
        HousingOutbox.status.in_(["pending", "blocked", "failed"]))) or 0
    return queued + len(collect_pending_push(session, 100000, settings))
