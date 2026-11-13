"""Source-bound lottery supply/demand statistics shared by reports and lookup.

Only anonymous aggregates are persisted. Unknown household counts, shared pools,
unresolved rounds and unreviewed rules never become a numerical chance.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from app.housing.batches import batch_key
from app.housing.models import HousingBatchUnit, HousingLotteryStat, HousingMeta, HousingNotice

SCHEMA = "lottery-competition-v1"


def evidence_version(notice):
    parts = [notice.business_version, notice.attachment_version, batch_key(notice), notice.attachments]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def count_records(records, *, complete=True):
    """records are (identifier, queue, round[, original order])."""
    people, queues, rounds = set(), defaultdict(set), defaultdict(set)
    positions = defaultdict(set)
    ambiguous = False
    for record in records:
        identifier, queue, round_no = record[:3]
        identifier = (identifier or "").strip().upper()
        if not identifier:
            ambiguous = True
            continue
        people.add(identifier)
        queues[queue or "未标注"].add(identifier)
        rounds[str(round_no or 0)].add(identifier)
        positions[identifier].add((queue, str(round_no or 0), record[3] if len(record) > 3 else None))
    queue_counts = {q: len(ids) for q, ids in queues.items()}
    repeated = any(len(values) > 1 for values in positions.values())
    roster_hash=hashlib.sha256(json.dumps(sorted(tuple(r) for r in records),ensure_ascii=False).encode()).hexdigest()
    return {"households": len(people), "records": len(records), "queues": queue_counts,"roster_hash":roster_hash,
            "rounds": {k: len(v) for k, v in rounds.items()},
            "households_verified": complete and not ambiguous,
            "queue_complete": complete and not ambiguous and "未标注" not in queues
                and sum(queue_counts.values()) == len(people),
            "round_ambiguous": repeated, "roster_complete": complete and not ambiguous}


def save_evidence(session, notice, categories):
    key = f"lottery-evidence:{notice.id}"
    record = session.get(HousingMeta, key)
    if record is None:
        record = HousingMeta(key=key)
        session.add(record)
    old = record.value or {}
    value = {"schema": SCHEMA, "source_version": evidence_version(notice), "categories": categories}
    if old.get("categories") != categories and old.get("source_version") != value["source_version"]:
        value["previous"] = old.get("categories", [])
        value["changed_at"] = notice.event_at.isoformat() if notice.event_at else None
    elif old.get("source_version") == value["source_version"]:
        for name in ("previous", "changed_at"):
            if name in old:
                value[name] = old[name]
    record.value = value
    session.flush()


def saved_categories(session, notice):
    record = session.get(HousingMeta, f"lottery-evidence:{notice.id}")
    if record and record.value.get("source_version") == evidence_version(notice):
        return record.value.get("categories", [])
    return None


def categories_from_payload(payload, index):
    batch = payload["batches"][index]
    result = []
    for c, category in enumerate(batch["c"]):
        records = [(no, batch["Q"][m[2]], m[6], m[3]) for no, matches in payload["rows"].items()
                   for m in matches if m[0] == index and m[1] == c]
        counts = count_records(records, complete=not batch.get("stale") and category.get("roster_complete", False))
        counts['round_ambiguous'] = counts['round_ambiguous'] or category.get('rank_scope') == 'multiple-records'
        result.append({"category": category.get("key", ""), "label": category["l"], **counts,
                       "url": batch.get("u", ""), "published_at": batch.get("d", ""),
                       "queue_mode": category.get("queue_mode", "unknown"), "error": category.get("issue", "")})
    return result


def _pool_overlap(keys):
    ranges = []
    for key in keys:
        # Project-specific labels cannot be silently merged with all-project totals.
        value = key.split("|")[-1]
        if not re.fullmatch(r"\d{2,3}(?:\.\d+)?(?:[,+\-]\d{2,3}(?:\.\d+)?)*\+?", value):
            continue
        numbers = re.findall(r"\d+(?:\.\d+)?", value)
        if not numbers:
            continue
        low, high = min(map(float, numbers)), max(map(float, numbers))
        if "+" in value:
            high = float("inf")
        for other, a, b in ranges:
            if "|" in key and "|" in other and key.rsplit("|", 1)[0] != other.rsplit("|", 1)[0]:
                continue
            if other != key and not (high < a or low > b):
                return True
        ranges.append((key, low, high))
    bases = {k.split("|")[-1] for k in keys if "|" not in k}
    return any("|" in k and k.split("|")[-1] in bases for k in keys)


def supply_catalog(session, key, inventory=None):
    catalog = {}
    if key:
        for unit in session.scalars(select(HousingBatchUnit).where(HousingBatchUnit.batch_key == key)):
            source = session.get(HousingNotice, unit.source_notice_id) if unit.source_notice_id else None
            if unit.verified and source and batch_key(source) == key and source.category == "rent_launch":
                catalog[unit.category_key] = {"units": unit.units, "url": unit.source_url or source.canonical_url,
                                              "verified": True}
    if inventory and inventory.get("batch_key") == key and inventory.get("complete"):
        groups, layouts = defaultdict(int), defaultdict(int)
        for project in inventory.get("projects", []):
            for row in project.get("rows", []):
                band = row.get("band", "")
                # area:50 is an exact physical area, not an inferred application band.
                if band and not band.startswith("area:"):
                    groups[band] += row["units"]
                layout = re.sub(r"\s+", "", row.get("layout", ""))
                # Room count is independent of floor area. Only explicit room
                # labels are equivalent; e.g. never translate 一房 into 50㎡.
                match = re.fullmatch(r"([一二两三四五六])房(?:[一二两三四]厅)?", layout)
                if match:
                    layouts[("两" if match[1] == "二" else match[1]) + "房"] += row["units"]
                elif layout in {"小三房", "单身公寓"}:
                    layouts[layout] += row["units"]
        for band, units in groups.items():
            if band in catalog and catalog[band]["units"] != units:
                catalog[band]["verified"] = False
            else:
                catalog[band] = {"units": units, "verified": True,
                                 "url": next((s["url"] for s in inventory.get("sources", [])
                                              if s.get("kind") != "proof"), "")}
        for layout, units in layouts.items():
            if layout not in catalog:
                catalog[layout] = {"units": units, "verified": True,
                    "url": next((s["url"] for s in inventory.get("sources", []) if s.get("kind") != "proof"), "")}
    return catalog


def summarize_category(item, supply, *, shared_pool=False):
    result = dict(item)
    units = supply.get("units") if supply.get("verified") and not shared_pool else None
    result.update(initial_units=units, supply_url=supply.get("url", ""), competition_ratio=None)
    problems = []
    if not item.get("households_verified"):
        problems.append("完整申请家庭数尚未核实")
    if item.get("round_ambiguous"):
        problems.append("存在多轮申请，需明确适用轮次")
    if item.get("category", "").startswith("unknown:"):
        problems.append("档位范围尚未可靠识别")
    if shared_pool:
        problems.append("档位涉及重叠房源池，不能重复分配套数")
    if units is None or units <= 0:
        problems.append("同档初始房源未可靠对应")
    if not problems:
        result["competition_ratio"] = str((Decimal(item["households"]) / Decimal(units)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
    result["missing"] = problems
    return result


def batch_summary(session, notice, inventory=None):
    items = saved_categories(session, notice)
    if items is None:
        items = [{"category": s.category_key, "label": s.label, "households": None,
                  "records": s.total_rows, "queues": s.queue_counts, "queue_complete": False,
                  "households_verified": False, "roster_complete": False, "round_ambiguous": False,
                  "url": s.source_url, "published_at": notice.published_at.isoformat() if notice.published_at else "",
                  "queue_mode": "unknown", "error": s.error or "名单家庭数待独立核实"}
                 for s in session.scalars(select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id))]
    catalog = supply_catalog(session, batch_key(notice), inventory)
    overlap = _pool_overlap([item["category"] for item in items])
    return [summarize_category(item, catalog.get(item["category"], {}), shared_pool=overlap) for item in items]


def attach_competition(session, payload):
    """Annotate a fully read lookup payload with the same report summary contract."""
    from app.housing.inventory import batch_inventory
    for index, batch in enumerate(payload["batches"]):
        notice = session.get(HousingNotice, batch["id"])
        if not notice:
            continue
        items = categories_from_payload(payload, index)
        catalog = supply_catalog(session, batch_key(notice), batch_inventory(session, batch_key(notice)))
        overlap = _pool_overlap([item["category"] for item in items])
        for category, item in zip(batch["c"], items):
            category["competition"] = summarize_category(item, catalog.get(item["category"], {}), shared_pool=overlap)
            category["u"] = category["competition"]["initial_units"] or 0
        batch["competition_schema"] = SCHEMA
    return payload


def competition_text(items):
    from app.housing.lottery import queue_sort_key
    blocks = ["**摇号房源竞争概览（申请人数，非审核合格人数）**"]
    supplies = {i.get("supply_url") for i in items} - {None, ""}
    common_supply = next(iter(supplies)) if len(supplies) == 1 else None
    # Preserve the official attachment/band order; lexicographic Chinese sorting
    # would place 三房 before 两房, and numeric sorting would put 100 before 40.
    for item in items:
        units, households, ratio = item.get("initial_units"), item.get("households"), item.get("competition_ratio")
        known = item.get("households_verified", item.get("complete", False))
        text = "【" + item["label"] + "】\n\n"
        text += (f"初始{units}套" if units is not None else "初始房源待核实") + "｜"
        text += f"申请{households}户" if known and households is not None else f"已解析{item.get('records', households) or 0}条记录，完整家庭数待核实"
        text += "｜" + (f"约{ratio}户申请一套" if ratio is not None else "竞争比暂无法计算")
        if item.get("queue_mode") == "none":
            text += "\n\n本批未划分队列"
        elif item.get("queue_complete", item.get("complete", False)):
            text += "\n\n" + "／".join(q.replace("队列", "队") + str(n) + "户" for q, n in sorted(item["queues"].items(), key=lambda p: queue_sort_key(p[0])))
        else:
            text += "\n\n队列信息未公开或未完整核实"
        if item.get("round_ambiguous"):
            text += "；存在多轮记录，需核对适用轮次"
        if item.get("url"):
            text += f"｜[名单原文]({item['url']})"
        if item.get("supply_url") and not common_supply:
            text += f"｜[房源来源]({item['supply_url']})"
        blocks.append(text)
    dates = sorted({item.get("published_at", "")[:10] for item in items} - {""})
    blocks.append("名单公布：" + "、".join(dates) + "。队列为名单当时分类；初始套数非实时剩余，竞争比不是个人成功概率。")
    if common_supply:
        blocks.append(f"[本批初始房源来源]({common_supply})")
    return "\n\n".join(blocks)


def rule_summary(session, notice, launch, inventory=None):
    """Each adapter is bound to a reviewed document, never to a district name."""
    rule = (inventory or {}).get("rule", {})
    source = launch.canonical_url if launch else notice.canonical_url
    lines = []
    if rule.get("verified") and rule.get("id") == "gm-2026-038-v2":
        lines = ["面积标准：单身居民40㎡左右；二至三人家庭50㎡左右；四人及以上家庭90㎡左右或100㎡及以上。允许自愿认租较小面积，同批不能跨档认租。",
                 "第一队列：规定时点光明户籍＋深圳正常参保且累计3年以上；第二队列：光明企事业单位正常参保＋深圳累计1年以上；第三队列：深圳正常参保＋累计1年以上。社保为养老或医疗，不含少儿医保；户籍、累计社保以2026-09-30规定时点核对。",
                 "100㎡及以上：同队列内3个及以上未成年子女、5人及以上且2个未成年子女、4人且2个未成年子女家庭同等优先，不重复叠加。"]
        if notice.id and "首轮资格核查" in (notice.body_text or ""):
            lines.append("首轮审核：90㎡、100㎡及以上三个队列全部纳入；40㎡、50㎡先审核第一、第二队列，第三队列待首轮选房后仍有房源，再依序启动后续核查、选房。")
    elif launch and "深圳市宝安区住房和建设局" in launch.title and "住保租〔2026〕023号" in launch.title and all(
        anchor in re.sub(r"\s+", "", launch.body_text or "") for anchor in (
            "单身及以上家庭可申请一房户型房源", "三人及以上家庭可申请两房户型房源", "无障碍房源优先面向残疾人家庭")):
        lines = ["本批户型条件：一房单身及以上；两房三人及以上；三房四人及以上。不得跨户型认租，只能在已申报项目中选一套。",
                 "第一队列：规定时点宝安户籍＋深圳正常参保累计3年以上；第二队列：宝安注册单位任职＋宝安累计社保1年以上；第三队列：深圳正常参保累计1年以上。户籍以2026-08-10 18:00前、累计社保以07-31为节点，其他资格以08-20核查。",
                 "一房含7套无障碍住房，优先面向残疾人家庭、全部申请成员均60周岁以上家庭，以及申请人为60周岁以上的单亲家庭；特殊房源与普通房源不能直接等同。",
                 "审核按各队列一定数量进行，具体范围见后续核查、选房公告；公开摇号名单未标注资格队列，不能由编号推断。"]
    else:
        # Keep complete short clauses including negations/exceptions. Long clauses
        # require review rather than a keyword-derived eligibility rule.
        for entry in (inventory or {}).get("evidence", []):
            if "配租面积" in entry.get("heading", ""):
                text = re.sub(r"\s+", " ", entry.get("text", "")).strip()
                if 0 < len(text) <= 240:
                    lines.append("本批面积条件：" + text)
        if not lines:
            lines.append("本批面积／家庭人数、队列条件、优先规则及审核范围尚未完成复核，请核对本批官方原文；不套用其他批次。")
    if source:
        lines.append(f"[本批条件与材料]({source})｜[摇号及审核安排]({notice.canonical_url})")
    return lines
