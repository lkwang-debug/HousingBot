from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import PROJECT_ROOT


def _parse_clock(value: str) -> time | None:
    value = value.strip()
    if not value or ":" not in value:
        return None
    hour, minute = value.split(":", 1)
    try:
        return time(int(hour), int(minute))
    except ValueError:
        return None


class HousingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="HOUSING_",
    )

    enabled: bool = True
    max_pages: int = Field(default=3, ge=1, le=10)
    backfill_pages: int = Field(default=5, ge=1, le=20)
    overlap_hours: int = Field(default=48, ge=0, le=720)
    http_concurrency: int = Field(default=3, ge=1, le=8)
    request_delay_ms: int = Field(default=800, ge=0, le=10000)
    timeout_seconds: float = Field(default=20.0, ge=5, le=120)
    instant_min_priority: int = Field(default=60, ge=0, le=100)
    instant_max_items: int = Field(default=8, ge=1, le=20)
    # 只在通告发布后这么多天内即时推送；更早的是历史补抓，进早报即可。
    instant_max_age_days: int = Field(default=3, ge=1, le=30)
    digest_hour: int = Field(default=8, ge=0, le=23)
    digest_minute: int = Field(default=30, ge=0, le=59)
    regions: str = ""
    types: str = "baozu,gongzu"
    quiet_windows: str = ""
    scan_lock_timeout_minutes: int = Field(default=30, ge=1, le=240)
    retention_days: int = Field(default=730, ge=30)
    # 自助查询页地址（tools/build_lottery_lookup.py 生成的静态页）。
    # 留空则推送里不出现"自己查排位"的引导，避免承诺一个还不存在的入口。
    lookup_url: str = ""
    # Fail closed by default. Legacy is explicit migration/offline-test compatibility.
    # Signed mode never falls back to the legacy client-side date gate.
    lookup_access_mode: Literal['legacy', 'signed'] = 'signed'
    lookup_signing_secret: SecretStr = SecretStr('')
    lookup_api_url: str = ''

    @property
    def region_filter(self) -> set[str]:
        return {item.strip() for item in self.regions.split(",") if item.strip()}

    @property
    def type_filter(self) -> set[str]:
        return {item.strip() for item in self.types.split(",") if item.strip()}

    @property
    def quiet_ranges(self) -> list[tuple[time, time]]:
        ranges: list[tuple[time, time]] = []
        for chunk in self.quiet_windows.split(","):
            if "-" not in chunk:
                continue
            start_text, end_text = chunk.split("-", 1)
            start = _parse_clock(start_text)
            end = _parse_clock(end_text)
            if start and end:
                ranges.append((start, end))
        return ranges

    @property
    def lock_path(self) -> Path:
        return PROJECT_ROOT / "data" / "housing.scan.lock"

    @property
    def groups_path(self) -> Path:
        """群组订阅配置；文件不存在就退回一对一推送。"""
        return PROJECT_ROOT / "data" / "groups.json"

    @property
    def sources_path(self) -> Path:
        return Path(__file__).resolve().parent / "sources.json"


@lru_cache
def get_housing_settings() -> HousingSettings:
    return HousingSettings()
