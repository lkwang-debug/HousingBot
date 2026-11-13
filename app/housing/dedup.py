from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

POST_ID_RE = re.compile(r"(?:m?post|content)[_-]?(\d{5,12})", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")
TITLE_NOISE_RE = re.compile(r"[（(]\s*住保[租售]〔\d{4}〕\d+号\s*[）)]")
PUNCT_RE = re.compile(r"[，。；：、,.!?！？\-—_（）()【】\[\]“”\"'‘’·]")
TRAILING_DATE_RE = re.compile(r"\s*20\d{2}-\d{1,2}-\d{1,2}\s*$")


def clean_title(title: str) -> str:
    return TRAILING_DATE_RE.sub("", title or "").strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in {"http", "https"} else parts.scheme
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path)
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def post_id_of(url: str) -> str | None:
    match = POST_ID_RE.search(url)
    return match.group(1) if match else None


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def canonical_key(url: str) -> str:
    normalized = normalize_url(url)
    host = urlsplit(normalized).netloc
    post_id = post_id_of(normalized)
    if post_id:
        return f"{host}:post:{post_id}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def normalize_title(title: str) -> str:
    text = WHITESPACE_RE.sub("", title or "")
    text = TITLE_NOISE_RE.sub("", text)
    return text.strip()


def content_hash(title: str, published: date | None, body: str) -> str:
    payload = f"{normalize_title(title)}|{published.isoformat() if published else ''}|{(body or '')[:3000]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_key(doc_number: str | None, title: str, published: date | None) -> str:
    left = (doc_number or "").strip() or normalize_title(title)
    payload = f"{left}|{published.isoformat() if published else ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def push_identity(notice) -> tuple:
    """推送去重用的身份。

    同一份通告会从两个栏目各抓到一个 URL，落成两行：canonical_key、merge_key
    都不同（发布时间的时分秒不同），但标题和认租截止时间完全一样。
    """
    return (notice.title_norm or clean_title(notice.title) or notice.canonical_key,
            notice.application_end)


def unique_notices(notices):
    """同一份通告只留一行，避免一条消息里出现两遍。"""
    seen: set[tuple] = set()
    result = []
    for notice in notices:
        key = push_identity(notice)
        if key in seen:
            continue
        seen.add(key)
        result.append(notice)
    return result


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def same_clock(first: datetime | None, second: datetime | None, days: int = 7) -> bool:
    if first is None or second is None:
        return True
    return abs((first - second).total_seconds()) <= days * 86400
