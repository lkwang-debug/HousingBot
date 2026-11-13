"""上车预测：用公开的房源数和摇号人数算概率，而不是拍脑袋。

深圳配租的规则是「先队列、后顺序号、房源选完即止」：
第一队列全部走完才轮到第二队列，所以同一个顺序号，在不同队列里命运完全不同。
把每档户型的房源数（《房源概况》）和每档认租人数（摇号名单）对上，
就能算出每一档、每个队列大概有多少机会。

这不是"保证"，也不该被说成保证：
- 前面的人放弃了，后面的人才能补上，公告不公布放弃率；
- 资格核查还会刷掉一部分人；
- 房源分项目，具体到某个项目、某种朝向，差异可能很大。
所以对外只说"按公开数据估算"，并给出依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.housing.batches import batch_key
from app.housing.lottery import QUEUE_ORDER, find_applicant
from app.housing.models import HousingBatchUnit, HousingNotice, utcnow

QUEUE_RULE_RE = re.compile(r"第([一二三四五])队列[：:]\s*([^。；\n]{4,120})")
# "光明区住房和建设局关于……的通告" 这种机构抬头对买家没信息量，去掉更短更好读。
NOTICE_LEAD_RE = re.compile(r"^.{0,15}?(?:住房和建设局|住房保障署|住建局|住房保障中心)关于")
NOTICE_TAIL_RE = re.compile(r"(?:的)?(?:通告|通知|公告)$")



@dataclass
class QueueOdds:
    queue: str
    applicants: int
    units_left: int
    chance: float


@dataclass
class CategoryForecast:
    key: str
    label: str
    units: int | None
    applicants: int
    queues: list[QueueOdds] = field(default_factory=list)

    @property
    def overall_chance(self) -> float | None:
        if not self.units or not self.applicants:
            return None
        return min(1.0, self.units / self.applicants)


def _ordered_queues(counts: dict[str, int]) -> list[str]:
    from app.housing.lottery import queue_sort_key
    return sorted((queue for queue in counts if counts[queue]), key=queue_sort_key)


def queue_odds(units: int | None, counts: dict[str, int]) -> list[QueueOdds]:
    """按队列优先级把房源依次消耗掉，算出每个队列的平均机会。"""
    if not units:
        return []
    remaining = int(units)
    result: list[QueueOdds] = []
    for queue in _ordered_queues(counts):
        applicants = int(counts.get(queue) or 0)
        if applicants <= 0:
            continue
        chance = 1.0 if applicants <= remaining else remaining / applicants
        result.append(
            QueueOdds(
                queue=queue,
                applicants=applicants,
                units_left=remaining,
                chance=round(chance, 3),
            )
        )
        remaining = max(0, remaining - applicants)
    return result


def overall_chance(units: int | None, applicants: int | None) -> float | None:
    if not units or not applicants:
        return None
    return min(1.0, units / applicants)


def verdict(chance: float) -> str:
    return "仅为名额供需比例，不是个人成功概率"


def queue_rank(orders: list[int], order: int) -> int:
    """同一队列内按顺序号排名（顺序号越小越靠前）。"""
    from bisect import bisect_right
    return bisect_right(sorted(orders), order)


def units_left_before(counts: dict[str, int], units: int | None, queue: str) -> int | None:
    if not units:
        return None
    remaining = int(units)
    for name in _ordered_queues(counts):
        if name == queue:
            return remaining
        remaining = max(0, remaining - int(counts.get(name) or 0))
    return remaining


def queue_rules(body: str) -> dict[str, str]:
    """从配租通告正文里抄出各队列的条件，推送时直接告诉买家"你算哪一队"。"""
    rules: dict[str, str] = {}
    for match in QUEUE_RULE_RE.finditer(body or ""):
        queue = f"第{match.group(1)}队列"
        text = re.sub(r"\s+", " ", match.group(2)).strip(" ，,。")
        if queue not in rules:
            rules[queue] = text
    return rules


def format_category(category: "CategoryForecast", limit_queues: int = 3) -> list[str]:
    lines: list[str] = []
    if category.units:
        lines.append(f"{category.label}：房源 {category.units} 套 / 认租 {category.applicants} 户")
    else:
        lines.append(f"{category.label}：认租 {category.applicants} 户（房源数未公布）")
    for item in category.queues[:limit_queues]:
        lines.append(
            f"  · {item.queue} {item.applicants} 户 → 轮到时剩 {item.units_left} 套，"
            f"供需比例 {_percent(item.chance)}"
        )
    return lines


def format_applicant(
    category_label: str,
    units: int | None,
    counts: dict[str, int],
    queue: str,
    order: int,
    rank: int,
    total: int,
) -> list[str]:
    """给单个认租家庭看的结论。"""
    lines = [f"你认租的是：{category_label}", f"队列：{queue} · 摇号顺序号 {order}（本档共 {total} 户）"]
    if not units:
        lines.append("本档房源数或批次/名单尚未核实，无法估算；先看官方附件。")
        return lines
    lines.append(f"本档房源：{units} 套")
    remaining = units_left_before(counts, units, queue)
    for item in queue_odds(units, counts):
        lines.append(f"  · {item.queue} {item.applicants} 户 → 剩 {item.units_left} 套")
    lines.append(f"你在本队列排第 {rank} 位")
    if remaining is None:
        return lines
    if remaining <= 0:
        lines.append("假设前序家庭均通过资格核查且不放弃，前序队列已占用全部名额")
    elif rank <= remaining:
        lines.append(f"在无淘汰、无放弃的假设下位于名额范围内（轮到时约剩 {remaining - rank + 1} 套左右）")
    else:
        over = rank - remaining
        if over <= max(3, int(remaining * 0.3)):
            lines.append(f"按当前名单估算，前面至少约 {over} 户退出才可能轮到，实际以选房通知为准")
        else:
            lines.append(f"按当前名单估算，前面至少约 {over} 户退出才可能轮到，实际以选房通知为准")
    lines.append("推算依据：公开房源数 + 摇号名单，不含资格核查淘汰和放弃率，仅供参考")
    return lines


def predict_whatif(units, counts, queue, order=None, *, rank=None, orders=None):
    """rank is explicitly hypothetical; order needs a real verified distribution."""
    if rank is not None and order is not None:
        raise ValueError("--rank 和 --order 不能同时指定")
    if rank is None:
        if orders is None or order not in orders or orders.count(order) != 1:
            return {"status": "unverified", "rank": None, "order": order,
                    "lines": ["无法在所选名单中确认该队列和顺序号；不生成虚拟排位"]}
        rank = queue_rank(orders, order)
    if rank < 1 or not counts.get(queue) or rank > counts[queue]:
        raise ValueError("假设排位必须在该队列人数范围内")
    remaining = units_left_before(counts, units, queue)
    return {"status": "ok", "units": units, "queue_counts": counts, "queue": queue,
            "order": order, "rank": rank, "units_left": remaining,
            "supply_ratio": overall_chance(units, sum(counts.values())),
            "lines": format_applicant("（指定档位，排位演算）", units, counts, queue,
                                      order if order is not None else "未指定（假设排位）", rank, sum(counts.values()))}


async def predict_order(session, client, notice, category, queue, order):
    from app.housing.lottery import read_attachment_rows, category_key, _queue_name, queue_prefix, queue_hint
    from app.housing.ingestion import row_issues
    rows = []
    for label, records in await read_attachment_rows(client, notice):
        if category_key(label) != category:
            continue
        for row in records:
            row.queue = _queue_name(row.queue, queue_prefix(label), queue_hint(label))
        rows.extend(records)
    if not rows or row_issues(rows) or not batch_key(notice):
        return {"status": "unverified", "lines": ["名单、队列或批次未核实，无法确认排位"]}
    from collections import Counter
    unit = session.scalar(select(HousingBatchUnit).where(HousingBatchUnit.batch_key == batch_key(notice),
        HousingBatchUnit.category_key == category, HousingBatchUnit.verified.is_(True)))
    return predict_whatif(unit.units if unit else None, dict(Counter(row.queue for row in rows)),
        queue, order, orders=[row.order for row in rows if row.queue == queue])


async def predict_by_apply_no(
    session: Session,
    client,
    apply_no: str,
    *,
    notice_id: int | None = None,
    days: int = 240,
) -> dict:
    """按认租编号查队列、顺序号、队列内排位，并给出能不能上车的判断。

    名单只在内存里过一遍，不写库——这是有意的，见 lottery.py 顶部说明。
    """
    seen_at = func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)
    statement = select(HousingNotice).where(HousingNotice.category == "lottery_result")
    if notice_id is not None:
        statement = statement.where(HousingNotice.id == notice_id)
    else:
        statement = statement.where(seen_at >= utcnow() - timedelta(days=days))
    notices = list(session.scalars(statement.order_by(seen_at.desc()).limit(30)).all())

    incomplete = []
    for notice in notices:
        try:
            applicant = await find_applicant(client, notice, apply_no)
        except ValueError:
            incomplete.append(notice.id)
            continue
        if applicant is None:
            continue
        key = batch_key(notice) or ""
        unit = session.scalar(
            select(HousingBatchUnit).where(
                HousingBatchUnit.batch_key == key,
                HousingBatchUnit.category_key == applicant.category_key,
            )
        )
        from app.housing.models import HousingLotteryStat
        stat = session.scalar(select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id,
            HousingLotteryStat.category_key == applicant.category_key))
        units = unit.units if unit and unit.verified and not applicant.issues and key and stat and stat.complete else None
        rank = queue_rank(applicant.queue_orders, applicant.order)
        lines = [f"批次：{notice_label(notice)}"]
        lines += format_applicant(
            applicant.category_label,
            units,
            applicant.queue_counts,
            applicant.queue,
            applicant.order,
            rank,
            applicant.total,
        )
        if applicant.issues:
            lines.extend(applicant.issues)
        lines.append(f"原文：{notice.canonical_url}")
        return {
            "status": "ok",
            "notice_id": notice.id,
            "region": notice.region_code,
            "category": applicant.category_label,
            "queue": applicant.queue,
            "order": applicant.order,
            "rank": rank,
            "total": applicant.total,
            "units": units,
            "queue_counts": applicant.queue_counts,
            "issues": applicant.issues,
            "message": "\n".join(lines),
        }
    return {"status": "unverified" if incomplete else "not_found", "apply_no": apply_no,
            "checked_notices": len(notices), "incomplete_notices": incomplete}


def notice_label(notice: HousingNotice, *, with_region: bool = True) -> str:
    """去掉机构抬头和"的通告"，保留买家需要的信息。

    with_region=False 用于群推送标题：前面已经有"光明区 · 管家版"，再加一次是噪音。
    """
    title = NOTICE_LEAD_RE.sub("", (notice.title or "").strip())
    title = NOTICE_TAIL_RE.sub("", title).strip()
    title = re.sub(r"^关于\s*", "", title).strip()
    region = notice.region_code or ""
    # 标题正文里已经提到这个区就别再补，「坪山区 面向坪山区户籍…」读着像两个人说的。
    if with_region and region and region not in title:
        # 后面接年份就连写（「南山区2026年度…」），否则空格分隔。
        title = f"{region}{title}" if title[:1].isdigit() else f"{region} {title}"
    return title[:70]


def _percent(value: float) -> str:
    value = max(0.0, min(1.0, value))
    if value >= 1:
        return "100%"
    if value >= 0.1:
        return f"{value * 100:.0f}%"
    return f"{value * 100:.1f}%"
