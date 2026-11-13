"""Local, read-only report sharing: one guide and PNGs, with private build records."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

from app.config import get_settings
from app.housing.lock import process_lock
from app.housing.share_pack import (
    ChromeRenderer, Document, TextBlock, LABELS, HEIGHT, WIDTH, parse_document, png_size, sha256,
    text_units, validate_pagination,
)

URL = re.compile(r"(?:https?://|www\.)[^\s<>\u3000-\u303f\uff00-\uffef]+", re.I)
BATCH = re.compile(r"【[^】]*?([BN]\d+)[^】]*】")
MAX_IMAGE_HEIGHT = 4320
LAYOUT_VERSION = "simple-long-report-v4-tab-and-markdown-tables"


def strip_platform_wrapper(text):
    """Remove only a recognizable copied push header before the duplicate body title."""
    lines = text.splitlines()
    titles = []
    for i, line in enumerate(lines[:30]):
        plain = re.sub(r"[*#]", "", line).strip()
        if plain.startswith(("统计：", "统计:")):
            break
        if re.match(r"[🏠🌙\s]*深圳住房管家(?:早报|晚报)", plain):
            titles.append((i, plain))
    if len(titles) >= 2 and titles[0][1] == titles[-1][1]:
        prefix = "\n".join(lines[:titles[-1][0]])
        if "群发" in prefix and re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}", prefix) and not BATCH.search(prefix):
            return "\n".join(lines[titles[-1][0]:])
    return text


def canonical_hash(document):
    # Formatting, blank lines and removed URLs do not create another report version.
    return sha256(re.sub(r"\s+", " ", document.text).strip().encode("utf-8"))


@dataclass(frozen=True)
class ShareReport:
    markdown: str
    kind: str
    day: date
    clock: time | None = None

    @property
    def stamp(self):
        return datetime.combine(self.day, self.clock or time())

    @property
    def show_clock(self):
        return self.clock is not None


def make_report(text, kind, day, clock=""):
    if kind not in LABELS:
        raise ValueError("请选择早报或晚报")
    try:
        parsed_day = date.fromisoformat(day.strip())
    except ValueError as exc:
        raise ValueError("请填写报表日期，格式为 YYYY-MM-DD；不要使用转发日期代替报表日期") from exc
    try:
        parsed_clock = time.fromisoformat(clock.strip()) if clock.strip() else None
        if parsed_clock and parsed_clock.tzinfo:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("报表时间请填写 HH:MM，或留空") from exc
    if not text.strip():
        raise ValueError("请先加载、导入或粘贴完整报表")
    return ShareReport(text.strip(), kind, parsed_day, parsed_clock)


def detect_metadata(text):
    """Use report headings / reporting window, never historical batch dates or today's date."""
    lines = text.splitlines()
    headings = [line for line in lines[:40] if re.search(r"(?:深圳住房管家|验收[·・]|^[#\s🏠🌙]*)(?:早报|晚报)", line)]
    kinds = {kind for line in headings for kind, label in LABELS.items() if label in line}
    result = {"kind": next(iter(kinds)) if len(kinds) == 1 else "", "day": "", "clock": ""}
    # Body starts with statistics; dates after the first batch are not report metadata.
    header = re.split(r"【[^】]*[BN]\d+", "\n".join(lines[:40]), maxsplit=1)[0]
    explicit = re.findall(r"(?<!\d)(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", header)
    report_dates = re.findall(r"(?:早报|晚报)[^\d\n]{0,8}(?:(20\d{2})[-年])?(\d{1,2})[-月](\d{1,2})", "\n".join(headings))
    try:
        full_days = {date(int(y), int(m), int(d)) for y, m, d in explicit}
        title_days = {(int(m), int(d)) for _, m, d in report_dates}
        title_years = {int(y) for y, _, _ in report_dates if y}
        if len(title_days) == 1:
            m, d = next(iter(title_days))
            matches = {v for v in full_days if (v.month, v.day) == (m, d)}
            if len(matches) == 1:
                result["day"] = next(iter(matches)).isoformat()
            elif len(title_years) == 1:
                result["day"] = date(next(iter(title_years)), m, d).isoformat()
        elif len(full_days) == 1:
            result["day"] = next(iter(full_days)).isoformat()
    except ValueError:
        pass
    window = re.search(r"(?:统计|比较区间)[：:].*?([^\n]+)", header)
    clocks = re.findall(r"(?<!\d)(\d{1,2}:\d{2})(?::\d{2})?", window.group(1)) if window else []
    if clocks:
        try:
            h, m = map(int, clocks[-1].split(":"))
            result["clock"] = time(h, m).strftime("%H:%M")
        except ValueError:
            pass
    return result


def read_report_file(path):
    path = Path(path)
    if path.suffix.lower() not in {".txt", ".md", ".markdown"}:
        raise ValueError("请选择 TXT 或 Markdown 文件")
    raw = path.read_bytes()
    for encoding in (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]):
        try:
            value = raw.decode(encoding)
            if "\x00" in value:
                continue
            return value
        except UnicodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 文本后再导入")


def prepare_document(text):
    # Plain text copied from a push has no Markdown headings / blank lines.
    # Add only layout delimiters; keep every substantive character in order.
    lines, fenced = [], False
    for line in strip_platform_wrapper(text).splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and not stripped.startswith("#"):
            if re.match(r"(?:[🏠🌙]\s*)?深圳住房管家(?:早报|晚报)", stripped):
                line = "## " + stripped
            elif re.match(r"(?:📊|📍|[一二三四五六七八九十]+[、，]\s*(?:市级|各区|待核实)|查询与说明$)", stripped):
                line = "### " + stripped
            if BATCH.match(stripped) or line.startswith("#"):
                lines.append("")
        lines.append(line)
        if line.startswith("#"):
            lines.append("")
    doc = parse_document("\n".join(lines), include_sources=False)
    # URLs inside a code block are not silently left behind or dropped wholesale.
    if URL.search(doc.text) or re.search(r"〔S\d+〕|\[S\d+\]", doc.text):
        raise ValueError("正文的代码块或特殊格式中仍有网址／链接编号，请在输入框移除后重试")
    before = BATCH.findall(text)
    if BATCH.findall(doc.text) != before:
        raise ValueError("批次编号校验失败，未输出不完整图片")
    return doc


def make_intro(report, document, pages):
    # Quote a complete statistics line rather than interpreting eligibility or inferring urgency.
    stats = next((line.strip() for b in document.blocks for line in b.text.splitlines()
                  if line.strip().startswith(("全市去重：","已采集范围：")) and len(line.strip()) <= 150), "")
    snapshot = f"{report.day:%Y-%m-%d}" + (f" {report.clock:%H:%M}" if report.clock else "")
    lines = [f"深圳住房管家｜{report.day:%Y-%m-%d}{LABELS[report.kind]}", f"报表时间：{snapshot}（北京时间）。"]
    if stats:
        lines.append(stats)
    else:
        lines.append("认租机会、批次进展及办理提示详见下方完整报表图片；以导入报表实际收录内容为准。")
    lines.extend([
        f"本份报表完整排成{pages}张图片，请按01起的页码顺序查看。图片保留各区域批次、统计、时间、对象及办理提示，不另附链接。",
        "建议先看顶部概览，再查看关注地区的批次；条件和时间以具体条目为准。跟进指仍在跟踪，不代表仍可申请。",
        f"文中“今天／明天”以{report.day:%m月%d日}为参照，不随转发日期改变。办理前请核对官方最新公告。公开信息整理，非官方账号。",
    ])
    intro = "\n\n".join(lines)
    if text_units(intro) >= 1000 or URL.search(intro):
        raise ValueError("导读超过字数限制或含网址，未输出成品")
    return intro


def export_simple_report(report, output=None, *, work=None, renderer=None, progress=None, target_pages=6):
    if isinstance(target_pages, bool) or not isinstance(target_pages, int) or not 2 <= target_pages <= 12:
        raise ValueError("目标图片数请填写2至12之间的整数")
    root = Path(output or get_settings().project_root / "分享输出").resolve()
    workspace = Path(work or get_settings().project_root / "data/share-tool").resolve()
    if workspace == root or workspace.is_relative_to(root):
        raise ValueError("工具工作目录不能放在成品输出目录内")
    root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda percent, message: None)
    notify(3, "正在核对正文和批次……")
    document = prepare_document(report.markdown)
    table_count=sum(b.kind=='table' for b in document.blocks)
    template = get_settings().project_root / "marketing/share_pack_template.html"
    version = sha256(LAYOUT_VERSION.encode() + template.read_bytes())
    canonical = canonical_hash(document)
    identity = sha256(json.dumps({"canonical": canonical, "kind": report.kind, "day": str(report.day),
                                "clock": report.clock.strftime("%H:%M") if report.clock else "", "version": version,
                                "target_pages": target_pages,
                                "root": str(root)}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    manifests = workspace / "manifests"
    manifests.mkdir(exist_ok=True)
    record_path = manifests / f"{identity}.json"
    # A separate lock from collection / delivery, also protects multiple tool windows.
    with process_lock(workspace / "export.lock", timeout=1):
        notify(8, "正在检查是否已有相同成品……")
        damaged = False
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            target = Path(record["directory"]).resolve()
            names = [f["name"] for f in record["files"]]
            if target.parent == root and target.is_dir() and sorted(p.name for p in target.iterdir()) == sorted(names) and all(
                (target / f["name"]).is_file() and sha256((target / f["name"]).read_bytes()) == f["sha256"] for f in record["files"]
            ):
                validate_pagination(Document([TextBlock(**b) for b in record["blocks"]], [], []), record["layout"])
                notify(100, "内容未变，已复用已有成品，没有新增版本")
                return {"status": "existing", "directory": str(target), "pages": record["pages"],
                        "intro": (target / "导读.txt").read_text(encoding="utf-8-sig"),
                        "image_height": record["image_height"], "target_pages": target_pages,
                        "reason": "内容及排版未变，复用已有成品", "tables":table_count}
            damaged = True
        notify(12, "正在排版，优先通过加长图片控制张数……")
        with tempfile.TemporaryDirectory(prefix="render-", dir=workspace) as build:
            build = Path(build)
            layout = (renderer or ChromeRenderer(target_pages=target_pages, max_height=MAX_IMAGE_HEIGHT, progress=notify)).render(report, document, build)
            validate_pagination(document, layout)
            pages = len(layout["pages"])
            height = layout.get("image_height", HEIGHT)
            if not HEIGHT <= height <= MAX_IMAGE_HEIGHT:
                raise ValueError("图片高度超出可读范围，未输出成品")
            images = sorted((build / "图片").glob("*.png"))
            if {p.name for p in images} != {f"{i:02d}.png" for i in range(1, pages + 1)} or any(png_size(p) != (WIDTH, height) for p in images):
                raise ValueError("图片页码或尺寸校验失败，未输出成品")
            intro = make_intro(report, document, pages)
            notify(92, f"已生成{pages}张图片，正在校验完整性并保存……")
            # Publish only finished artifacts, atomically on the output filesystem.
            with tempfile.TemporaryDirectory(prefix=".building-", dir=root) as temporary:
                staging = Path(temporary) / "成品"
                staging.mkdir()
                (staging / "导读.txt").write_text(intro, encoding="utf-8-sig")
                for image in images:
                    name = f"{int(image.stem):0{max(2, len(str(pages)))}d}.png"
                    shutil.copyfile(image, staging / name)
                files = [{"name": p.name, "sha256": sha256(p.read_bytes())} for p in sorted(staging.iterdir())]
                base = f"{report.day}{LABELS[report.kind]}"
                target, revision = root / base, 1
                while target.exists():
                    revision += 1
                    target = root / f"{base}-第{revision}版"
                reason = "首次生成这份报表" if revision == 1 else (
                    "已有成品被修改或不完整，保留旧文件并另存" if damaged else "正文或排版设置与旧版不同，保留旧版并另存")
                record = {"schema": 2, "directory": str(target), "pages": pages, "files": files,
                          "source_sha256": sha256(report.markdown.encode("utf-8")), "layout": layout,
                          "batch_codes": BATCH.findall(document.text), "canonical_sha256": canonical,
                          "blocks": [asdict(b) for b in document.blocks], "image_height": height,
                          "target_pages": target_pages, "reason": reason, "tables":table_count}
                pending_record = workspace / f"{identity}.tmp"
                pending_record.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                staging.rename(target)
                pending_record.replace(record_path)
            notify(100, "生成完成，导读和全部图片已保存")
            return {"status": "created", "directory": str(target), "pages": pages, "intro": intro,
                    "image_height": height, "target_pages": target_pages, "reason": reason,"tables":table_count}
