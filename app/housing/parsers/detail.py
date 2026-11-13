from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import get_settings
from app.housing.parsers.dates import parse_date_text

CONTENT_SELECTORS = (
    "div.article_content#main",
    "div#main",
    "div.article_content",
    "div.article-content",
    "div.article_article",
    "div.article",
    "div.content",
    "div.TRS_Editor",
    "div#zoom",
    "article",
)
TITLE_SELECTORS = (
    "meta[name='ArticleTitle']",
    "meta[property='og:title']",
    "h1",
    "h2.article-title",
    "div.article-title",
)
PUBLISHED_RE = re.compile(
    r"(?:发布时间|发布日期|成文日期|时间)\s*[：:]\s*"
    r"(20\d{2}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}\s*日?)"
)
# .et 是 WPS 表格，实际是 BIFF8（和 .xls 同一种容器），xlrd 能直接读。
ATTACHMENT_SUFFIXES = (".pdf", ".xls", ".xlsx", ".et", ".doc", ".docx", ".wps", ".zip", ".rar")
MAX_BODY_CHARS = 50_000


@dataclass
class DetailResult:
    title: str | None = None
    published_at: datetime | None = None
    body_text: str = ""
    content_html: str = ""
    attachments: list = field(default_factory=list)
    department: str | None = None


def parse_detail(html: str, page_url: str) -> DetailResult:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()

    title = _meta_or_text(soup, TITLE_SELECTORS)
    node = _pick_content_node(soup)
    content_html = str(node) if node is not None else str(soup.body or soup)
    body_text = _node_text(node) if node is not None else _node_text(soup.body or soup)
    if not body_text:
        body_text = _node_text(soup)

    published = _published_from_meta(soup)
    if published is None:
        published = parse_date_text(PUBLISHED_RE.search(body_text).group(1)) if PUBLISHED_RE.search(body_text) else None
    published_at = datetime.combine(published, datetime.min.time()) if published else None

    return DetailResult(
        title=html_lib.unescape(title).strip() if title else None,
        published_at=published_at,
        body_text=body_text[:MAX_BODY_CHARS],
        content_html=content_html,
        attachments=_collect_attachments(soup, node, page_url),
        department=_department(soup),
    )


def _meta_or_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node is None:
            continue
        if node.name == "meta":
            value = node.get("content")
        else:
            value = node.get_text(" ", strip=True)
        if value and value.strip():
            return value.strip()
    return None


def _pick_content_node(soup: BeautifulSoup):
    best = None
    best_len = 0
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            length = len(node.get_text(strip=True))
            if length > best_len:
                best, best_len = node, length
        if best is not None and best_len > 200:
            break
    return best


def _node_text(node) -> str:
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for cell in node.find_all("td"):
        cell.append("\t")
    for row in node.find_all("tr"):
        row.append("\n")
    text = node.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _published_from_meta(soup: BeautifulSoup) -> "datetime.date | None":
    meta = soup.find("meta", attrs={"name": re.compile("pubdate|publishdate|PubDate", re.I)})
    if meta and meta.get("content"):
        found = parse_date_text(meta["content"])
        if found:
            return found
    prop = soup.find("meta", attrs={"property": "article:published_time"})
    if prop and prop.get("content"):
        found = parse_date_text(prop["content"])
        if found:
            return found
    return None


ATTACHMENT_BLOCKS = (".article_pdf", ".article_attachment", ".article_fj", ".fj", "#fj", ".attachment")


def _collect_attachments(soup, node, page_url: str) -> list[dict]:
    """附件经常在正文 div 之外的「相关附件」块里（市级站是 div.article_pdf），
    正文里找不到时再往那几个常见容器、最后整页找一遍。"""
    found = _attachments(node, page_url) if node is not None else []
    if found:
        return found
    for selector in ATTACHMENT_BLOCKS:
        block = soup.select_one(selector)
        if block is not None:
            found = _attachments(block, page_url)
            if found:
                return found
    return _attachments(soup, page_url)


def _attachments(node, page_url: str) -> list[dict]:
    result: dict[str, dict] = {}
    for anchor in node.find_all("a", href=True):
        href = anchor["href"].strip()
        lowered = href.lower().split("?")[0]
        if not lowered.endswith(ATTACHMENT_SUFFIXES):
            continue
        url = urljoin(page_url, href)
        name = anchor.get_text(" ", strip=True) or url.rsplit("/", 1)[-1]
        result[url] = {
            "name": html_lib.unescape(name)[:200],
            "url": url,
            "kind": lowered.rsplit(".", 1)[-1],
        }
    return list(result.values())


def _department(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"name": re.compile("ContentSource|source", re.I)})
    if meta and meta.get("content"):
        value = meta["content"].strip()
        if value:
            return value
    match = re.search(r"(?:来源|发布机构)\s*[：:]\s*([^\n]{2,40})", soup.get_text(" ", strip=True))
    return match.group(1).strip() if match else None


def local_now():
    return datetime.now(get_settings().tz)
