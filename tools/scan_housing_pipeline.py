"""Scan collected rent notices for project-level delivery signals.

配租通告的正文里会写每个项目是「现房」还是「预计交付时间为 YYYY 年 M 月」。
这是全市唯一按项目给出竣工/交付时点的官方字段，配租预测就从这里取信号。

只读：以 mode=ro 打开 SQLite，产出一份 markdown 报告，不改任何数据。

    python -m tools.scan_housing_pipeline --since 2025-01-01
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "stockbot.db"

# 「（一）项目名 该项目位于…」——项目段的起点。项目名后面可能还带「（无学位）」这类括注。
SEGMENT = re.compile(r"[（(]([一二三四五六七八九十]+)[）)]\s*([^，。\s]{2,20}?)(?:（[^）]*）)?\s*(?:该项目)?位于")
DELIVERY = re.compile(r"预计交付时间[为是]?\s*(\d{4})年(\d{1,2})月")
SUPPLY = re.compile(r"本次供应\s*(\d+)\s*套")
READY = "项目均已达到入住条件"
# 有些区不写「（一）项目名」，而是「（一）基本情况 御景荟都项目（以下简称『项目』）位于…」。
# 这类句式靠 DELIVERY 命中后再回溯项目名，比硬套段首正则稳。
BACKTRACK = re.compile(r"([一-龥A-Za-z0-9]{2,16})(?:项目)?(?:（以下简称[^）]*）)?\s*$")

# 项目名里偶尔混进编号或标点，统一收一下。
NAME_NOISE = re.compile(r"^(该项目|项目)+|(项目|[·・])$")


def _status(text: str) -> str:
    if "现房" in text:
        return "现房"
    delivery = DELIVERY.search(text)
    if delivery:
        return f"预计{delivery.group(1)}-{int(delivery.group(2)):02d}"
    return "未知"


def scan(since: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        """select region_code, doc_number, published_at, title, body_text, content_hash
           from housing_notices
           where is_target = 1
             and category in ('rent_launch', 'rent_retire_launch')
             and published_at >= ?
           order by published_at""",
        (since,),
    )
    seen_hash: set[str] = set()
    records: list[dict] = []
    for region, doc, published, title, body, content_hash in rows:
        if not body or content_hash in seen_hash:
            continue
        seen_hash.add(content_hash)
        text = re.sub(r"\s+", " ", body)
        notice_ready = READY in text
        matches = list(SEGMENT.finditer(text))
        for match in matches:
            name = NAME_NOISE.sub("", match.group(2))
            if len(name) < 2:
                continue
            window = text[match.end():match.end() + 260]
            supply_at = window.find("本次供应")
            status_text = window[:supply_at] if supply_at > 0 else window[:120]
            status = _status(status_text)
            if status == "未知" and notice_ready:
                status = "已入住条件"
            supply = SUPPLY.search(window)
            records.append({
                "region": region,
                "doc": doc or "",
                "published": (published or "")[:10],
                "notice": re.sub(r"（.*?）", "", title or "")[:44],
                "project": name,
                "status": status,
                "units": int(supply.group(1)) if supply else None,
            })
        records.extend(_delivery_fallback(text, region, doc or "", (published or "")[:10],
                                          re.sub(r"（.*?）", "", title or "")[:44], records))
    conn.close()
    return records


def _delivery_fallback(text, region, doc, published, notice, existing) -> list[dict]:
    """段首正则没覆盖到的格式，用「预计交付时间」反查项目名补齐。"""
    known = {(r["region"], r["published"], r["project"]) for r in existing if r["region"] == region}
    extra = []
    for match in DELIVERY.finditer(text):
        before = text[max(0, match.start() - 400):match.start()]
        anchor = before.rfind("位于")
        if anchor < 0:
            continue
        name_match = BACKTRACK.search(before[:anchor].split("。")[-1])
        if not name_match:
            continue
        name = NAME_NOISE.sub("", name_match.group(1))
        if len(name) < 2 or (region, published, name) in known:
            continue
        known.add((region, published, name))
        extra.append({
            "region": region,
            "doc": doc,
            "published": published,
            "notice": notice,
            "project": name,
            "status": f"预计{match.group(1)}-{int(match.group(2)):02d}",
            "units": None,
        })
    return extra


def transitions(records: list[dict]) -> list[tuple]:
    """同一项目先后出现「预计交付」与「现房」的，就是交付动作完成的证据。"""
    timeline: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        timeline[(record["region"], record["project"])].append(record)
    out = []
    for (region, project), items in timeline.items():
        items.sort(key=lambda r: r["published"])
        expected = next((i for i in items if i["status"].startswith("预计")), None)
        ready = next((i for i in items if i["status"] in {"现房", "已入住条件"}), None)
        if expected and ready and ready["published"] > expected["published"]:
            out.append((region, project, expected["status"], expected["published"],
                        ready["published"], ready["status"]))
    return sorted(out)


def render(records: list[dict]) -> str:
    today = date.today().isoformat()
    pending = [r for r in records if r["status"].startswith("预计")]
    unknown_regions = sorted({r["region"] for r in records if r["status"] == "未知"})

    lines = [
        f"# 保障房项目交付信号扫描（{today}）",
        "",
        "数据来源：`data/stockbot.db` 中 `category in ('rent_launch','rent_retire_launch')` 的通告正文。",
        "状态判定：正文写「项目为现房」记为现房；写「预计交付时间为 YYYY 年 M 月」记为预计交付；两者都没有记为未知。",
        "",
        "## 一、明确写了未来交付时点的项目",
        "",
    ]
    if pending:
        lines += ["| 通告日 | 区 | 文号 | 项目 | 状态 | 本次供应 |", "|---|---|---|---|---|---|"]
        for r in sorted(pending, key=lambda r: (r["status"], r["published"])):
            units = f"{r['units']} 套" if r["units"] else "—"
            lines.append(f"| {r['published']} | {r['region']} | {r['doc'] or '—'} | {r['project']} | {r['status']} | {units} |")
    else:
        lines.append("（无）")

    lines += ["", "## 二、「预计交付 → 现房」的状态迁移", "",
              "同一项目先写预计交付、后写现房，说明交付已经完成。迁移间隔就是该区「交付到可配租」的实际节奏。", ""]
    moves = transitions(records)
    if moves:
        lines += ["| 区 | 项目 | 先前状态 | 先前通告 | 转为现房 | 转为 |", "|---|---|---|---|---|---|"]
        for region, project, old, old_at, new_at, new in moves:
            days = (datetime.fromisoformat(new_at) - datetime.fromisoformat(old_at)).days
            lines.append(f"| {region} | {project} | {old} | {old_at} | {new_at}（{days} 天） | {new} |")
    else:
        lines.append("（无）")

    by_region: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_region[r["region"]].append(r)
    lines += ["", "## 三、按区分布", "", "| 区 | 项目数 | 非现房 | 最近一次通告 |", "|---|---|---|---|"]
    for region in sorted(by_region):
        items = by_region[region]
        latest = max(i["published"] for i in items)
        not_ready = len({i["project"] for i in items if i["status"] != "现房"})
        lines.append(f"| {region} | {len({i['project'] for i in items})} | {not_ready} | {latest} |")

    lines += ["", "## 四、解析盲区", ""]
    if unknown_regions:
        lines.append(f"以下区的通告用表格而非「项目位于…」句式，项目状态未能抽取，需人工补：{'、'.join(unknown_regions)}。")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描配租通告里的项目交付信号")
    parser.add_argument("--since", default="2025-01-01", help="通告起始日期，默认 2025-01-01")
    parser.add_argument("--out", default="", help="输出路径，默认 docs/housing-pipeline-YYYYMMDD.md")
    parser.add_argument("--print", dest="to_stdout", action="store_true", help="打印到终端而不写文件")
    args = parser.parse_args()

    records = scan(args.since)
    report = render(records)
    if args.to_stdout:
        print(report)
        return
    out = Path(args.out) if args.out else ROOT / "docs" / f"housing-pipeline-{date.today():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"已写入 {out}（{len(records)} 条项目记录）")


if __name__ == "__main__":
    main()
