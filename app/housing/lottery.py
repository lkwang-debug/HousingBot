"""摇号结果：把附件里的名单变成"多少人抢多少套"，而不是把名单存下来。

三条硬规则：

1. **不落个人信息**。附件里有姓名和证件号（掩码），解析后只保留
   「各队列人数 / 顺序号区间 / 总人数」，一行个人记录都不入库。
2. **不复制文件**。原文 PDF 在政府站上公开可点，推送里直接给链接；
   本地只按需临时下载，用于回答"我排第几"。
3. **解析失败不影响推送**。没有聚合数据就只发通告和附件链接。
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.housing.batches import batch_key
from app.housing.fetch import HousingHttpClient
from app.housing.models import (
    HousingBatchUnit,
    HousingLotteryStat,
    HousingNotice,
    utcnow,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_PAGES = 600
MAX_ROWS = 200_000
MAX_FILES = 8


class IncompleteLotteryRows(ValueError):
    """Known numbered applicant rows were not parsed; do not call it complete."""


APPLICANT_ANCHOR_RE = re.compile(r'(?<![A-Za-z0-9])\d{1,6}\s+([A-Za-z]{1,5}\d[A-Za-z0-9]{5,})(?![A-Za-z0-9])')

LOTTERY_NAME_TERMS = ("摇号", "名单", "排位", "排序", "结果")
UNITS_NAME_TERMS = ("房源概况", "房源情况", "项目概况", "房源信息")
QUEUE_ORDER = ("第一队列", "第二队列", "第三队列", "第四队列", "第五队列")


def queue_sort_key(name: str):
    group = 0 if name.startswith("优先-") else 2 if name.startswith("其他-") else 1
    base = name.split("-")[-1]
    return (group, QUEUE_ORDER.index(base) if base in QUEUE_ORDER else len(QUEUE_ORDER), name)

# 真实样例：1 1 SQ2026BZ360007741 何光宇 4311281990******** 第三队列
ROW_WITH_ROUND_RE = re.compile(
    r"^\s*(?P<round>\d{1,3})\s+(?P<order>\d{1,6})\s+(?P<apply>[A-Za-z0-9]{6,})\s+"
    r"(?P<name>[^\r\n]{1,60}?)\s*(?P<id>[A-Za-z0-9*＊]{8,})\s*(?P<queue>第[一二三四五六七八九十]队列)?\s*(?P<note>[（(][^）)\r\n]{1,120}[）)])?\s*$"
)
ROW_PLAIN_RE = re.compile(
    r"^\s*(?P<order>\d{1,6})\s+(?P<apply>[A-Za-z0-9]{6,})\s+"
    r"(?P<name>[^\r\n]{1,60}?)\s*(?P<id>[A-Za-z0-9*＊]{8,})\s*(?P<queue>第[一二三四五六七八九十]队列)?\s*(?P<note>[（(][^）)\r\n]{1,120}[）)])?\s*$"
)
# 真实样例（坪山/福田公证摇号结果）：队列 顺序号 认租编号，没有姓名和证件号。
#   1    1    SQ2026BZ370001675
COMPACT_ROW_RE = re.compile(
    r"^\s*(?P<q>\d{1,3})\s+(?P<order>\d{1,6})\s+(?P<apply>[A-Za-z][A-Za-z0-9]{5,})\s*$"
)
# 真实样例（宝安选房名单）：选房排位 认租编号 姓名 身份证 与主申请关系 梯队分类 队内摇号结果。
#   1  SQ2025BZ190003650  张*芸  140411********4821  主申请人  第一队列  1
# 排位是全表连续号，队内摇号结果才是"同队列里排第几"，后者才是选房依据。
ROW_TEAM_RE = re.compile(
    r"^\s*(?P<pos>\d{1,6})\s+(?P<apply>[A-Za-z][A-Za-z0-9]{5,})\s+"
    r"(?P<name>[^\r\n]{1,60}?)\s*(?P<id>[A-Za-z0-9*＊]{8,})(?=\s)[^\r\n]{0,1000}?"
    r"(?P<queue>第[一二三四五六七八九十]队列)(?:\s+(?P<order>\d{1,7}))?\s*$"
)
# 有表头没有值的版本（坪山部分批次队列列留空）：顺序号 认租编号。
COMPACT_PAIR_RE = re.compile(
    r"^\s*(?P<order>\d{1,6})\s+(?P<apply>[A-Za-z][A-Za-z0-9]{5,})\s*$"
)
# 真实样例（罗湖/坪山选房名单）：一行家庭被 PDF 拆成多行，
#   1   BHR00085841
#   张三 本市户籍 4403011987********
#   5   20777
#   第二队列
# 用"序号 + 认租编号"当锚点，把中间几行当成一个块来读。
BLOCK_ANCHOR_RE = re.compile(r"^\s*\d{1,6}\s+(?P<apply>[A-Za-z]{1,4}\d{5,})\b")
# 顺序号在有的表里是单独一列（罗湖/南山），有的紧跟在家庭人口数后面（坪山）。
# 先找 5-7 位（避开 2025/2026 这种年份），没有再退到 4 位。
BLOCK_ORDER_RE = re.compile(r"(?<![\d*])(?P<order>\d{5,7})(?![\d*])")
BLOCK_ORDER_SHORT_RE = re.compile(r"(?<![\d*])(?P<order>\d{4})(?![\d*])")
BLOCK_QUEUE_RE = re.compile(r"(第[一二三四五六七八九十]队列)")
QUEUE_GROUP_RE = re.compile(r"(优先|其他)[^。\s]{0,6}队列")
QUEUE_LABEL_RE = re.compile(r"(第[一二三四五六七八九十]队列)")
MASKED_ID_RE = re.compile(r"\d{6,}\*{3,}")
UNITS_LINE_RES = (
    re.compile(r"(?P<label>100)\s*(?:㎡|平方米|平米)?\s*(?:及以上|以上)\s*(?:的)?(?:房源)?\s*[:：]?\s*(?P<units>\d{1,5})(?!\d)"),
    re.compile(r"(?P<label>40|50|90)\s*(?:㎡|平方米|平米)?\s*左右\s*(?:的)?(?:房源)?\s*[:：]?\s*(?P<units>\d{1,5})(?!\d)"),
)
CATEGORY_FROM_LABEL = (
    (re.compile(r"100\s*(?:㎡|平方米|平米)?\s*(?:及以上|以上)"), "100+"),
    (re.compile(r"40\s*(?:㎡|平方米|平米)?\s*左右"), "40"),
    (re.compile(r"50\s*(?:㎡|平方米|平米)?\s*左右"), "50"),
    (re.compile(r"90\s*(?:㎡|平方米|平米)?\s*左右"), "90"),
    (re.compile(r"单身公寓|单房"), "40"),
    (re.compile(r"一房"), "50"),
    (re.compile(r"两房|二房"), "两房"),
    # 小三房和三房是两拨人抢两拨房，合并会把两边的人数算到一起。
    (re.compile(r"小三房"), "小三房"),
    (re.compile(r"三房"), "三房"),
)
HEADER_TERMS = ("姓名", "身份证", "认租编号", "顺序号", "排位", "队列")
COLUMN_ALIASES = {
    "order": ("顺序号", "选房排序", "选房排位", "排位", "排序", "序号"),
    "apply": ("认租编号", "申请编号", "受理编号", "认租申请编号", "备案回执号"),
    "queue": ("队列分类", "队列", "选房队列"),
    "name": ("姓名", "申请人", "主申请人"),
}


@dataclass
class LotteryRow:
    order: int
    apply_no: str
    queue: str = ""
    name: str = ""
    round: int = 0
    annotated: bool = False


@dataclass
class LotteryAggregate:
    category_key: str
    label: str
    rows: int
    max_order: int | None
    queue_counts: dict[str, int] = field(default_factory=dict)
    queue_ranges: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class CategorySummary:
    key: str
    label: str
    applicants: int
    queue_counts: dict[str, int] = field(default_factory=dict)
    units: int | None = None

    @property
    def odds(self) -> float | None:
        if not self.units or not self.applicants:
            return None
        return min(1.0, self.units / self.applicants)


@dataclass
class LotterySummary:
    notice_id: int
    categories: list[CategorySummary] = field(default_factory=list)
    parse_error: str | None = None


def category_key(label: str) -> str:
    import hashlib
    text = re.sub(r"^\s*(?:附件)?\d+[.、．：:](?!\d)\s*", "", label or "")
    # Area and layout are distinct dimensions; a one-room flat is not necessarily 50m².
    project = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,20}(?:新筑|花园|新苑|家园|雅苑|公馆|新居|逸居|府))", text)
    prefix = (project.group(1) + "|") if project else ""
    multiple = re.search(r"(\d{2,3}(?:\s*[、,，]\s*\d{2,3})+)\s*(?:㎡|平方米|平米)", text)
    if multiple:
        return prefix + ",".join(sorted(set(re.findall(r"\d+", multiple.group(1))), key=int))
    interval = re.search(r"(\d{2,3})\s*[-—～~至]\s*(\d{2,3})\s*(?:㎡|平方米|平米)", text)
    if interval:
        return prefix + interval.group(1) + "-" + interval.group(2)
    for pattern, key in CATEGORY_FROM_LABEL[:4]:
        if pattern.search(text):
            return prefix + key
    area = re.search(r"(\d{2,3}(?:\.\d+)?)\s*(?:㎡|平方米|平米)", text)
    if area:
        return prefix + area.group(1)
    if re.fullmatch(r"\d{2,3}(?:\.\d+)?", text.strip()):
        return prefix + text.strip()
    for pattern, key in CATEGORY_FROM_LABEL[4:]:
        if pattern.search(text):
            if key == "40": key = "单身公寓"
            elif key == "50": key = "一房"
            return prefix + key
    normalized = re.sub(r"第[一二三四五六七八九十]+队列|(?:优先|其他)配租队列|[\s\d.、．]+|摇号|公证|结果|名单|\.(?:pdf|xlsx?|et)$", "", text)
    return "unknown:" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def category_display(label: str, key: str) -> str:
    """附件名通常很长，截成买家一眼能看懂的一段。"""
    clean = re.sub(r"^附件?\s*\d*[.、．:：]?\s*", "", (label or "").strip())
    clean = re.sub(r"\.(pdf|xlsx?|docx?|zip)$", "", clean, flags=re.I)
    clean = re.sub(r"^建筑面积", "", clean)
    clean = re.sub(r"的?(?:认租|认购)?申请家庭(?:公证)?(?:摇号|选房)(?:结果|名单)$", "", clean)
    clean = re.sub(r"(?:公证)?(?:摇号|选房)(?:结果|名单)$", "", clean)
    return clean.strip(" ：:")[:60] or key


# ---------------------------------------------------------------- 文本解析


def parse_pdf_rows(data: bytes) -> list[LotteryRow]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("PDF 已加密") from exc
    if len(reader.pages) > MAX_ATTACHMENT_PAGES:
        raise ValueError("PDF 超过页数上限，不能将截断名单当作完整结果")
    pages = reader.pages[:MAX_ATTACHMENT_PAGES]
    if not pages:
        return []
    # 普通模式就够读绝大多数名单；按坐标对齐的布局模式慢一个数量级，只当兜底。
    try:
        rows = _parse_pages(pages, layout=False)
    except IncompleteLotteryRows:
        rows = []
    if not rows:
        rows = _parse_pages(pages, layout=True)
    return rows


def _parse_pages(pages, *, layout: bool) -> list[LotteryRow]:
    rows: list[LotteryRow] = []
    allow_queue = _has_queue_column(pages, layout)
    for page in pages:
        text = _page_text(page, layout)
        page_rows = _page_rows(text, allow_queue=allow_queue)
        expected = Counter(m.group(1).upper() for m in APPLICANT_ANCHOR_RE.finditer(text))
        parsed = Counter(row.apply_no.upper() for row in page_rows)
        missing = sum((expected-parsed).values())
        if missing:
            raise IncompleteLotteryRows(f'存在{missing}条已识别认租编号但未解析的名单记录，不能发布完整统计')
        rows.extend(page_rows)
        if len(rows) > MAX_ROWS:
            raise ValueError("名单超过行数上限")
    return rows


def _has_queue_column(pages, layout: bool) -> bool:
    """表头里有「队列」才把首列当队列读。龙华华章新筑的表头是「轮数 顺序号 认租编号」，
    首列是第几轮（有的家庭摇了两轮），按队列读会把同一队拆成好几队。"""
    for page in pages[:3]:
        # 有的 PDF 一个单元格一行（"轮数\n顺序号\n认租编号"），先去掉空白再找。
        head = re.sub(r"\s+", "", _page_text(page, layout)[:2000])
        if "队列顺序号" in head or "顺序号队列" in head:
            return True
        if "轮数" in head and "顺序号" in head:
            return False
    return True


def _page_rows(text: str, *, allow_queue: bool = True) -> list[LotteryRow]:
    """一页名单可能是"一行一个家庭"，也可能是"一个家庭好几行"，两种都要认。"""
    # 带掩码证件号的是多行版式；这种页里"序号 认租编号"不能当顺序号读。
    allow_pairs = not MASKED_ID_RE.search(text or "")
    rows = parse_text_rows(text, allow_pairs=allow_pairs, allow_queue=allow_queue)
    if rows:
        return rows
    return parse_block_rows(text)


def _page_text(page, layout: bool) -> str:
    try:
        return (page.extract_text(extraction_mode="layout") if layout else page.extract_text()) or ""
    except (KeyError, TypeError):  # 老版本 pypdf 不支持 layout 模式
        return page.extract_text() or ""


def parse_text_rows(
    text: str, *, allow_pairs: bool = True, allow_queue: bool = True
) -> list[LotteryRow]:
    rows: list[LotteryRow] = []
    for line in (text or "").splitlines():
        row = _match_row(line, allow_pairs=allow_pairs, allow_queue=allow_queue)
        if row is not None:
            rows.append(row)
    return rows


def parse_block_rows(text: str) -> list[LotteryRow]:
    """一个家庭跨多行的名单：序号 + 认租编号当锚点，往后读到下一个锚点。"""
    rows: list[LotteryRow] = []
    apply_no = ""
    buffer: list[str] = []

    def flush() -> None:
        if not apply_no:
            return
        # 去掉认租编号本身，否则 SQ2025BZ... 里的 2025 会被当成顺序号。
        body = "\n".join(buffer).replace(apply_no, " ")
        order_match = BLOCK_ORDER_RE.search(body) or BLOCK_ORDER_SHORT_RE.search(body)
        if order_match is None:
            return
        order = int(order_match.group("order"))
        if order <= 0:
            return
        queue_match = BLOCK_QUEUE_RE.search(body)
        rows.append(
            LotteryRow(
                order=order,
                apply_no=apply_no,
                queue=queue_match.group(1) if queue_match else "",
            )
        )

    for line in (text or "").splitlines():
        anchor = BLOCK_ANCHOR_RE.match(line)
        if anchor is not None:
            flush()
            apply_no = anchor.group("apply")
            buffer = [line]
        elif apply_no:
            buffer.append(line)
    flush()
    return rows


def _match_row(
    line: str, *, allow_pairs: bool = True, allow_queue: bool = True
) -> LotteryRow | None:
    match = ROW_WITH_ROUND_RE.match(line) or ROW_PLAIN_RE.match(line)
    if match is not None:
        try:
            order = int(match.group("order"))
        except (TypeError, ValueError):
            return None
        if order <= 0:
            return None
        return LotteryRow(
            order=order,
            apply_no=(match.group("apply") or "").strip(),
            queue=(match.group("queue") or "").strip(),
            name=(match.group("name") or "").strip(),
            round=int(match.groupdict().get("round") or 0),
            annotated=bool(match.groupdict().get('note')),
        )
    team = ROW_TEAM_RE.match(line)
    if team is not None:
        order_text = team.group("order") or team.group("pos")
        order = int(order_text)
        if order > 0:
            return LotteryRow(
                order=order,
                apply_no=team.group("apply").strip(),
                queue=team.group("queue").strip(),
                name=team.group("name").strip(),
            )
    compact = COMPACT_ROW_RE.match(line)
    if compact is not None:
        order = int(compact.group("order"))
        if order <= 0:
            return None
        index = int(compact.group("q"))
        queue = QUEUE_ORDER[index - 1] if 1 <= index <= len(QUEUE_ORDER) else ""
        return LotteryRow(
            order=order,
            apply_no=compact.group("apply").strip(),
            queue=queue if allow_queue else "",
            round=0 if allow_queue else index,
        )
    if not allow_pairs:
        return None
    pair = COMPACT_PAIR_RE.match(line)
    if pair is None:
        return None
    order = int(pair.group("order"))
    if order <= 0:
        return None
    return LotteryRow(order=order, apply_no=pair.group("apply").strip())


def parse_excel_rows(data: bytes, suffix: str) -> list[LotteryRow]:
    table = _read_table(data, suffix)
    if not table:
        return []
    header_index, columns = _find_header(table)
    if header_index is None:
        return []
    rows: list[LotteryRow] = []
    for cells in table[header_index + 1 :]:
        row = _row_from_cells(cells, columns)
        if row is not None:
            rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError("名单超过行数上限")
    return rows


def _read_table(data: bytes, suffix: str) -> list[list[str]]:
    # 政府站的扩展名经常和真实格式对不上（.xlsx 其实是老 .xls，反之亦然），两种都试。
    readers = (_read_xlsx, _read_xls) if suffix == "xlsx" else (_read_xls, _read_xlsx)
    for reader in readers:
        try:
            return reader(data)
        except Exception:  # noqa: BLE001 换下一种格式再试
            continue
    return []


def _read_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        return [
            ["" if cell is None else str(cell).strip() for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()


def _read_xls(data: bytes) -> list[list[str]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    return [
        [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
        for r in range(sheet.nrows)
    ]


def _find_header(table: list[list[str]]) -> tuple[int | None, dict[str, int]]:
    for index, cells in enumerate(table[:30]):
        joined = "".join(cells)
        if sum(1 for term in HEADER_TERMS if term in joined) < 2:
            continue
        columns: dict[str, int] = {}
        for field, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                hit = next((i for i, cell in enumerate(cells) if alias in cell), None)
                if hit is not None:
                    columns[field] = hit
                    break
        if "order" in columns:
            return index, columns
    return None, {}


def _row_from_cells(cells: list[str], columns: dict[str, int]) -> LotteryRow | None:
    def take(field: str) -> str:
        index = columns.get(field)
        if index is None or index >= len(cells):
            return ""
        return str(cells[index]).strip()

    order_text = take("order").split(".")[0]
    if not order_text.isdigit():
        return None
    order = int(order_text)
    if order <= 0:
        return None
    return LotteryRow(order=order, apply_no=take("apply"), queue=take("queue"), name=take("name"))


def queue_prefix(label: str) -> str:
    """福田会把同一批按"优先/其他配租队列"拆成两份附件，队列编号各自从 1 开始，
    不加前缀直接合并会把两组人算成同一队。"""
    match = QUEUE_GROUP_RE.search(label or "")
    return f"{match.group(1)}-" if match else ""


def queue_hint(label: str) -> str:
    """龙华、福田有些批次把队列拆成多个附件，队列只写在附件名里（"…第一队列摇号结果"），
    PDF 行内没有队列列；这时只能按附件名补，否则三个队列会被混成一队。"""
    match = QUEUE_LABEL_RE.search(label or "")
    return match.group(1) if match else ""


def _queue_name(queue: str, prefix: str = "", hint: str = "") -> str:
    # 附件名写了「第一队列」就是这一队，比表里读出来的列更可信
    # （华章新筑的表里首列是轮数，不是队列）。
    name = hint or queue or "未标注"
    return f"{prefix}{name}" if prefix else name


def aggregate(rows: list[LotteryRow], label: str) -> LotteryAggregate | None:
    if not rows:
        return None
    prefix = queue_prefix(label)
    hint = queue_hint(label)
    counts = Counter(_queue_name(row.queue, prefix, hint) for row in rows)
    ranges: dict[str, list[int]] = {}
    for queue in counts:
        orders = [row.order for row in rows if _queue_name(row.queue, prefix, hint) == queue]
        if orders:
            ranges[queue] = [min(orders), max(orders)]
    ordered = {queue: counts[queue] for queue in QUEUE_ORDER if queue in counts}
    ordered.update({queue: value for queue, value in counts.items() if queue not in ordered})
    ordered_ranges = {queue: ranges[queue] for queue in ordered if queue in ranges}
    return LotteryAggregate(
        category_key=category_key(label),
        label=category_display(label, category_key(label)),
        rows=len(rows),
        max_order=max(row.order for row in rows),
        queue_counts=ordered,
        queue_ranges=ordered_ranges,
    )


def parse_units_overview(text: str) -> dict[str, tuple[str, int]]:
    """Use explicit area bands and, when present, the final subtotal block."""
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text or "")
    starts = [text.find(term) for term in ("房源概况", "房源情况", "项目基本情况", "房源基本情况") if text.find(term) >= 0]
    if starts:
        text = text[min(starts):]
    section = re.split(r"上述房源|注[：:]|备注[：:]|[（(]二[）)]\s*费用租金|二、\s*配租对象|二、\s*申请", text, maxsplit=1)[0]
    pattern = re.compile(
        r"(?<![\d.\-—~～])(?P<label>\d{2,3}(?:[、,，\-—~～]\d{2,3})*)\s*(?:㎡|平方米|平米)?\s*"
        r"(?P<band>左右|及以上|以上)\s*(?:的)?(?:房源|住房)?\s*[:：]?\s*"
        r"(?P<units>\d{1,5})(?![\d年月日㎡])"
    )
    matches = list(pattern.finditer(section))
    markers = list(re.finditer(r"小计|合计", section))
    if markers:
        subtotal = [m for m in matches if m.start() > markers[-1].end()]
        if subtotal:
            matches = subtotal
    candidates = {}
    for match in matches:
        label = match.group("label")
        key = category_key(label + "㎡" + match.group("band"))
        value = int(match.group("units"))
        if 0 < value <= 100000:
            candidates.setdefault(key, set()).add(value)
    # Conflicting per-project values are not a batch total unless explicitly subtotalled.
    return {key: (key.replace("+", "及以上") + ("" if key.endswith("+") else "左右"), next(iter(values)))
            for key, values in candidates.items() if len(values) == 1}


def parse_units_pdf(data: bytes) -> dict[str, tuple[str, int]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    return parse_units_overview(text)


# ---------------------------------------------------------------- 入库


async def _ensure_detail(
    session: Session, client: HousingHttpClient, notice: HousingNotice
) -> HousingNotice:
    """老通告的正文/附件可能一直没抓过（scan 只补新条目），解析前先补一次。

    直接改当前行，不走 store_notice：store_notice 会按 URL 重新定位通告，
    对已经在库、id 被统计表引用的行没必要，也有误合并风险。
    """
    # 标了 ok 却没有附件，多半是当年解析器没认出「相关附件」块，值得再抓一次。
    if (notice.detail_checked_at and notice.detail_checked_at > utcnow() - timedelta(hours=24)) or not notice.canonical_url:
        return notice
    from app.housing.parsers import parse_detail

    try:
        if not session.info.get("housing_dry_run"):
            session.commit()
        response = await client.get(notice.canonical_url)
        detail = parse_detail(response.text, response.url)
    except Exception:  # noqa: BLE001 补抓失败就维持原样，按"没有附件"处理
        return notice
    notice.attachments = detail.attachments or notice.attachments
    if detail.body_text:
        notice.body_text = detail.body_text
    notice.detail_status = "ok"
    notice.detail_checked_at = utcnow()
    notice.detail_attempts = (notice.detail_attempts or 0) + 1
    session.flush()
    return notice


async def ingest_lottery(session, client, notice, *, force=False):
    from app.housing.ingestion import ingest_lottery as ingest
    return await ingest(session, client, notice, force=force)


async def read_attachment_rows(
    client: HousingHttpClient, notice: HousingNotice, *, strict: bool = True
) -> list[tuple[str, list[LotteryRow]]]:
    """把摇号附件读成内存里的行，给"自助查询页"生成器用——同样不落库。"""
    result: list[tuple[str, list[LotteryRow]]] = []
    files = _pick_files(notice, LOTTERY_NAME_TERMS)
    if strict and (not files or len(files) > MAX_FILES):
        raise ValueError("名单附件缺失或超过处理上限，无法确认完整数据")
    for attachment in files[:MAX_FILES]:
        url = str(attachment.get("url") or "")
        kind = _attachment_kind(attachment)
        if not url or kind not in {"pdf", "xls", "xlsx", "et"}:
            if strict:
                raise ValueError("存在不支持的名单附件，无法确认完整数据")
            continue
        try:
            data = await client.get_bytes(url, referer=notice.canonical_url)
            rows = (
                parse_pdf_rows(data)
                if kind == "pdf"
                else parse_excel_rows(data, "xls" if kind == "et" else kind)
            )
        except Exception as exc:
            if strict:
                raise ValueError(f"公告{notice.id}的附件《{attachment.get('name','未命名')}》未完整读取，暂停查询估算") from exc
            continue
        if not rows and strict:
            raise ValueError("名单附件没有识别到记录，无法确认完整数据")
        if rows:
            result.append((str(attachment.get("name") or ""), rows))
    return result


async def ingest_units(session, client, notice, *, force=False):
    from app.housing.ingestion import ingest_units as ingest
    return await ingest(session, client, notice, force=force)


def _store_units(
    session: Session,
    notice: HousingNotice,
    found: dict[str, tuple[str, int]],
    *,
    source_url: str,
) -> list[HousingBatchUnit]:
    key = batch_key(notice)
    if not key:
        return []
    rows: list[HousingBatchUnit] = []
    for cat_key, (label, units) in found.items():
        existing = session.scalar(
            select(HousingBatchUnit).where(
                HousingBatchUnit.batch_key == key,
                HousingBatchUnit.category_key == cat_key,
            )
        )
        if existing is None:
            existing = HousingBatchUnit(batch_key=key, category_key=cat_key)
            session.add(existing)
        existing.region_code = notice.region_code or ""
        existing.label = label
        existing.units = units
        existing.source_notice_id = notice.id
        existing.source_url = source_url
        existing.parsed_at = utcnow()
        rows.append(existing)
    session.flush()
    return rows


async def backfill_lottery(
    session: Session,
    client: HousingHttpClient,
    *,
    days: int = 180,
    force: bool = False,
    include_units: bool = True,
) -> dict:
    """给老库补数据：已有的摇号通告补人数，已有的配租通告补分档房源数。"""
    seen_at = func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)
    since = utcnow() - timedelta(days=days)
    _prune_reclassified(session)
    lottery_notices = list(
        session.scalars(
            select(HousingNotice)
            .where(
                HousingNotice.category == "lottery_result",
                HousingNotice.is_target.is_(True),
                seen_at >= since,
            )
            .order_by(seen_at.desc())
            .limit(80)
        ).all()
    )
    parsed = skipped = failed = 0
    for notice in lottery_notices:
        stats = await ingest_lottery(session, client, notice, force=force)
        if any(stat.parsed for stat in stats):
            parsed += 1
        else:
            failed += 1
        # 一条一批次：单条解析可能几十秒，长事务会把定时扫描的写锁卡住。
        session.commit()

    unit_batches = 0
    if include_units:
        launches = list(
            session.scalars(
                select(HousingNotice)
                .where(
                    HousingNotice.category == "rent_launch",
                    HousingNotice.is_target.is_(True),
                    seen_at >= since,
                )
                .order_by(seen_at.desc())
                .limit(60)
            ).all()
        )
        for notice in launches:
            key = batch_key(notice)
            if not key:
                continue
            if await ingest_units(session, client, notice, force=force):
                unit_batches += 1

    return {
        "lottery_notices": len(lottery_notices),
        "parsed": parsed,
        "skipped": skipped,
        "failed": failed,
        "unit_batches": unit_batches,
    }


def _prune_reclassified(session: Session) -> None:
    """分类规则收紧后，老库里已不属于 lottery_result 的通告不再保留测算数据。"""
    stale = select(HousingLotteryStat.id).join(
        HousingNotice, HousingNotice.id == HousingLotteryStat.notice_id
    ).where(HousingNotice.category != "lottery_result")
    session.execute(delete(HousingLotteryStat).where(HousingLotteryStat.id.in_(stale)))
    session.flush()


def _attachment_kind(attachment: dict) -> str:
    """附件类型以标题里的文件名为准：老数据里 kind 常是 file，URL 又没有扩展名。"""
    kind = str(attachment.get("kind") or "").lower()
    if kind and kind != "file":
        return kind
    for text in (str(attachment.get("name") or ""), str(attachment.get("url") or "")):
        lowered = text.lower().split("?")[0]
        for suffix in ("pdf", "xlsx", "xls", "et"):
            if lowered.endswith(f".{suffix}"):
                return suffix
    return kind or "file"


def _pick_files(notice: HousingNotice, terms: tuple[str, ...]) -> list[dict]:
    files = []
    for attachment in notice.attachments or []:
        name = str(attachment.get("name") or "")
        if any(term in name for term in terms):
            files.append(attachment)
    return files


def _has_stats(session: Session, notice_id: int) -> bool:
    """有任何一行就认为处理过：失败也记一行，避免每轮扫描都重下几 MB 的 PDF。"""
    return bool(
        session.scalar(
            select(HousingLotteryStat.id).where(HousingLotteryStat.notice_id == notice_id)
        )
    )


def _has_units(session: Session, key: str) -> bool:
    return bool(
        session.scalar(select(HousingBatchUnit.id).where(HousingBatchUnit.batch_key == key))
    )


def _upsert_stat(
    session: Session,
    notice: HousingNotice,
    *,
    category_key: str,
    label: str,
    aggregate: LotteryAggregate | None,
    source_url: str,
    error: str | None,
) -> HousingLotteryStat:
    stat = session.scalar(
        select(HousingLotteryStat).where(
            HousingLotteryStat.notice_id == notice.id,
            HousingLotteryStat.category_key == category_key,
        )
    )
    if stat is None:
        stat = HousingLotteryStat(notice_id=notice.id, category_key=category_key)
        session.add(stat)
    stat.batch_key = batch_key(notice) or ""
    stat.region_code = notice.region_code or ""
    stat.label = label
    stat.source_url = source_url
    stat.error = error
    stat.parsed_at = utcnow()
    if aggregate is not None:
        _fill_stat(stat, aggregate)
    session.flush()
    return stat


def _fill_stat(stat: HousingLotteryStat, agg: LotteryAggregate) -> None:
    """同一通告下多份同档附件（合格/候补/递补名单）要累加，不能后一份覆盖前一份。"""
    if stat.parsed:
        counts = Counter(stat.queue_counts or {})
        counts.update(agg.queue_counts)
        ranges = dict(stat.queue_ranges or {})
        for queue, (low, high) in (agg.queue_ranges or {}).items():
            current = ranges.get(queue)
            if current:
                ranges[queue] = [min(current[0], low), max(current[1], high)]
            else:
                ranges[queue] = [low, high]
        stat.total_rows = (stat.total_rows or 0) + agg.rows
        stat.max_order = max(stat.max_order or 0, agg.max_order or 0) or None
        stat.queue_counts = dict(counts)
        stat.queue_ranges = ranges
        return
    stat.label = agg.label
    stat.total_rows = agg.rows
    stat.max_order = agg.max_order
    stat.queue_counts = agg.queue_counts
    stat.queue_ranges = agg.queue_ranges
    stat.parsed = True
    stat.error = None


# ---------------------------------------------------------------- 渲染用摘要


def summary_map(session: Session, notices: list[HousingNotice]) -> dict[int, LotterySummary]:
    """给推送渲染用的轻量摘要：每档认租人数 + 该批次该档房源数。"""
    if not notices:
        return {}
    unit_cache: dict[str, dict[str, HousingBatchUnit]] = {}
    result: dict[int, LotterySummary] = {}
    for notice in notices:
        if notice.category != "lottery_result":
            continue
        stats = list(
            session.scalars(
                select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id)
            ).all()
        )
        key = batch_key(notice) or ""
        if key not in unit_cache:
            unit_cache[key] = {
                row.category_key: row
                for row in session.scalars(
                    select(HousingBatchUnit).where(HousingBatchUnit.batch_key == key)
                ).all()
            } if key else {}
        units = unit_cache[key]
        categories: list[CategorySummary] = []
        for stat in stats:
            if not stat.parsed:
                continue
            unit = units.get(stat.category_key)
            categories.append(
                CategorySummary(
                    key=stat.category_key,
                    label=stat.label,
                    applicants=stat.total_rows,
                    queue_counts=dict(stat.queue_counts or {}),
                    units=unit.units if unit and unit.verified and stat.complete and key else None,
                )
            )
        error = next((stat.error for stat in stats if stat.error), None)
        result[notice.id] = LotterySummary(
            notice_id=notice.id, categories=categories, parse_error=error
        )
    return result


# ---------------------------------------------------------------- 个人查询


@dataclass
class Applicant:
    notice_id: int
    category_key: str
    category_label: str
    queue: str
    order: int
    total: int
    queue_counts: dict[str, int]
    queue_orders: list[int] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


async def find_applicant(client, notice, apply_no):
    from app.housing.ingestion import row_issues
    target = (apply_no or "").strip().upper()
    grouped = {}
    for label, rows in await read_attachment_rows(client, notice):
        key = category_key(label)
        group = grouped.setdefault(key, {"label": label, "rows": []})
        for row in rows:
            row.queue = _queue_name(row.queue, queue_prefix(label), queue_hint(label))
        group["rows"].extend(rows)
    matches = []
    for key, group in grouped.items():
        rows, label = group["rows"], group["label"]
        for row in rows:
            if row.apply_no.upper() == target:
                agg = aggregate(rows, label)
                matches.append(Applicant(notice_id=notice.id, category_key=key,
                    category_label=category_display(label, key), queue=row.queue, order=row.order,
                    total=len(rows), queue_counts=agg.queue_counts,
                    queue_orders=[r.order for r in rows if r.queue == row.queue], issues=row_issues(rows)))
    if len(matches) > 1:
        for match in matches:
            match.issues.append("认租编号匹配多条记录，需核对户型和轮次")
    return matches[0] if matches else None
