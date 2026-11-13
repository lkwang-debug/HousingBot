"""Audit every collected lottery event, optionally import verified anonymous counts.

No downloads, notice edits, delivery events or publication receipts are created.
--apply-evidence writes only version-bound lottery-evidence HousingMeta records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.config import get_settings
from app.housing.batches import batch_key
from app.housing.competition import (batch_summary, categories_from_payload, save_evidence,
                                     rule_summary, attach_competition)
from app.housing.inventory import batch_inventory
from app.housing.lookup import source_version, build_directory
from app.housing.lookup_data import event_groups
from app.housing.models import HousingNotice
from app.housing.opportunity import load_payload
from app.housing.reporting import REGIONS, build_snapshot


def import_lookup_evidence(session, directory):
    directory = build_directory(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    imported, skipped = [], []
    for name, info in manifest.get("pages", {}).items():
        if name == "index.html":
            continue
        path = directory / name
        if Path(name).name != name or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
            skipped.append({"page": name, "reason": "文件与清单不符"})
            continue
        payload = load_payload(path)
        for index, batch in enumerate(payload["batches"]):
            notice = session.get(HousingNotice, batch["id"])
            if not notice or batch.get("stale") or batch.get("version") != source_version(session, notice):
                skipped.append({"notice_id": batch["id"], "reason": "旧名单版本与当前来源不符"})
                continue
            items = categories_from_payload(payload, index)
            if not items or any(not item["households_verified"] for item in items):
                skipped.append({"notice_id": notice.id, "reason": "完整家庭数未核实"})
                continue
            from app.housing.lookup_data import event_groups
            aliases = list(session.scalars(select(HousingNotice).where(HousingNotice.id.in_(batch.get("source_ids", [notice.id])))))
            # A source_ids list is not proof of mirror identity by itself.
            mirrors = next((g for g in event_groups(aliases) if any(n.id == notice.id for n in g)), [notice])
            for alias in mirrors:
                save_evidence(session, alias, items)
                imported.append(alias.id)
    return {"imported_notice_ids": sorted(set(imported)), "skipped": skipped}


def coverage(session, now=None):
    now = now or datetime.now()
    snapshot = build_snapshot(session, "evening", now=now)
    cards = {i: c for c in snapshot.cards for i in c.notice_ids}
    groups = event_groups(list(session.scalars(select(HousingNotice).where(
        HousingNotice.is_target.is_(True), HousingNotice.category == "lottery_result").order_by(HousingNotice.id))))
    events = []
    for group in groups:
        notice = group[0]
        key = batch_key(notice)
        inv = batch_inventory(session, key)
        c = next((cards[n.id] for n in group if n.id in cards), None)
        items = batch_summary(session, notice, inv)
        missing = list(dict.fromkeys(m for i in items for m in i.get("missing", [])))
        if any(not i.get("queue_complete") and i.get("queue_mode") != "none" for i in items):
            missing.append("队列信息未公开或未完整核实")
        if not items:
            missing.append("名单尚未完整解析")
        if not key:
            missing.append("配租启动公告尚未可靠关联")
        verified = bool(inv and inv.get("rule", {}).get("verified"))
        if not verified:
            missing.append("个性演算所需批次规则尚未复核")
        events.append({"notice_id": notice.id, "aliases": [n.id for n in group],
            "code": c.code if c else f"N{notice.id}", "region": notice.region_code,
            "title": notice.title, "url": notice.canonical_url, "batch_key": key,
            "in_transit": bool(c and c.in_transit), "in_current_report": bool(c),
            "rule_verified": verified, "categories": items, "missing": missing})
    regions = [{"region": r, "events": sum(e["region"] == r for e in events),
                "tracked_events": sum(e["region"] == r and e["in_transit"] for e in events),
                "status": "已盘点" if any(e["region"] == r for e in events) else "当前已采集数据中无摇号公告，不代表本区没有房源"}
               for r in ("深圳市", *REGIONS)]
    return {"generated_at": now.isoformat(), "regions": regions, "events": events,
            "summary": {"events": len(events), "tracked_events": sum(e["in_transit"] for e in events),
                        "categories_with_ratio": sum(i.get("competition_ratio") is not None for e in events for i in e["categories"])}}


def write_coverage(result, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "全市摇号覆盖清单.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 全市摇号竞争统计覆盖清单", "", "无已采集摇号公告不代表当地没有房源。历史窗口外及未关联公告也单独盘点，不强行放入当前报表。", "",
             "|区域|已采集摇号事件|在途事件|状态|", "|---|---:|---:|---|"]
    lines += [f"|{r['region']}|{r['events']}|{r['tracked_events']}|{r['status']}|" for r in result["regions"]]
    for e in result["events"]:
        lines += ["", f"## {e['region']} {e['code']}／公告{e['notice_id']}", e["title"],
                  f"在途：{'是' if e['in_transit'] else '否'}；个性演算规则：{'已复核' if e['rule_verified'] else '待复核'}", e["url"]]
        for i in e["categories"]:
            lines.append(f"- {i['label']}：申请{i.get('households') if i.get('households_verified') else '待核实'}户；初始{i.get('initial_units') if i.get('initial_units') is not None else '待核实'}套；竞争比{i.get('competition_ratio') or '待核实'}")
        lines.append("缺项：" + ("；".join(e["missing"]) or "无统计缺项；正式审核及排序按后续公告更新"))
    (output / "全市摇号覆盖清单.md").write_text("\n\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookup", type=Path, default=get_settings().project_root / "lookup")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply-evidence", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        imported = import_lookup_evidence(session, args.lookup)
        result = coverage(session)
        result["evidence_import"] = imported
        result["mode"] = "applied_anonymous_evidence_only" if args.apply_evidence else "preview_rolled_back"
        write_coverage(result, args.output)
        session.commit() if args.apply_evidence else session.rollback()
    print(json.dumps({"summary": result["summary"], "imported": len(imported["imported_notice_ids"]),
                      "skipped": len(imported["skipped"]), "mode": result["mode"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
