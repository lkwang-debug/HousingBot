from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.housing.config import get_housing_settings
from app.housing.models import HousingSource


class SourceDef(BaseModel):
    key: str
    name: str
    level: str = "district"
    region_code: str = ""
    entry_url: str
    adapter: str = "nfcms"
    priority: int = Field(default=50, ge=0, le=100)
    enabled: bool = True
    classify_ids: list[int] = Field(default_factory=list)
    api_template: str | None = None
    request_delay_ms: int | None = None
    table_kind: str | None = None

    @property
    def origin(self) -> str:
        parts = urlsplit(self.entry_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


@lru_cache
def load_source_defs() -> tuple[SourceDef, ...]:
    path = get_housing_settings().sources_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(SourceDef.model_validate(item) for item in payload["sources"])


def get_source_def(key: str) -> SourceDef | None:
    for item in load_source_defs():
        if item.key == key:
            return item
    return None


def sync_sources(session: Session) -> list[HousingSource]:
    """把 sources.json 同步进数据库；不删除已下线源，只标记 enabled。"""
    existing = {row.key: row for row in session.scalars(select(HousingSource))}
    result: list[HousingSource] = []
    for definition in load_source_defs():
        row = existing.get(definition.key)
        if row is None:
            row = HousingSource(key=definition.key)
            session.add(row)
        row.name = definition.name
        row.level = definition.level
        row.region_code = definition.region_code
        row.entry_url = definition.entry_url
        row.adapter = definition.adapter
        row.priority = definition.priority
        row.enabled = definition.enabled
        row.config_hash = definition.config_hash
        result.append(row)
    session.flush()
    return result


def source_row_for(session: Session, key: str) -> HousingSource | None:
    return session.scalar(select(HousingSource).where(HousingSource.key == key))
