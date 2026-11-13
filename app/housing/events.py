"""Stable business events: polling timestamps and presentation do not create messages."""
from __future__ import annotations

import hashlib
import json
import re

from app.housing.qualify import requirements


def business_version(notice) -> str:
    def clean(value):
        return re.sub(r"\s+", "", str(value or ""))
    evidence = [line for line in re.split(r"[。；\n]+", notice.body_text or "")
                if any(term in line for term in ("截止", "房源", "套", "资格", "认租时间", "选房", "签约", "摇号"))]
    payload = {
        "title": clean(notice.title), "category": notice.category,
        "start": str(notice.application_start or ""), "end": str(notice.application_end or ""),
        "units": notice.units_count, "projects": sorted(notice.projects or []),
        "requirements": [clean(line) for line in requirements(notice)],
        "evidence": [clean(line) for line in evidence],
        "attachments": sorted((a.get("url", ""), clean(a.get("name"))) for a in notice.attachments or []),
    }
    if notice.attachment_version:
        payload["attachment_content"] = notice.attachment_version
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def instant_event(notice) -> str:
    identity = hashlib.sha256(notice.canonical_key.encode()).hexdigest()[:24]
    return f"notice:{identity}:{notice.business_version or business_version(notice)}"


def record_revision(session, notice, previous_version, *, at, reason):
    """Evidence for future report updates; polling/initial hydration is not a revision."""
    if not previous_version or previous_version == notice.business_version:
        return
    from app.housing.models import HousingMeta
    key = f"notice-revisions:{notice.id}"
    record = session.get(HousingMeta, key)
    if record is None:
        record = HousingMeta(key=key, value={"revisions": []}); session.add(record)
    revisions = list(record.value.get("revisions", []))
    item = {"from": previous_version, "to": notice.business_version, "at": at.isoformat(), "reason": reason}
    if item not in revisions:
        revisions.append(item)
    record.value = {"revisions": revisions[-200:]}
