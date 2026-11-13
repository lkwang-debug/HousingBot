from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.housing.dedup import clean_title
from app.housing.parsers.dates import parse_date_text

LIST_SELECTORS = (
    "div.listcontent_right li",
    "ul.ftdt-list li",
    "div.list li",
    "div.news-list li",
    "ul.list li",
    "div.content li",
)


@dataclass
class ListItem:
    title: str
    url: str
    published_at: datetime | None = None
    doc_number: str | None = None
    attachments: list = field(default_factory=list)


def page_url_for(entry_url: str, page: int) -> str:
    """page 从 0 开始；0 返回入口本身，其余按 NFCMS 的 index_N.html 规则。"""
    if page <= 0:
        return entry_url
    if entry_url.endswith("index.html"):
        return entry_url[: -len("index.html")] + f"index_{page + 1}.html"
    if entry_url.endswith("/"):
        return entry_url + f"index_{page + 1}.html"
    head, _, last = entry_url.rpartition("/")
    if "." in last:
        return f"{head}/index_{page + 1}.html"
    return entry_url + f"/index_{page + 1}.html"


def parse_nfcms_list(html: str, base_url: str, limit: int | None = None) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, ListItem] = {}

    nodes = []
    for selector in LIST_SELECTORS:
        nodes = soup.select(selector)
        if nodes:
            break
    if not nodes:
        nodes = [li for li in soup.find_all("li") if li.find("a", href=True)]

    for node in nodes:
        anchor = node.find("a", href=True)
        if anchor is None:
            continue
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        if not _looks_like_article(href):
            continue
        title = clean_title((anchor.get("title") or anchor.get_text(" ", strip=True) or "").strip())
        if not title or title in JUNK_TITLES or len(title) < 6:
            continue
        published = _date_outside_title(node, anchor)
        item = ListItem(
            title=title,
            url=urljoin(base_url, href),
            published_at=datetime.combine(published, datetime.min.time()) if published else None,
        )
        key = item.url
        if key not in seen:
            seen[key] = item
        if limit and len(seen) >= limit:
            break

    if not seen:
        # 部分站点（如大鹏）列表项不是 li 结构，直接扫描文章锚点。
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not _looks_like_article(href):
                continue
            title = clean_title(
                (anchor.get("title") or anchor.get_text(" ", strip=True) or "").strip()
            )
            if not title or title in JUNK_TITLES or len(title) < 6:
                continue
            published = _date_outside_title(anchor.parent, anchor)
            seen.setdefault(
                urljoin(base_url, href),
                ListItem(
                    title=title,
                    url=urljoin(base_url, href),
                    published_at=datetime.combine(published, datetime.min.time())
                    if published
                    else None,
                ),
            )
            if limit and len(seen) >= limit:
                break
    return list(seen.values())


def _date_outside_title(node, anchor):
    if node is None:
        return None
    for selector in ("time", ".date", ".time", ".pubdate", ".publish-time", "span"):
        for element in node.select(selector):
            if element is anchor or anchor in element.parents or element in anchor.parents:
                continue
            found = parse_date_text(element.get("datetime", "") or element.get_text(" ", strip=True))
            if found:
                return found
    # Text siblings are safe; the anchor/title itself is explicitly excluded.
    text = " ".join(str(value) for value in node.find_all(string=True)
                    if anchor not in value.parents)
    return parse_date_text(text)


def _context_text(anchor) -> str:
    node = anchor
    for _ in range(4):
        node = node.parent
        if node is None:
            return ""
        text = node.get_text(" ", strip=True)
        if len(text) < 300:
            return text
    return ""


JUNK_TITLES = {"关于我们", "联系我们", "网站地图", "网站声明", "隐私声明", "首页"}


def _looks_like_article(href: str) -> bool:
    lowered = href.lower()
    if "/content/" in lowered:
        return True
    return "/post_" in lowered or "mpost_" in lowered


def article_host(url: str) -> str:
    return urlsplit(url).netloc
