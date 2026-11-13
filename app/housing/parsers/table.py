from __future__ import annotations

import re

from bs4 import BeautifulSoup

EXIT_REQUIRED = ("序号", "小区名称")
PROJECT_HEADERS = ("小区名称", "项目名称", "房源项目", "项目")
AREA_RE = re.compile(r"(\d+(?:\.\d+)?)")
SKIP_VALUES = {"", "-", "—", "合计", "小计", "总计", "序号"}
JUNK_PROJECT_TOKENS = (
    "租金",
    "面积",
    "备注",
    "序号",
    "小区名称",
    "项目名称",
    "套数",
    "户型",
    "房型",
    "标准",
    "一房",
    "二房",
    "两房",
    "三房",
    "四房",
    "单房",
    "建筑面积",
    "㎡",
    "平方米",
    "元左右",
)


def extract_exit_rows(content_html: str) -> list[dict]:
    """从退出信息详情页抽取行级记录，只保留社区/房型/面积，不保留姓名。"""
    if not content_html:
        return []
    soup = BeautifulSoup(content_html, "html.parser")
    for table in soup.find_all("table"):
        rows = [row for row in table.find_all("tr") if row.find_all(["td", "th"])]
        if len(rows) < 2:
            continue
        header = [_cell_text(cell) for cell in rows[0].find_all(["td", "th"])]
        if not all(any(token in cell for cell in header) for token in EXIT_REQUIRED):
            continue
        positions = {
            "row_no": _index_of(header, "序号"),
            "housing_type": _index_of(header, "住房类型") or _index_of(header, "房屋类型"),
            "area_sqm": _index_of(header, "面积"),
            "community_name": _index_of(header, "小区名称") or _index_of(header, "项目名称"),
        }
        records: list[dict] = []
        for row in rows[1:]:
            cells = [_cell_text(cell) for cell in row.find_all(["td", "th"])]
            record = _build_exit_record(cells, positions)
            if record:
                records.append(record)
        if records:
            return records
    return []


def _build_exit_record(cells: list[str], positions: dict[str, int]) -> dict | None:
    def take(key: str) -> str | None:
        index = positions.get(key)
        if index is None or index < 0 or index >= len(cells):
            return None
        value = cells[index].strip()
        return None if value in SKIP_VALUES else value

    community = take("community_name")
    if not community:
        return None
    if any(token in community for token in JUNK_PROJECT_TOKENS):
        return None
    row_no_text = take("row_no")
    row_no = int(row_no_text) if row_no_text and row_no_text.isdigit() else None
    area_text = take("area_sqm")
    area = float(AREA_RE.search(area_text).group(1)) if area_text and AREA_RE.search(area_text) else None
    return {
        "row_no": row_no,
        "housing_type": take("housing_type"),
        "area_sqm": area,
        "community_name": community[:128],
    }


# 从正文枚举里拆出来的非项目片段（多字噪声）。
PROJECT_NAME_NOISE = (
    "的",
    "配租",
    "个人",
    "条件",
    "项目",
    "房源",
    "其中",
    "办理",
    "结束",
    "通过",
    "腾退",
    "退出",
    "剩余",
    "家庭",
    "面积",
    "轮候",
    "申请",
    "认租",
    "通知",
    "短信",
    "手机",
    "电话",
    "咨询",
    "了解",
    "视为",
    "结合",
    "可能",
    "方式",
    "部分",
    "保障性租赁住房",
    "公共租赁住房",
    # “具体项目详见附件”“房源明细如下”这类句子会被枚举正则误切成项目名。
    "具体",
    "详见",
    "如下",
    "附件",
    "明细",
    "名单",
)
# 被截断的房型/面积片段，如“龙瑞佳园两”。
TRUNCATED_TAIL = ("一", "二", "两", "三", "四", "房", "套", "㎡")


def filter_projects(names: list[str] | None) -> list[str]:
    """过滤历史入库里混进来的表头/面积/房型等非项目名。"""
    cleaned: list[str] = []
    for name in names or []:
        value = str(name).strip()
        if not is_project_name(value):
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def is_project_name(value: str) -> bool:
    if not 2 <= len(value) <= 40:
        return False
    if any(token in value for token in JUNK_PROJECT_TOKENS):
        return False
    if any(token in value for token in PROJECT_NAME_NOISE):
        return False
    return not value.endswith(TRUNCATED_TAIL)


def extract_project_names(content_html: str, limit: int = 50) -> list[str]:
    if not content_html:
        return []
    soup = BeautifulSoup(content_html, "html.parser")
    names: list[str] = []
    seen: set[str] = set()
    for table in soup.find_all("table"):
        rows = [row for row in table.find_all("tr") if row.find_all(["td", "th"])]
        if not rows:
            continue
        header = [_cell_text(cell) for cell in rows[0].find_all(["td", "th"])]
        column = _project_column(header)
        if column is None:
            continue
        for row in rows[1:]:
            cells = [_cell_text(cell) for cell in row.find_all(["td", "th"])]
            if column >= len(cells):
                continue
            value = cells[column].strip()
            if value in SKIP_VALUES or len(value) < 2 or len(value) > 40:
                continue
            if any(token in value for token in JUNK_PROJECT_TOKENS):
                continue
            if value in seen:
                continue
            seen.add(value)
            names.append(value)
            if len(names) >= limit:
                return names
    return names


def _project_column(header: list[str]) -> int | None:
    for token in PROJECT_HEADERS:
        index = _index_of(header, token, exact=True)
        if index is not None:
            return index
    return None


def _index_of(header: list[str], token: str, exact: bool = False) -> int | None:
    for index, cell in enumerate(header):
        if (cell == token) if exact else (token in cell):
            return index
    return None


def _cell_text(cell) -> str:
    return cell.get_text(" ", strip=True).replace("\xa0", " ").strip()
