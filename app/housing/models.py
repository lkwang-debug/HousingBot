from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.housing.db import HousingBase


def utcnow() -> datetime:
    """统一使用 Asia/Shanghai 的 naive 时间：SQLite 不保留时区，混用会出错。"""
    return datetime.now(get_settings().tz).replace(tzinfo=None)


class HousingSource(HousingBase):
    __tablename__ = "housing_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(16), default="district")
    region_code: Mapped[str] = mapped_column(String(32), default="")
    entry_url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(32), default="nfcms")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HousingSourceState(HousingBase):
    __tablename__ = "housing_source_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("housing_sources.id", ondelete="CASCADE"), unique=True, index=True
    )
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(128))
    last_seen_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_verify_status: Mapped[str | None] = mapped_column(String(32))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HousingNotice(HousingBase):
    __tablename__ = "housing_notices"
    __table_args__ = (
        Index("ix_housing_notices_push_queue", "is_push_candidate", "instant_pushed_at"),
        Index("ix_housing_notices_open_window", "is_target", "application_end"),
        Index("ix_housing_notices_region_date", "region_code", "published_at"),
        Index("ix_housing_notices_category_date", "category", "published_at"),
        Index("ix_housing_notices_merge_key", "merge_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    canonical_url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    title_norm: Mapped[str] = mapped_column(Text, default="")
    doc_number: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    merge_key: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    department: Mapped[str | None] = mapped_column(String(128))
    region_code: Mapped[str] = mapped_column(String(32), default="")
    level: Mapped[str] = mapped_column(String(16), default="district")
    housing_types: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(32), default="other")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_target: Mapped[bool] = mapped_column(Boolean, default=False)
    is_push_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_reason: Mapped[str | None] = mapped_column(String(64))
    application_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    application_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    units_count: Mapped[int | None] = mapped_column(Integer)
    projects: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("housing_sources.id", ondelete="SET NULL")
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    detail_status: Mapped[str] = mapped_column(String(32), default="pending")
    detail_attempts: Mapped[int] = mapped_column(Integer, default=0)
    detail_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_unverified: Mapped[bool] = mapped_column(Boolean, default=False)
    business_version: Mapped[str] = mapped_column(String(64), default="")
    attachment_version: Mapped[str] = mapped_column(String(64), default="")
    event_at: Mapped[datetime | None] = mapped_column(DateTime)
    batch_key_v2: Mapped[str | None] = mapped_column(String(128), index=True)
    batch_link_status: Mapped[str] = mapped_column(String(32), default="unresolved")
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    instant_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instant_push_status: Mapped[str | None] = mapped_column(String(32))
    digest_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    digest_push_status: Mapped[str | None] = mapped_column(String(32))
    last_pushed_content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HousingNoticeOccurrence(HousingBase):
    __tablename__ = "housing_notice_occurrences"
    __table_args__ = (UniqueConstraint("source_id", "url_hash", name="uq_occurrence_source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("housing_notices.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("housing_sources.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingExitRecord(HousingBase):
    __tablename__ = "housing_exit_records"
    __table_args__ = (
        UniqueConstraint("row_fingerprint", name="uq_exit_row_fingerprint"),
        Index("ix_housing_exit_community", "community_name", "housing_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int | None] = mapped_column(
        ForeignKey("housing_notices.id", ondelete="SET NULL"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(64), default="")
    row_no: Mapped[int | None] = mapped_column(Integer)
    housing_type: Mapped[str | None] = mapped_column(String(64))
    area_sqm: Mapped[float | None] = mapped_column(Float)
    community_name: Mapped[str | None] = mapped_column(String(128))
    region_code: Mapped[str] = mapped_column(String(32), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingRun(HousingBase):
    __tablename__ = "housing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    run_type: Mapped[str] = mapped_column(String(16), default="scan")
    status: Mapped[str] = mapped_column(String(16), default="running")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources_total: Mapped[int] = mapped_column(Integer, default=0)
    sources_ok: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    pushed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    degraded_reasons: Mapped[list] = mapped_column(JSON, default=list)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class HousingDelivery(HousingBase):
    __tablename__ = "housing_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_key", "attempt_number", name="uq_housing_delivery_attempt"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(128), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16), default="instant")
    channel: Mapped[str] = mapped_column(String(16), default="wechat")
    status: Mapped[str] = mapped_column(String(16), default="created")
    title: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    notice_ids: Mapped[list] = mapped_column(JSON, default=list)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    response_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingDispatchItem(HousingBase):
    __tablename__ = "housing_dispatch_items"
    __table_args__ = (
        UniqueConstraint("delivery_id", "notice_id", "section", name="uq_dispatch_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("housing_deliveries.id", ondelete="CASCADE"), index=True
    )
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("housing_notices.id", ondelete="CASCADE"), index=True
    )
    section: Mapped[str] = mapped_column(String(32), default="instant")


class HousingLotteryStat(HousingBase):
    """摇号结果的聚合统计，按户型档一行。

    只存"这档多少人、各队列多少人、顺序号区间"，不存姓名和证件号：
    摇号名单是公开的，但把它逐行搬进数据库就是另一回事了——没有必要，也有风险。
    """

    __tablename__ = "housing_lottery_stats"
    __table_args__ = (UniqueConstraint("notice_id", "category_key", name="uq_lottery_stat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("housing_notices.id", ondelete="CASCADE"), index=True
    )
    batch_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    region_code: Mapped[str] = mapped_column(String(32), default="")
    category_key: Mapped[str] = mapped_column(String(16), default="all")
    label: Mapped[str] = mapped_column(String(128), default="")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    max_order: Mapped[int | None] = mapped_column(Integer)
    queue_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    queue_ranges: Mapped[dict] = mapped_column(JSON, default=dict)
    source_url: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[bool] = mapped_column(Boolean, default=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingBatchUnit(HousingBase):
    """每个批次、每个户型档的房源套数，来自配租通告的《房源概况》附件。"""

    __tablename__ = "housing_batch_units"
    __table_args__ = (UniqueConstraint("batch_key", "category_key", name="uq_batch_unit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(128), index=True)
    region_code: Mapped[str] = mapped_column(String(32), default="")
    category_key: Mapped[str] = mapped_column(String(16), default="all")
    label: Mapped[str] = mapped_column(String(128), default="")
    units: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_notice_id: Mapped[int | None] = mapped_column(
        ForeignKey("housing_notices.id", ondelete="SET NULL")
    )
    source_url: Mapped[str] = mapped_column(Text, default="")
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingWatchRule(HousingBase):
    __tablename__ = "housing_watch_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, default="default")
    regions: Mapped[list] = mapped_column(JSON, default=list)
    housing_types: Mapped[list] = mapped_column(JSON, default=list)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    instant_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HousingBatchAnchor(HousingBase):
    __tablename__ = "housing_batch_anchors"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    launch_id: Mapped[int] = mapped_column(ForeignKey("housing_notices.id"), unique=True)
    region: Mapped[str] = mapped_column(String(32), default="")
    housing_type: Mapped[str] = mapped_column(String(32), default="unknown")
    audience: Mapped[str] = mapped_column(String(16), default="unknown")
    year: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int | None] = mapped_column(Integer)
    doc_number: Mapped[str | None] = mapped_column(String(64))


class HousingGroupState(HousingBase):
    __tablename__ = "housing_group_states"
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    topic: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    eligible_since: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HousingOutbox(HousingBase):
    __tablename__ = "housing_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)


class HousingOutboxEvent(HousingBase):
    __tablename__ = "housing_outbox_events"
    __table_args__ = (UniqueConstraint("group_id", "kind", "event_key", name="uq_group_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    event_key: Mapped[str] = mapped_column(String(128))
    notice_id: Mapped[int | None] = mapped_column(ForeignKey("housing_notices.id", ondelete="SET NULL"))
    outbox_id: Mapped[int] = mapped_column(ForeignKey("housing_outbox.id"), index=True)


class HousingAttachmentState(HousingBase):
    __tablename__ = "housing_attachment_state"
    __table_args__ = (UniqueConstraint("notice_id", "purpose", name="uq_attachment_purpose"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    notice_id: Mapped[int] = mapped_column(ForeignKey("housing_notices.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(16))
    parser_version: Mapped[str] = mapped_column(String(32), default="")
    manifest_hash: Mapped[str] = mapped_column(String(64), default="")
    content_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class HousingMeta(HousingBase):
    __tablename__ = "housing_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
