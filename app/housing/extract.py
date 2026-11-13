from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.housing.parsers.table import extract_project_names, is_project_name

DATE_TOKEN_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?"
    r"(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日"
    r"(?:\s*(?P<hour>\d{1,2})\s*(?:[:：]\s*(?P<minute>\d{2})|时))?"
)
RANGE_SEP_RE = re.compile(r"(?:起至|至|到|截至|—|–|-|－|~|～)")
BARE_DAY_END_RE = re.compile(r"(?:起至|至|到|截至|—|–|-|－|~|～)\s*(?P<day>\d{1,2})\s*日")
TIME_ANCHORS = (
    "网上认租时间",
    "认租申请时间",
    "认租时间",
    "受理申请时间",
    "受理时间",
    "申请时间",
    "申报时间",
)
DEADLINE_ANCHORS = ("认租截止时间", "申请截止时间", "受理截止时间")
UNIT_RE = re.compile(r"(\d{1,6})\s*套")
DOC_NUMBER_RE = re.compile(r"住保[租售]〔\d{4}〕\d+号")
# 从“配租 A、B、C 等项目”里拆出来的非项目片段。
PROJECT_LIST_RE = re.compile(
    r"(?:配租|筹集|推出|供应)([^。；\n]{2,300}?)(?:等)?[一二两三四五六七八九十百\d]{0,3}个?"
    r"(?:保障性租赁住房|公共租赁住房|保租房|公租房)?(?:项目|房源)"
)


@dataclass
class RentWindow:
    start: datetime | None = None
    end: datetime | None = None
    inferred: bool = False


@dataclass
class ReviewInfo:
    """Explicit review-publicity dates and counts; never an application window."""
    start: date | None = None
    end: date | None = None
    eligible: int | None = None
    ineligible: int | None = None
    evidence: str = ""
    notice_id: int | None = None
    label: str = "核查公示"


def extract_review_info(text: str, published: datetime | None = None) -> ReviewInfo:
    text = re.sub(r"\s+", " ", text or "")
    periods = {}
    for anchor in re.finditer(r"公示(?:时间(?:和方式)?|期限|期)(?:为|[：:]|\s)", text):
        context = text[anchor.start():anchor.end() + 180]
        tokens = list(DATE_TOKEN_RE.finditer(context))
        if len(tokens) < 2:
            continue
        first, second = tokens[:2]
        # A whole date pair, not two unrelated dates in adjacent sentences.
        if not re.fullmatch(r"\s*(?:起至|至|到|—|–|-|－|~|～)\s*", context[first.end():second.start()]):
            continue
        year = int(first.group("year")) if first.group("year") else published.year if published else None
        if year is None:
            continue
        try:
            start = date(year, int(first.group("month")), int(first.group("day")))
            end_year = int(second.group("year")) if second.group("year") else year
            if not second.group("year") and int(second.group("month")) < start.month:
                end_year += 1
            end = date(end_year, int(second.group("month")), int(second.group("day")))
        except ValueError:
            continue
        if end < start or (end - start).days > 90:
            continue
        periods[(start, end)] = context[:second.end()]
    result = ReviewInfo(label="初审公示" if "初审" in text else "核查公示")
    if len(periods) == 1:
        (result.start, result.end), result.evidence = next(iter(periods.items()))
    for attr, prefix in (("eligible", r"(?<!不)合格"), ("ineligible", "不合格")):
        values = {int(m) for m in re.findall(prefix + r"(?:认租)?家庭(?:共|为|有)?\s*(\d+)\s*户", text)}
        if len(values) == 1:
            setattr(result, attr, values.pop())
    return result


@dataclass
class ExtractedFields:
    doc_number: str | None = None
    units_count: int | None = None
    projects: list[str] = field(default_factory=list)
    window: RentWindow = field(default_factory=RentWindow)
    summary: str = ""


def extract_fields(title: str, body: str, content_html: str = "", published: datetime | None = None) -> ExtractedFields:
    text = body or ""
    projects = extract_project_names(content_html)
    if not projects:
        projects = _projects_from_text(text)
    return ExtractedFields(
        doc_number=extract_doc_number(title) or extract_doc_number(text),
        units_count=extract_units(text),
        projects=projects[:50],
        window=extract_rent_window(text, published),
        summary=_summary(text),
    )


def extract_doc_number(text: str) -> str | None:
    match = DOC_NUMBER_RE.search(text or "")
    return match.group(0) if match else None


def extract_units(text: str) -> int | None:
    if not text:
        return None
    for match in UNIT_RE.finditer(text):
        value = int(match.group(1))
        if 1 <= value <= 100_000:
            return value
    return None


def extract_rent_window(text: str, published: datetime | None) -> RentWindow:
    if not text:
        return RentWindow()
    published_date = published.date() if published else date.today()

    for anchor in TIME_ANCHORS:
        index = text.find(anchor)
        if index < 0:
            continue
        window_text = text[index : index + 260]
        tokens = list(DATE_TOKEN_RE.finditer(window_text))
        if tokens and re.search(r"截至|截止", window_text[:tokens[0].start()]):
            end, _, inferred = _build_start(tokens[0], published_date, default=time(18, 0))
            return RentWindow(start=None, end=end, inferred=inferred)
        if len(tokens) >= 2:
            first, second = tokens[0], tokens[1]
            if not RANGE_SEP_RE.fullmatch(window_text[first.end() : second.start()].strip()):
                continue
            return _build_window(first, second, published_date)
        if len(tokens) == 1:
            bare = BARE_DAY_END_RE.search(window_text[tokens[0].end() :])
            if bare:
                return _build_window_with_bare_day(tokens[0], int(bare.group("day")), published_date)

    for anchor in DEADLINE_ANCHORS:
        index = text.find(anchor)
        if index < 0:
            continue
        token = DATE_TOKEN_RE.search(text[index : index + 200])
        if token:
            end, _, inferred = _build_start(token, published_date, default=time(18, 0))
            return RentWindow(start=None, end=end, inferred=inferred)
    return RentWindow()


def _build_window(first, second, published_date: date) -> RentWindow:
    start, _, start_inferred = _build_start(first, published_date, default=time(9, 0))
    end, _, end_inferred = _build_start(second, published_date, default=time(18, 0))
    if start and end and end < start:
        end = end.replace(year=end.year + 1)
    return RentWindow(start=start, end=end, inferred=start_inferred or end_inferred)


def _build_window_with_bare_day(first, day: int, published_date: date) -> RentWindow:
    start, _, start_inferred = _build_start(first, published_date, default=time(9, 0))
    if start is None:
        return RentWindow()
    try:
        end = start.replace(day=day, hour=18, minute=0)
    except ValueError:
        return RentWindow(start=start, end=None, inferred=start_inferred)
    if end < start:
        month = end.month + 1
        year = end.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        try:
            end = end.replace(year=year, month=month)
        except ValueError:
            return RentWindow(start=start, end=None, inferred=start_inferred)
    return RentWindow(start=start, end=end, inferred=start_inferred)


def _build_start(token, published_date: date, default: time) -> tuple[datetime | None, time, bool]:
    year_text = token.group("year")
    month = int(token.group("month"))
    day = int(token.group("day"))
    inferred = False
    if year_text:
        year = int(year_text)
    else:
        year = published_date.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None, default, True
        if candidate < published_date - timedelta(days=30):
            year += 1
    hour = int(token.group("hour")) if token.group("hour") else default.hour
    if token.group("hour") is None:
        inferred = True
    minute = int(token.group("minute")) if token.group("minute") else (0 if token.group("hour") else default.minute)
    try:
        value = datetime(year, month, day, hour, minute)
    except ValueError:
        return None, default, True
    return value, default, inferred


def _projects_from_text(text: str) -> list[str]:
    """正文里“配租 A、B、C 等项目”的枚举比正则整句更可靠，按顿号拆开再过滤。"""
    names: list[str] = []
    seen: set[str] = set()
    for match in PROJECT_LIST_RE.finditer(text or ""):
        for raw in re.split(r"[、，,]", match.group(1)):
            value = raw.strip("。 　；;")
            if value.startswith(("（", "(")) or value.endswith(("（", "(")):
                continue
            if len(value) > 20 or value in seen or not is_project_name(value):
                continue
            seen.add(value)
            names.append(value)
            if len(names) >= 50:
                return names
    return names


def _summary(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean[:200]
