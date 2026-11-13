"""Read-only exports of submitted, frozen reports for manual group sharing."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from markdown_it import MarkdownIt

from app.config import get_settings

LABELS = {"digest": "早报", "evening": "晚报"}
WIDTH, HEIGHT = 1080, 1920
TEXT_LIMIT = 900
SOURCE_LABEL = re.compile(r"(?:官方资料\d+|名单\d+|房源来源|查看排位与分析|官方原文|配租原文|核查原文|初审公示原文|选房原文|摇号原文|公告原文|签约原文|资格原文|原文链接|官方链接|点击查看(?:原文)?|已登记发布的查询入口|查询入口|入口|原文|来源|再次查看)")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_units(text: str) -> int:
    """Conservative character budget: emoji count as two UTF-16 units."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class FrozenReport:
    key: str
    kind: str
    generated_at: str
    parts: tuple[str, ...]
    record_sha256: str

    @property
    def markdown(self):
        return "\n\n".join(self.parts)

    @property
    def stamp(self):
        value = datetime.fromisoformat(self.generated_at)
        return value.astimezone(get_settings().tz).replace(tzinfo=None) if value.tzinfo else value


def select_frozen_report(connection, kind="digest", day=None) -> FrozenReport:
    if kind not in LABELS:
        raise ValueError("分享包类型必须是 digest 或 evening")
    if day:
        datetime.strptime(day, "%Y-%m-%d")
    prefix = f"report:{kind}:"
    rows = connection.execute("select key,value from housing_meta where key like ? order by key desc", (prefix + "%",))
    for key, raw in rows:
        report_day = key.removeprefix(prefix)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_day) or (day and day != report_day):
            continue
        payload = json.loads(raw)
        parts = payload.get("parts", [])
        if not parts or any(not isinstance(p.get("content"), str) or not p["content"].strip() for p in parts):
            continue
        # All parts must have been accepted for at least one common group.
        accepted_groups = None
        for index, part in enumerate(parts):
            event = f"{kind}:{report_day}" + (f":part:{index + 1}" if index else "")
            groups = {r[0] for r in connection.execute(
                "select o.group_id from housing_outbox o join housing_outbox_events e on e.outbox_id=o.id "
                "where e.kind=? and e.event_key=? and o.kind=? and o.status in ('submitted','confirmed') and o.content=?",
                (kind, event, kind, part["content"]))}
            accepted_groups = groups if accepted_groups is None else accepted_groups & groups
        if not accepted_groups:
            continue
        generated = payload["generated_at"]
        datetime.fromisoformat(generated)
        return FrozenReport(key, kind, generated, tuple(p["content"] for p in parts), sha256(raw.encode("utf-8")))
    raise ValueError(f"没有找到{day or '最近'}已完整受理的正式{LABELS[kind]}冻结正文；不会重新采集或生成替代报表")


def read_frozen_report(kind="digest", day=None, database=None):
    if database is None:
        value = get_settings().database_url
        if not value.startswith("sqlite:///"):
            raise ValueError("只读分享导出需要SQLite数据库")
        database = Path(value.removeprefix("sqlite:///"))
        if not database.is_absolute():
            database = get_settings().project_root / database
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError("数据库不存在，未创建新库")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        return select_frozen_report(connection, kind, day)


@dataclass
class SourceLink:
    number: str
    url: str
    labels: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TextBlock:
    id: int
    kind: str
    text: str
    table: tuple = ()


@dataclass
class Document:
    blocks: list[TextBlock]
    groups: list[dict]
    links: list[SourceLink]

    @property
    def text(self):
        return "\n\n".join(b.text for b in self.blocks)


def parse_document(markdown: str, *, include_sources=True) -> Document:
    links, by_url, blocks, groups = [], {}, [], []
    context, section, current_batch, list_stack = "全市概览", "全市概览", None, []

    def source(url, label):
        if not url.startswith(("https://", "http://")):
            raise ValueError("正文包含不能安全导出的链接，请先核对来源")
        if url not in by_url:
            value = SourceLink(f"S{len(links) + 1:02d}", url)
            by_url[url] = value; links.append(value)
        value = by_url[url]
        if label and label not in value.labels: value.labels.append(label)
        if context and context not in value.contexts: value.contexts.append(context)
        return value.number

    def inline(tokens):
        out, opened = [], []
        for token in tokens:
            if token.type == "link_open":
                opened.append((token.attrGet("href") or "", len(out)))
            elif token.type == "link_close":
                url, start = opened.pop()
                label = "".join(out[start:])
                if include_sources:
                    number = source(url, label)
                    out.append(f"〔{number}〕")
                elif SOURCE_LABEL.fullmatch(label.strip()):
                    del out[start:]
            elif token.type in {"text", "code_inline"}:
                value = token.content
                if not opened:
                    def replace_url(match):
                        raw = match.group(0)
                        url = raw.rstrip("。，；、）)]")
                        return f"来源〔{source(url, '原文链接')}〕" + raw[len(url):]
                    if include_sources:
                        value = re.sub(r"https?://[^\s<>]+", replace_url, value)
                    else:
                        value = re.sub(r"(?:https?://|www\.)[^\s<>\u3000-\u303f\uff00-\uffef]+", "", value)
                out.append(value)
            elif token.type in {"softbreak", "hardbreak"}: out.append("\n")
            elif token.type in {"strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close"}: pass
            else:
                raise ValueError(f"正文包含尚未支持的格式 {token.type}，拒绝遗漏内容")
        if opened: raise ValueError("链接结构不完整")
        value = "".join(out)
        if not include_sources:
            value = re.sub(r"(?:https?://|www\.)[^\s<>\u3000-\u303f\uff00-\uffef]+", "", value, flags=re.I)
            value = re.sub(r"〔S\d+〕|\[S\d+\]", "", value)
            cleaned = []
            for line in value.splitlines():
                if re.fullmatch(r'\s*(?:房源来源|来源)\s*[｜|：:]\s*[\d\s｜|、，,]+',line):continue
                parts = re.split(r"([｜|；;])", line)
                for i in range(0, len(parts), 2):
                    stripped = parts[i].strip()
                    if SOURCE_LABEL.fullmatch(stripped.rstrip("：:")):
                        parts[i] = ""
                    else:
                        match = SOURCE_LABEL.search(stripped)
                        if match and not re.search(r"[\w\u4e00-\u9fff]", stripped[:match.start()]) and stripped[match.end():].startswith(("（", "(")):
                            parts[i] = stripped[:match.start()] + stripped[match.end():]
                cleaned.append("".join(parts))
            value = "\n".join(cleaned)
            value = re.sub(r"[｜|]\s*[；;]", "；", value)
            value = "\n".join(re.sub(r"[｜|；;·\s]+$", "", re.sub(r"^[｜|；;·\s]+", "", line)) for line in value.splitlines()).strip()
            value = re.sub(r"(?:[｜|]\s*){2,}", "｜", value)
            if not re.search(r"[\w\u4e00-\u9fff]", value):
                value = ""
        value=re.sub(r'(?:（点击展开）|（展开）|\(点击展开\)|\(展开\))\s*$','',value).rstrip()
        value=re.sub(r'(?:（点击展开）|（展开）|\(点击展开\)|\(展开\))\s*$','',value).rstrip()
        return value

    # Browser clipboard tables use TABs; exported Markdown uses pipes. Parse
    # both before CommonMark, which otherwise collapses them into paragraphs.
    table_markers = {}
    table_counter = 0
    lines = markdown.splitlines()
    replaced=[]; i=0
    marker_prefix='HOUSING_TABLE_'+sha256(markdown.encode())[:16]+'_'
    def cells(line,mode='pipe'):
        line=line.strip(' \r\n')
        if mode=='tab':return [v.strip() for v in line.split('\t')]
        if line.startswith('|'):line=line[1:]
        if line.endswith('|') and not line.endswith(r'\|'):line=line[:-1]
        return [v.strip() for v in re.split(r'(?<!\\)\|',line)]
    def is_separator(line):
        values=cells(line)
        return len(values)>=2 and all(re.fullmatch(r':?-+:?',v) for v in values)
    headers={'批次','批次及对象','当前状态','最近节点','认租截止','房源','认租档位','档位','初始套数','申请户数','户／套','户/套',
             '实际户型','户型','建筑面积','面积','套数','费用项目','标准','单位','项目','地址','执行租金','市场参考','物业','专项维修','停车'}
    def tab_header(line):
        values=cells(line,'tab')
        return '\t' in line and len(values)>=2 and all(v in headers or re.fullmatch(r'第[一二三四五六七八九十\d]+队列|优先配租队列|其他配租队列',v) for v in values)
    def next_nonempty(position):
        while position<len(lines) and not lines[position].strip():position+=1
        return position
    fenced=False
    while i<len(lines):
        if lines[i].lstrip().startswith(('```','~~~')):
            fenced=not fenced;replaced.append(lines[i]);i+=1;continue
        separator_at=next_nonempty(i+1)
        pipe_table=not fenced and '|' in lines[i] and separator_at<len(lines) and is_separator(lines[separator_at])
        tab_table=not fenced and tab_header(lines[i])
        if pipe_table or tab_table:
            mode='pipe' if pipe_table else 'tab'
            header=cells(lines[i],mode);rows=[];i=separator_at+1 if pipe_table else i+1
            while i<len(lines):
                position=next_nonempty(i)
                if position>=len(lines):i=position;break
                line=lines[position]
                if mode=='tab':
                    if '\t' not in line or tab_header(line):i=position;break
                else:
                    following=next_nonempty(position+1)
                    if '|' not in line or (following<len(lines) and is_separator(lines[following])):i=position;break
                row=cells(line,mode)
                if len(row)!=len(header):
                    raise ValueError(f'表格第{position+1}行列数不一致，请保留制表符或导入Markdown原文；未截断数据')
                rows.append(row);i=position+1
            marker=marker_prefix+str(table_counter)
            table_counter+=1;table_markers[marker]={'headers':header,'rows':rows}
            replaced.extend(['',marker,'']);continue
        replaced.append(lines[i]);i+=1
    markdown='\n'.join(replaced)
    tokens = MarkdownIt("commonmark", {"html": False}).parse(markdown)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type in {"heading_open", "paragraph_open"}:
            child = tokens[index + 1]
            if child.type != "inline": raise ValueError("无法完整解析正文段落")
            kind = token.tag if token.type == "heading_open" else "p"
            batch = re.match(r"\*{0,2}【[^】]*?([BN]\d+)[^】]*】", child.content)
            if kind.startswith("h"):
                context = child.content; section = child.content; current_batch = None
            elif batch:
                context = batch.group(1); current_batch = None
            value = inline(child.children or [])
            if value.strip() in table_markers:
                raw=table_markers[value.strip()]
                def cell_text(value):return inline(MarkdownIt('commonmark',{'html':False}).parseInline(value)[0].children or [])
                table={'headers':[cell_text(v) for v in raw['headers']], 'rows':[[cell_text(v) for v in row] for row in raw['rows']]}
                flat='\n'.join([' | '.join(table['headers']),*[' | '.join(row) for row in table['rows']]])
                block=TextBlock(len(blocks),'table',flat,((table['headers'],tuple(table['rows'])),))
                blocks.append(block)
                groups.append({'kind':'table','label':'','section':section,'blocks':[asdict(block)]})
                current_batch=None
                index += 3;continue
            if not value:
                index += 3; continue
            if list_stack:
                value = (f"{list_stack[-1]['next']}. " if list_stack[-1]["ordered"] else "• ") + value
            block = TextBlock(len(blocks), kind, value); blocks.append(block)
            if kind.startswith("h"):
                groups.append({"kind": "heading", "label": value, "section": section, "blocks": [asdict(block)]})
            elif batch:
                current_batch = {"kind": "batch", "label": batch.group(1), "section": section, "blocks": [asdict(block)]}
                groups.append(current_batch)
            elif current_batch is not None:
                current_batch["blocks"].append(asdict(block))
            else:
                groups.append({"kind": "text", "label": "", "section": section, "blocks": [asdict(block)]})
            index += 3; continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append({"ordered": token.type == "ordered_list_open", "next": int(token.attrGet("start") or 1) - 1})
        elif token.type == "list_item_open":
            if list_stack: list_stack[-1]["next"] += 1
        elif token.type in {"bullet_list_close", "ordered_list_close"}: list_stack.pop()
        elif token.type in {"fence", "code_block", "hr"}:
            value = token.content if token.type != "hr" else "────────"
            block = TextBlock(len(blocks), "code" if token.type != "hr" else "rule", value); blocks.append(block)
            groups.append({"kind": "text", "label": "", "section": section, "blocks": [asdict(block)]}); current_batch = None
        elif token.type in {"list_item_close", "blockquote_open", "blockquote_close"}: pass
        else: raise ValueError(f"无法完整解析正文格式 {token.type}")
        index += 1
    if not blocks: raise ValueError("正文为空")
    return Document(blocks, groups, links)


def split_entries(entries, title, limit=TEXT_LIMIT):
    """Never split a source entry or URL; count the final numbered header too."""
    budget = limit - 60
    if budget < 100: raise ValueError("文字预算太小")
    chunks, current = [], []
    for entry in entries:
        if text_units(entry) > budget:
            raise ValueError("单个来源条目超出文字预算，未截断网址；请另行核对该条目")
        candidate = "\n\n".join([*current, entry])
        if current and text_units(candidate) > budget:
            chunks.append("\n\n".join(current)); current = []
        current.append(entry)
    if current: chunks.append("\n\n".join(current))
    result = [f"【{title} {i}/{len(chunks)}】\n\n{body}" for i, body in enumerate(chunks, 1)]
    if any(text_units(value) > limit for value in result): raise ValueError("带编号的文字超出预算")
    return result


def split_body(text, title, limit=TEXT_LIMIT):
    budget = limit - 60
    if budget < 100: raise ValueError("文字预算太小")
    chunks, start = [], 0
    while start < len(text):
        end, used = start, 0
        while end < len(text) and used + text_units(text[end]) <= budget:
            used += text_units(text[end]); end += 1
        if end < len(text):
            boundary = text.rfind("\n\n", start, end)
            if boundary > start + (end - start) // 2: end = boundary + 2
        chunks.append(text[start:end]); start = end
    assert "".join(chunks) == text
    return [f"【{title} {i}/{len(chunks)}】\n\n{value}" for i, value in enumerate(chunks, 1)]


class ChromeRenderer:
    """One isolated headless Chrome, fixed viewport, no logged-in user profile."""
    def __init__(self, chrome=None, *, target_pages=None, max_height=4320, progress=None):
        self.chrome = Path(chrome or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.target_pages = target_pages
        self.max_height = max_height
        self.progress = progress or (lambda percent, message: None)

    def render(self, report, document, directory):
        import websocket
        template = get_settings().project_root / "marketing" / "share_pack_template.html"
        if not self.chrome.is_file(): raise ValueError("没有找到Chrome，无法生成完整图片包")
        payload = {"groups": document.groups, "generated_at": report.stamp.isoformat(),
                   "label": LABELS[report.kind], "width": WIDTH, "height": HEIGHT,
                   "show_clock": getattr(report, "show_clock", True)}
        page = directory / "排版预览.html"
        source = template.read_text(encoding="utf-8")
        def write_page(height):
            payload["height"] = height
            encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            page.write_text(source.replace("__REPORT_DATA__", encoded), encoding="utf-8")
        write_page(HEIGHT)
        self.progress(15, "正在启动图片渲染器……")
        with tempfile.TemporaryDirectory(prefix="housing-share-chrome-", ignore_cleanup_errors=True) as temp:
            profile = Path(temp)
            process = subprocess.Popen([str(self.chrome), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0",
                f"--user-data-dir={profile}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ws = None
            try:
                deadline = time.monotonic() + 20
                port_file = profile / "DevToolsActivePort"
                while not port_file.exists():
                    if process.poll() is not None or time.monotonic() > deadline: raise RuntimeError("Chrome启动失败")
                    time.sleep(.05)
                port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                with httpx.Client(trust_env=False, timeout=10) as client:
                    tabs = client.get(f"http://127.0.0.1:{port}/json/list").json()
                target = next(t for t in tabs if t.get("type") == "page")
                ws = websocket.create_connection(target["webSocketDebuggerUrl"], suppress_origin=True, timeout=20)
                seq = 0
                def call(method, params=None):
                    nonlocal seq
                    seq += 1; request_id = seq
                    ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                    while True:
                        answer = json.loads(ws.recv())
                        if answer.get("id") != request_id: continue
                        if "error" in answer: raise RuntimeError(f"Chrome渲染失败：{answer['error']['message']}")
                        return answer.get("result", {})
                def evaluate(expression, *, await_promise=False):
                    result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": await_promise})
                    if result.get("exceptionDetails"): raise RuntimeError("图片排版脚本执行失败")
                    return result.get("result", {}).get("value")
                call("Page.enable")
                height = HEIGHT
                while True:
                    self.progress(25, f"正在计算分页，图片高度{height}像素……")
                    write_page(height)
                    call("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": height, "deviceScaleFactor": 1, "mobile": False})
                    call("Runtime.evaluate", {"expression": "window.paginationResult=null"})
                    call("Page.navigate", {"url": page.as_uri() + f"?height={height}"})
                    deadline = time.monotonic() + 30
                    manifest = None
                    while manifest is None:
                        manifest = evaluate("window.paginationResult || null")
                        if time.monotonic() > deadline: raise RuntimeError("图片分页超时")
                        if manifest is None: time.sleep(.05)
                    if manifest.get("error"): raise RuntimeError("图片分页失败：" + manifest["error"])
                    validate_pagination(document, manifest)
                    if not self.target_pages or len(manifest["pages"]) <= self.target_pages or height >= self.max_height:
                        break
                    # Increase canvas height, not density / font size; remeasure real browser layout.
                    estimate = 320 + (height - 320) * len(manifest["pages"]) / self.target_pages
                    height = min(self.max_height, max(height + 240, int(estimate / 120 + 1) * 120))
                manifest["image_height"] = height
                manifest["image_width"] = WIDTH
                pictures = directory / "图片"; pictures.mkdir()
                for index in range(len(manifest["pages"])):
                    self.progress(35 + int(53 * index / len(manifest["pages"])), f"正在生成第{index + 1}/{len(manifest['pages'])}张图片……")
                    fits = evaluate(f"(async()=>{{window.showPage({index});await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return window.validateVisiblePage();}})()", await_promise=True)
                    if not fits: raise RuntimeError(f"第{index + 1}页存在裁切，未输出成品")
                    data = call("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False})
                    path = pictures / f"{index + 1:02d}.png"
                    path.write_bytes(base64.b64decode(data["data"]))
                    if png_size(path) != (WIDTH, height): raise RuntimeError("图片尺寸不正确")
                return manifest
            finally:
                if ws:
                    try: ws.send(json.dumps({"id": 999999, "method": "Browser.close"}))
                    except Exception: pass
                    try: ws.close()
                    except Exception: pass
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def png_size(path):
    raw = Path(path).read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24: raise ValueError("图片文件损坏")
    return struct.unpack(">II", raw[16:24])


def validate_pagination(document, manifest):
    if not manifest.get("pages"): raise ValueError("没有生成图片页")
    collected = {b.id: [] for b in document.blocks}
    table_parts={b.id:[] for b in document.blocks if b.kind=='table'}
    actual_order = []
    for page in manifest["pages"]:
        if not page.get("fits") or not page.get("fragments"): raise ValueError("图片存在溢出或空页")
        for fragment in page["fragments"]:
            block_id = fragment["block_id"]
            if block_id not in collected: raise ValueError("图片包含未知段落")
            collected[block_id].append(fragment["text"])
            if block_id in table_parts:table_parts[block_id].append(fragment)
            if not actual_order or actual_order[-1] != block_id: actual_order.append(block_id)
    if actual_order != [b.id for b in document.blocks]: raise ValueError("图片段落丢失、重复或乱序")
    for block in document.blocks:
        if block.kind=='table':
            headers,rows=block.table[0];offset=0
            for fragment in table_parts[block.id]:
                start,end=fragment['start'],fragment['end']
                if start!=offset or end<start or end>len(rows):raise ValueError('表格分页行范围丢失或重复')
                if fragment.get('headers')!=list(headers) or fragment.get('rows')!=[list(r) for r in rows[start:end]]:
                    raise ValueError('表格单元格数据不完整，未输出成品')
                offset=end
            if offset!=len(rows):raise ValueError('表格存在未生成的数据行')
            continue
        if "".join(collected[block.id]) != block.text: raise ValueError(f"第{block.id}段文字不完整")


def export_share_pack(report, output=None, *, renderer=None):
    root = Path(output or get_settings().project_root / "marketing/out/xhs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    document = parse_document(report.markdown)
    content_hash = sha256(report.markdown.encode("utf-8"))
    template = get_settings().project_root / "marketing/share_pack_template.html"
    exporter_hash = sha256(Path(__file__).read_bytes() + template.read_bytes())
    stamp, label = report.stamp, LABELS[report.kind]
    name = f"{label}-{stamp:%Y%m%d-%H%M}-{content_hash[:6]}-{exporter_hash[:6]}"
    target = root / name
    if target.exists():
        manifest_file = target / "核对清单.json"
        if manifest_file.is_file():
            existing = json.loads(manifest_file.read_text(encoding="utf-8"))
            if existing.get("source_sha256") == content_hash and existing.get("exporter_sha256") == exporter_hash and all(
                (target / f["path"]).is_file() and sha256((target / f["path"]).read_bytes()) == f["sha256"]
                for f in existing.get("files", [])
            ) and existing.get("files"):
                validate_pagination(document, {"pages": existing["page_coverage"]})
                return {"status": "existing", "directory": str(target), "archive": str(root / (name + ".zip")),
                        "pages": existing["pages"], "source_key": report.key}
        raise ValueError("同名分享包不完整或已被修改，未覆盖；请使用另一个输出目录")
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=root)).resolve()
    try:
        (staging / "原始正文.md").write_text(report.markdown, encoding="utf-8")
        layout = (renderer or ChromeRenderer()).render(report, document, staging)
        validate_pagination(document, layout)
        pages = len(layout["pages"])
        images = sorted((staging / "图片").glob("*.png"))
        if [p.name for p in images] != [f"{i:02d}.png" for i in range(1, pages + 1)]:
            raise ValueError("图片页数或页码不完整")
        if any(png_size(p) != (WIDTH, HEIGHT) for p in images):
            raise ValueError("图片尺寸不一致")
        intro = (f"深圳住房管家｜{stamp:%Y-%m-%d}{label}\n"
            f"信息截至{stamp:%Y-%m-%d %H:%M}（北京时间）。\n"
            f"以下为对应已发报表的完整内容，共{pages}张图片，按页码查看。\n"
            f"文中“今天／明天”以{stamp:%m月%d日}为准，不随转发时间变化。办理前请核对官方最新通知。\n"
            "原文链接见后附编号清单。公开信息整理，非官方账号。")
        if text_units(intro) > TEXT_LIMIT: raise ValueError("导读文字超出预算")
        (staging / "00-先发这条导读.txt").write_text(intro, encoding="utf-8")
        entries = [f"〔{link.number}〕{'／'.join(link.contexts)} · {'／'.join(link.labels)}\n{link.url}" for link in document.links]
        messages = split_entries(entries, f"{stamp:%Y-%m-%d}{label}原文链接")
        links_dir = staging / "原文链接"; links_dir.mkdir()
        for index, message in enumerate(messages, 1): (links_dir / f"{index:02d}.txt").write_text(message, encoding="utf-8")
        backup_dir = staging / "纯文字备用"; backup_dir.mkdir()
        for index, message in enumerate(split_body(document.text, f"{stamp:%Y-%m-%d}{label}正文"), 1):
            if text_units(message) > TEXT_LIMIT: raise ValueError("备用正文超出文字预算")
            (backup_dir / f"{index:02d}.txt").write_text(message, encoding="utf-8")
        (staging / "正文全文.txt").write_text(document.text + "\n\n原文链接\n\n" + "\n\n".join(entries), encoding="utf-8")
        guide = (f"{stamp:%Y-%m-%d %H:%M}{label} · 小红书群分享包\n\n"
            f"1. 复制发送 00-先发这条导读.txt。\n2. 按顺序发送 图片/01.png 至 图片/{pages:02d}.png。\n"
            f"3. 依次复制发送 原文链接/ 内的{len(messages)}条文字，图片中的S编号与之对应。\n\n"
            "每条可发送文字（含序号）均按不超过900字符校验，给1000字限制留余量；表情保守按2字符计。\n"
            "纯文字备用目录用于不发图片时替代正文，不必与图片重复发送。\n"
            "原始正文.md、正文全文.txt、核对清单.json和排版预览.html留在本地核对即可。\n"
            "ZIP用于传到手机后解压，不需要把ZIP发到群里。\n")
        (staging / "发送顺序.txt").write_text(guide, encoding="utf-8")
        manifest = {"schema": 1, "source_key": report.key, "source_generated_at": report.generated_at,
            "source_record_sha256": report.record_sha256, "source_sha256": content_hash, "exporter_sha256": exporter_hash,
            "batch_codes": list(dict.fromkeys(re.findall(r"【[^】]*?([BN]\d+)[^】]*】", document.text))),
            "source_parts": len(report.parts), "pages": pages, "width": WIDTH, "height": HEIGHT,
            "sources": [asdict(v) for v in document.links], "page_coverage": layout["pages"],
            "source_blocks": [asdict(v) for v in document.blocks], "text_limit_units": TEXT_LIMIT,
            "text_messages": [{"path": str(p.relative_to(staging)).replace("\\", "/"), "units": text_units(p.read_text(encoding="utf-8"))}
                for p in [staging / "00-先发这条导读.txt", *sorted(links_dir.glob("*.txt")), *sorted(backup_dir.glob("*.txt"))]],
            "files": [{"path": p.relative_to(staging).as_posix(), "sha256": sha256(p.read_bytes())}
                for p in sorted(staging.rglob("*")) if p.is_file()]}
        (staging / "核对清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.rename(target)
        archive = root / (name + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(target.rglob("*")):
                if path.is_file(): zipped.write(path, arcname=name + "/" + path.relative_to(target).as_posix())
        return {"status": "created", "directory": str(target), "archive": str(archive), "pages": pages,
                "link_messages": len(messages), "source_key": report.key}
    finally:
        if staging.exists():
            if not staging.is_relative_to(root) or not staging.name.startswith(".building-"):
                raise RuntimeError("临时目录越界，拒绝清理")
            shutil.rmtree(staging)
