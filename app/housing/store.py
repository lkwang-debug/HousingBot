from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.housing.classify import classify
from app.housing.dedup import (
    canonical_key,
    clean_title,
    content_hash,
    merge_key,
    normalize_title,
    title_similarity,
    url_hash,
)
from app.housing.extract import extract_doc_number, extract_fields
from app.housing.models import (
    HousingExitRecord,
    HousingNotice,
    HousingNoticeOccurrence,
    HousingSource,
    utcnow,
)
from app.housing.parsers.table import extract_exit_rows, filter_projects


def store_notice(
    session: Session,
    *,
    source: HousingSource,
    title: str,
    url: str,
    published_at: datetime | None = None,
    doc_number: str | None = None,
    body_text: str = "",
    content_html: str = "",
    attachments: list | None = None,
    detail_status: str = "pending",
) -> tuple[HousingNotice, bool]:
    now = utcnow()
    # 列表锚文本可能带发布日期（坪山等），统一在这里清洗，避免各适配器重复处理。
    title = clean_title(title)
    key = canonical_key(url)
    doc_number = doc_number or extract_doc_number(title) or extract_doc_number(body_text)
    published_date = published_at.date() if published_at else None
    current_merge_key = merge_key(doc_number, title, published_date)
    current_content_hash = content_hash(title, published_date, body_text)

    notice = session.scalar(select(HousingNotice).where(HousingNotice.canonical_key == key))
    if notice is None and doc_number:
        # 文号是“批次级”的，同一批次的配租通告/选房结果文号相同，
        # 必须再比标题相似度，否则会把不同通告合并掉。
        candidates = session.scalars(
            select(HousingNotice).where(HousingNotice.doc_number == doc_number).limit(20)
        ).all()
        for candidate in candidates:
            if title_similarity(candidate.title, title) >= 0.98:
                notice = candidate
                break
    if notice is None:
        notice = _find_merge_candidate(session, current_merge_key, title, published_at)

    created = notice is None
    if created:
        notice = HousingNotice(
            canonical_key=key,
            canonical_url=url,
            url_hash=url_hash(url),
            title=title,
            title_norm=normalize_title(title),
            merge_key=current_merge_key,
            first_seen_at=now,
        )
        session.add(notice)
        session.flush()
    previous_body = notice.body_text
    previous_version = notice.business_version
    def inputs():
        return json.dumps([notice.title,notice.category,notice.application_start,notice.application_end,
            notice.units_count,notice.projects,notice.attachments,notice.attachment_version],sort_keys=True,default=str,ensure_ascii=False)
    previous_inputs=inputs()
    previous_detail_ok = notice.detail_status == "ok" and bool(previous_body)

    if source.priority >= _current_source_priority(session, notice) or created:
        notice.canonical_url = url
        notice.url_hash = url_hash(url)
        notice.source_id = source.id
    if not (notice.detail_status == "ok" and detail_status != "ok" and ("..." in title or "…" in title)):
        notice.title = title
    notice.title_norm = normalize_title(title)
    notice.doc_number = doc_number or notice.doc_number
    notice.merge_key = current_merge_key or notice.merge_key
    if published_at and published_at.date() > now.date():
        notice.published_unverified = True
        published_at = None
    elif published_at and (detail_status == "ok" or notice.detail_status != "ok" or notice.published_unverified or notice.published_at is None):
        notice.published_at = published_at
        notice.published_unverified = False
    notice.last_seen_at = now
    notice.level = source.level
    if not notice.region_code:
        notice.region_code = source.region_code

    if body_text and detail_status == "ok":
        notice.body_text = body_text
        notice.detail_status = detail_status
        notice.content_hash = current_content_hash
    elif created:
        notice.content_hash = current_content_hash
        notice.detail_status = detail_status

    notice.attachments = attachments or notice.attachments or []
    _apply_classification(notice, published_at, content_html)
    _upsert_occurrence(session, notice, source, url)
    _store_exit_rows(session, notice, source, content_html, published_at)
    if detail_status == "ok":
        notice.detail_checked_at = now
    from app.housing.events import business_version
    version = business_version(notice)
    if previous_version and previous_body and previous_inputs==inputs() and re.sub(r'\s+','',previous_body)==re.sub(r'\s+','',notice.body_text or ''):
        version=previous_version
    if version != notice.business_version:
        notice.business_version = version
        notice.event_at = (notice.published_at if (created or not previous_body) and notice.published_at
                           and (now - notice.published_at).days > 3 else now)
        if previous_detail_ok and detail_status == "ok":
            from app.housing.events import record_revision
            record_revision(session, notice, previous_version, at=now, reason="official-detail-changed")
    if notice.category == "rent_launch":
        from app.housing.batches import batch_key
        notice.batch_key_v2 = batch_key(notice)
        notice.batch_link_status = "anchor"
        if content_html:
            from app.housing.inventory import capture_detail
            capture_detail(session, notice, content_html)
    session.flush()  # autoflush=False，先落盘才能数到本轮新增的 occurrence
    notice.occurrence_count = int(
        session.scalar(
            select(func.count(HousingNoticeOccurrence.id)).where(
                HousingNoticeOccurrence.notice_id == notice.id
            )
        )
        or 1
    )
    return notice, created


def _apply_classification(
    notice: HousingNotice,
    published_at: datetime | None,
    content_html: str = "",
    reextract: bool = True,
) -> None:
    result = classify(notice.title, notice.body_text or "", notice.region_code)
    notice.category = result.category
    notice.priority = result.priority
    notice.is_target = result.is_target
    notice.is_push_candidate = result.is_push_candidate
    notice.exclude_reason = result.exclude_reason
    if result.housing_types:
        notice.housing_types = result.housing_types
    if result.region_code:
        notice.region_code = result.region_code

    if reextract and notice.body_text and (content_html or not notice.summary):
        extracted = extract_fields(notice.title, notice.body_text, content_html, published_at)
        notice.doc_number = notice.doc_number or extracted.doc_number
        if extracted.units_count is not None and (content_html or notice.units_count is None):
            notice.units_count = extracted.units_count
        # 表格抽取比正文正则准；没有 HTML 时不要把已有的好结果覆盖成噪声。
        if extracted.projects and (content_html or not notice.projects):
            notice.projects = extracted.projects
        notice.summary = extracted.summary
        if content_html or extracted.window.start or extracted.window.end:
            notice.application_start = extracted.window.start
            notice.application_end = extracted.window.end
            notice.time_inferred = extracted.window.inferred
        notice.needs_manual_review = bool(
            notice.is_push_candidate
            and not notice.application_end
            and notice.category == "rent_launch"
        )
    # 项目名清洗独立于正文抽取，reclassify 才能修好历史数据。
    notice.projects = filter_projects(notice.projects)


def _find_merge_candidate(session: Session, key: str, title: str, published_at: datetime | None):
    if not key:
        return None
    candidates = session.scalars(
        select(HousingNotice).where(HousingNotice.merge_key == key).limit(20)
    ).all()
    for candidate in candidates:
        if title_similarity(candidate.title, title) >= 0.98:
            if published_at is None or candidate.published_at is None:
                return candidate
            if abs((candidate.published_at - published_at).total_seconds()) <= 7 * 86400:
                return candidate
    return None


def _current_source_priority(session: Session, notice: HousingNotice) -> int:
    if not notice.source_id:
        return -1
    source = session.get(HousingSource, notice.source_id)
    return source.priority if source else -1


def _upsert_occurrence(
    session: Session, notice: HousingNotice, source: HousingSource, url: str
) -> None:
    # SessionLocal 关闭了 autoflush，先落盘才能看到本轮已插入的 occurrence。
    session.flush()
    digest = url_hash(url)
    existing = session.scalar(
        select(HousingNoticeOccurrence).where(
            HousingNoticeOccurrence.source_id == source.id,
            HousingNoticeOccurrence.url_hash == digest,
        )
    )
    if existing:
        existing.notice_id = notice.id
        existing.last_seen_at = utcnow()
        return
    session.add(
        HousingNoticeOccurrence(
            notice_id=notice.id,
            source_id=source.id,
            url=url,
            url_hash=digest,
        )
    )


def _store_exit_rows(
    session: Session,
    notice: HousingNotice,
    source: HousingSource,
    content_html: str,
    published_at: datetime | None,
) -> None:
    if not content_html or notice.category != "vacancy_exit":
        # 配租通告里的“序号+小区名称”表是房源清单，不是退出名单。
        return
    session.flush()
    rows = extract_exit_rows(content_html)
    for row in rows:
        fingerprint = hashlib.sha256(
            "|".join(
                [
                    source.key,
                    str(row.get("community_name") or ""),
                    str(row.get("housing_type") or ""),
                    str(row.get("area_sqm") or ""),
                ]
            ).encode("utf-8")
        ).hexdigest()
        exists = session.scalar(
            select(HousingExitRecord.id).where(HousingExitRecord.row_fingerprint == fingerprint)
        )
        if exists:
            continue
        session.add(
            HousingExitRecord(
                notice_id=notice.id,
                source_key=source.key,
                row_no=row.get("row_no"),
                housing_type=row.get("housing_type"),
                area_sqm=row.get("area_sqm"),
                community_name=row.get("community_name"),
                region_code=notice.region_code,
                published_at=published_at,
                row_fingerprint=fingerprint,
            )
        )


def notice_to_dict(notice: HousingNotice) -> dict:
    return {
        "id": notice.id,
        "title": notice.title,
        "url": notice.canonical_url,
        "published_at": notice.published_at.isoformat() if notice.published_at else None,
        "region": notice.region_code,
        "category": notice.category,
        "priority": notice.priority,
        "is_target": notice.is_target,
        "is_push_candidate": notice.is_push_candidate,
        "housing_types": notice.housing_types,
        "doc_number": notice.doc_number,
        "units_count": notice.units_count,
        "projects": notice.projects,
        "application_start": notice.application_start.isoformat() if notice.application_start else None,
        "application_end": notice.application_end.isoformat() if notice.application_end else None,
        "detail_status": notice.detail_status,
        "needs_manual_review": notice.needs_manual_review,
        "batch": notice.batch_key_v2,
        "batch_link_status": notice.batch_link_status,
        "published_unverified": notice.published_unverified,
        "start_status": "已核实" if notice.application_start else "开始时间未核实",
    }
