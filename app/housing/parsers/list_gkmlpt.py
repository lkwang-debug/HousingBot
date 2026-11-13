from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from app.config import get_settings
from app.housing.parsers.list_nfcms import ListItem

SID_RE = re.compile(r"SID\s*:\s*['\"](\d+)['\"]")
TREE_RE = re.compile(r"TREE\s*:\s*")
DEFAULT_CLASSIFY_KEYWORDS = ("通知公告", "住房保障", "分配", "退出", "配租", "工作动态")


def parse_gkmlpt_index(html: str) -> tuple[str | None, list[dict]]:
    sid_match = SID_RE.search(html)
    sid = sid_match.group(1) if sid_match else None
    tree_match = TREE_RE.search(html)
    if not tree_match:
        return sid, []
    try:
        tree, _ = json.JSONDecoder().raw_decode(html[tree_match.end() :])
    except ValueError:
        return sid, []
    return sid, tree if isinstance(tree, list) else []


def select_classify_ids(tree: list[dict], keywords: tuple[str, ...] = DEFAULT_CLASSIFY_KEYWORDS) -> list[int]:
    """在信息公开目录树里挑出与住房保障相关的叶子节点。"""
    found: list[int] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            children = node.get("children") or []
            name = str(node.get("name") or "")
            if children:
                walk(children)
                continue
            if any(keyword in name for keyword in keywords):
                try:
                    found.append(int(node["id"]))
                except (KeyError, TypeError, ValueError):
                    continue

    walk(tree)
    return found


def parse_gkmlpt_payload(payload: dict, base_url: str) -> list[ListItem]:
    tz = get_settings().tz
    items: list[ListItem] = []
    for article in payload.get("articles") or []:
        title = html_lib.unescape(str(article.get("title") or "")).strip()
        url = str(article.get("url") or "").strip()
        if not title or not url:
            continue
        # display_publish_time 常为 0，实际日期在 date（epoch 秒）里。
        epoch = next(
            (
                article.get(key)
                for key in ("display_publish_time", "publish_time", "first_publish_time", "date")
                if article.get(key)
            ),
            None,
        )
        published: datetime | None = None
        seconds = _epoch_seconds(epoch)
        if seconds:
            published = datetime.fromtimestamp(seconds, tz=tz).replace(tzinfo=None)
        items.append(
            ListItem(
                title=title,
                url=urljoin(base_url, url),
                published_at=published,
                doc_number=article.get("document_number") or None,
                attachments=_attachments(article.get("attachment"), base_url),
            )
        )
    return items


def _epoch_seconds(value: Any) -> float | None:
    """兼容秒/毫秒以及字符串形式的 epoch。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds > 1e11:  # 毫秒
        seconds /= 1000
    return seconds


def _attachments(raw: Any, base_url: str) -> list[dict]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append({"name": entry.rsplit("/", 1)[-1], "url": urljoin(base_url, entry), "kind": _kind(entry, entry.rsplit("/", 1)[-1])})
        elif isinstance(entry, dict):
            url = str(entry.get("url") or entry.get("path") or "")
            if not url:
                continue
            name = str(entry.get("name") or entry.get("title") or url.rsplit("/", 1)[-1])
            result.append({"name": name, "url": urljoin(base_url, url), "kind": _kind(url, name)})
    return result


def _kind(url: str, name: str = "") -> str:
    # 附件的 URL 经常没有真实扩展名；标题里的文件名更可信，先看它。
    for text in (name, url):
        lowered = text.lower().split("?")[0]
        for suffix in (".pdf", ".xlsx", ".xls", ".et", ".docx", ".doc", ".zip", ".rar"):
            if lowered.endswith(suffix):
                return suffix.lstrip(".")
    return "file"
