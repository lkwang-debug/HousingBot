from __future__ import annotations

import re
from datetime import date, datetime, time

CN_TZ = None  # 由调用方传入 ZoneInfo

DATE_RE = re.compile(r"(?P<year>20\d{2})\s*[-年/.]\s*(?P<month>\d{1,2})\s*[-月/.]\s*(?P<day>\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(?P<year>\d{2})\s*-\s*(?P<month>\d{1,2})\s*-\s*(?P<day>\d{1,2})(?!\d)")
CLOCK_RE = re.compile(r"(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2})")


def parse_date_text(text: str, today: date | None = None) -> date | None:
    """从列表项文本里解析发布日期，兼容 2026-10-20、26-10-20、［2026-10-10 ］。"""
    if not text:
        return None
    match = DATE_RE.search(text)
    if match:
        return _safe_date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    match = SHORT_DATE_RE.search(text)
    if match:
        year = 2000 + int(match.group("year"))
        return _safe_date(year, int(match.group("month")), int(match.group("day")))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_clock(text: str) -> time | None:
    match = CLOCK_RE.search(text or "")
    if not match:
        return None
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def to_local_naive(value: datetime, tz) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(tz).replace(tzinfo=None)
