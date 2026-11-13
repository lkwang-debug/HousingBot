"""Attachment parsing with retryable manifests and replace-as-a-whole aggregates."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import delete, select

from app.housing.batches import batch_key
from app.housing.models import HousingAttachmentState, HousingBatchUnit, HousingLotteryStat, utcnow

PARSER_VERSION = "housing-2.4"


def _state(session, notice, purpose):
    row = session.scalar(select(HousingAttachmentState).where(
        HousingAttachmentState.notice_id == notice.id, HousingAttachmentState.purpose == purpose))
    if row is None:
        row = HousingAttachmentState(notice_id=notice.id, purpose=purpose)
        session.add(row); session.flush()
    return row


def _manifest(notice, purpose):
    payload = [purpose, notice.business_version, notice.attachments, batch_key(notice)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _due(state, manifest, force):
    if force or state.manifest_hash != manifest or state.parser_version != PARSER_VERSION:
        return True
    return not state.next_attempt_at or state.next_attempt_at <= utcnow()


def _finish(state, manifest, hashes, errors, *, session=None):
    now = utcnow()
    if session is not None:
        from app.housing.models import HousingMeta
        key=f'attachment-observed:{state.notice_id}:{state.purpose}'
        observation=session.get(HousingMeta,key)
        if observation is None:observation=HousingMeta(key=key);session.add(observation)
        observation.value={'at':now.isoformat(),'manifest':manifest,'hashes':hashes,'errors':errors}
    state.manifest_hash, state.parser_version = manifest, PARSER_VERSION
    # Partial observations cannot replace the last verified attachment set.
    if not errors:
        state.content_hashes = hashes
    state.checked_at = now
    state.error = "; ".join(errors)[:1500] or None
    if errors:
        state.status = "partial" if hashes else "failed"
        state.failures += 1
        wait = timedelta(minutes=30) if state.failures == 1 else timedelta(hours=2) if state.failures == 2 else timedelta(days=1)
    else:
        state.status, state.failures = "ok", 0
        wait = timedelta(days=1)
    state.next_attempt_at = now + wait


def attachment_event(session, notice, *, purpose=None, semantic=None):
    """Same URL, different attachment bytes is still a new notice version."""
    from app.housing.events import business_version
    records = list(session.scalars(select(HousingAttachmentState).where(
        HousingAttachmentState.notice_id == notice.id).order_by(HousingAttachmentState.purpose)))
    if purpose is not None:
        from app.housing.models import HousingMeta
        key=f'attachment-semantics:{notice.id}'
        record=session.get(HousingMeta,key)
        previous=dict(record.value) if record else {}
        if previous.get(purpose)==semantic:return
        current={**previous,purpose:semantic}
        if record is None:
            record=HousingMeta(key=key,value=current);session.add(record)
        else:record.value=current
        # Seed old byte-hash versions without inventing a historical update.
        if purpose not in previous and notice.attachment_version:
            session.flush();return
        hashes=sorted(current.items())
    else:
        if any(record.status!='ok' for record in records):return
        hashes = [(record.purpose, record.content_hashes) for record in records if record.content_hashes]
    if not hashes:
        return
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if version == notice.attachment_version:
        return
    initial = not notice.attachment_version
    previous_version = notice.business_version
    notice.attachment_version = version
    notice.business_version = business_version(notice)
    notice.event_at = (notice.published_at if initial and notice.published_at and
                       notice.published_at < utcnow() - timedelta(days=3) else utcnow())
    if not initial:
        from app.housing.events import record_revision
        record_revision(session, notice, previous_version, at=notice.event_at, reason="official-attachment-changed")
    session.flush()


def row_issues(rows):
    counts = defaultdict(int)
    rounds = defaultdict(set)
    for row in rows:
        counts[row.apply_no.upper()] += 1
        rounds[row.apply_no.upper()].add(row.round)
    issues = []
    if any(not row.apply_no for row in rows):
        issues.append("存在缺少认租编号的记录")
    duplicates = [key for key, count in counts.items() if count > 1]
    if duplicates:
        multi = sum(len(rounds[key]) > 1 for key in duplicates)
        issues.append(f"存在 {len(duplicates)} 个重复编号，其中 {multi} 个涉及多轮；保留记录，暂停估算")
    if any(not row.queue or row.queue == "未标注" for row in rows):
        issues.append("部分队列未标注，不能确认队列优先关系")
    return issues


async def ingest_lottery(session, client, notice, *, force=False):
    from app.housing import lottery as lib
    if notice.category != "lottery_result":
        return []
    state = _state(session, notice, "lottery")
    manifest = _manifest(notice, "lottery")
    if not _due(state, manifest, force):
        return list(session.scalars(select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id)))
    if force:
        notice.detail_checked_at = None
    notice = await lib._ensure_detail(session, client, notice)
    manifest = _manifest(notice, "lottery")
    files = lib._pick_files(notice, lib.LOTTERY_NAME_TERMS)
    errors, hashes, grouped = [], {}, defaultdict(list)
    labels, urls = {}, {}
    if not files:
        errors.append("没有可解析的摇号附件")
    if len(files) > lib.MAX_FILES:
        errors.append("附件超过处理上限，统计不完整")
    for attachment in files[:lib.MAX_FILES]:
        url, label = attachment.get("url", ""), attachment.get("name", "")
        kind = lib._attachment_kind(attachment)
        if not url or kind not in {"pdf", "xls", "xlsx", "et"}:
            errors.append("存在不支持的名单附件类型"); continue
        try:
            if not session.info.get("housing_dry_run"):
                session.commit()
            data = await client.get_bytes(url, referer=notice.canonical_url)
            rows = lib.parse_pdf_rows(data) if kind == "pdf" else lib.parse_excel_rows(data, "xls" if kind == "et" else kind)
            if not rows:
                raise ValueError("没有识别到名单行")
            hashes[url] = hashlib.sha256(data).hexdigest()
            key = lib.category_key(label)
            prefix, hint = lib.queue_prefix(label), lib.queue_hint(label)
            for row in rows:
                row.queue = lib._queue_name(row.queue, prefix, hint)
                if row.queue == "未标注":
                    row.queue = ""
            grouped[key].extend(rows)
            labels[key], urls[key] = lib.category_display(label, key), url
        except Exception as exc:
            errors.append(f"附件解析失败：{type(exc).__name__}")
    # Replace the whole set, including partial results. Never add old and new totals.
    session.execute(delete(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id))
    session.flush()
    written = []
    for key, rows in grouped.items():
        issues = row_issues(rows)
        if any(row.queue == "未标注" for row in rows):
            issues.append("队列未核实")
        agg = lib.aggregate(rows, labels[key])
        row = HousingLotteryStat(notice_id=notice.id, batch_key=batch_key(notice) or f"unresolved:{notice.id}",
            region_code=notice.region_code, category_key=key, label=labels[key],
            total_rows=agg.rows, max_order=agg.max_order, queue_counts=agg.queue_counts,
            queue_ranges=agg.queue_ranges, source_url=urls[key], parsed=True,
            complete=not errors and not issues and bool(batch_key(notice)),
            error="; ".join(errors + issues) or ("批次未关联" if not batch_key(notice) else None))
        session.add(row); written.append(row)
    if not written:
        session.add(HousingLotteryStat(notice_id=notice.id, batch_key=batch_key(notice) or "",
            region_code=notice.region_code, category_key="unparsed", parsed=False, complete=False,
            error="; ".join(errors) or "解析失败"))
    all_issues = errors + [row.error for row in written if row.error]
    _finish(state, manifest, hashes, all_issues,session=session)
    if not errors:
        semantic=hashlib.sha256(json.dumps(sorted((key,r.apply_no.upper(),r.queue,r.order,r.round)
            for key,rows in grouped.items() for r in rows),ensure_ascii=False).encode()).hexdigest()
        attachment_event(session, notice,purpose='lottery',semantic=semantic)
    state.manifest_hash = _manifest(notice, "lottery")
    from app.housing.competition import count_records, save_evidence
    save_evidence(session, notice, [{"category": key, "label": labels[key],
        **count_records([(r.apply_no, r.queue, r.round, r.order) for r in rows], complete=not errors),
        "url": urls[key], "published_at": notice.published_at.isoformat() if notice.published_at else "",
        "queue_mode": "unknown", "error": "; ".join(errors + row_issues(rows))}
        for key, rows in grouped.items()])
    session.flush()
    return written


def parse_units_table(table):
    """Read explicit project + layout/area + unit columns; do not infer 1 room = 50m²."""
    from app.housing.lottery import category_key
    for header_at, cells in enumerate(table[:30]):
        def column(terms):
            return next((i for i, cell in enumerate(cells) if any(term in str(cell) for term in terms)), None)
        size = column(("户型", "面积"))
        units = column(("套数", "房源数量", "住房套数", "供应数量"))
        project = column(("项目名称", "小区名称"))
        if size is None or units is None:
            continue
        result = {}
        for row in table[header_at + 1:]:
            if len(row) <= max(size, units):
                continue
            label = str(row[size]).strip()
            if not label or "合计" in label:
                continue
            amount = re.fullmatch(r"\s*(\d{1,6})(?:\.0)?\s*(?:套)?\s*", str(row[units]))
            if not amount:
                continue
            if project is not None and project < len(row) and row[project]:
                label = str(row[project]).strip() + " " + label
            key = category_key(label)
            if key.startswith("unknown:"):
                continue
            value = int(amount.group(1))
            old = result.get(key, (label, 0))
            result[key] = (label, old[1] + value)
        return result
    return {}


async def ingest_units(session, client, notice, *, force=False):
    from app.housing import lottery as lib
    if notice.category != "rent_launch":
        return []
    key = batch_key(notice)
    if not key:
        return []
    state = _state(session, notice, "units")
    manifest = _manifest(notice, "units")
    if not _due(state, manifest, force):
        return list(session.scalars(select(HousingBatchUnit).where(HousingBatchUnit.batch_key == key)))
    if force:
        notice.detail_checked_at = None
    notice = await lib._ensure_detail(session, client, notice)
    manifest = _manifest(notice, "units")
    hashes, errors = {}, []
    found = lib.parse_units_overview(notice.body_text or "")
    source = notice.canonical_url
    if not found:
        files = lib._pick_files(notice, lib.UNITS_NAME_TERMS)
        if len(files) > lib.MAX_FILES:
            errors.append("房源附件超过处理上限")
        for attachment in files[:lib.MAX_FILES]:
            url, kind = attachment.get("url", ""), lib._attachment_kind(attachment)
            if not url or kind not in {"pdf", "xls", "xlsx", "et"}:
                errors.append("不支持的房源附件类型"); continue
            try:
                if not session.info.get("housing_dry_run"):
                    session.commit()
                data = await client.get_bytes(url, referer=notice.canonical_url)
                parsed = lib.parse_units_pdf(data) if kind == "pdf" else parse_units_table(lib._read_table(data, "xls" if kind == "et" else kind))
                if not parsed:
                    raise ValueError("房源表格未识别")
                hashes[url] = hashlib.sha256(data).hexdigest()
                for category, value in parsed.items():
                    if category in found:
                        errors.append("同一档位出现在多个附件，需核对后合并")
                        continue
                    found[category] = value
                source = url
            except Exception as exc:
                errors.append(f"房源解析失败：{type(exc).__name__}")
    if not found:
        errors.append("没有可确认的分档房源数")
    session.execute(delete(HousingBatchUnit).where(HousingBatchUnit.batch_key == key))
    session.flush()
    written = lib._store_units(session, notice, found, source_url=source) if found else []
    for row in written:
        row.verified = not errors
    _finish(state, manifest, hashes, errors,session=session)
    if not errors:
        semantic=hashlib.sha256(json.dumps(found,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        attachment_event(session, notice,purpose='units',semantic=semantic)
    state.manifest_hash = _manifest(notice, "units")
    session.flush()
    return written
