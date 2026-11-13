"""Verified query metadata; merge mirror publications, retain distinct rounds."""
from __future__ import annotations

import io
import re
from datetime import timedelta
from collections import defaultdict
from bisect import bisect_right

from sqlalchemy import select, func

from app.housing.batches import batch_key
from app.housing.models import HousingNotice, utcnow


def event_groups(notices):
    groups = defaultdict(list)
    for notice in notices:
        attachments = []
        for item in notice.attachments or []:
            url = item.get("url", "")
            file_id = re.search(r"/attachment/\d+/\d+/(\d+)/", url)
            # Official mirrored links keep the attachment file identity even when
            # the article ID / URL prefix changes. Unknown formats remain separate.
            attachments.append(file_id[1] if file_id else url)
        unresolved = ("unresolved", notice.region_code, re.sub(r"\s+", "", notice.title or "")) if attachments else f"unresolved:{notice.id}"
        key = (batch_key(notice) or unresolved, notice.category,
               notice.published_at.date().isoformat() if notice.published_at else f"unknown:{notice.id}",
               tuple(sorted(attachments)) or (str(notice.id),))
        groups[key].append(notice)
    return [sorted(items, key=lambda n: n.id) for items in groups.values()]


def merge_mirror_payload(session, payload):
    """Merge proven mirror events only when their anonymous row signatures agree."""
    notices = [session.get(HousingNotice, b['id']) for b in payload['batches']]
    representatives = {n.id: g[0].id for g in event_groups([n for n in notices if n]) for n in g}
    signatures = defaultdict(list)
    for no, matches in payload['rows'].items():
        for m in matches:
            b = payload['batches'][m[0]]
            signatures[m[0]].append((no, b['c'][m[1]].get('key'), b['Q'][m[2]], m[3], m[6]))
    remap, selected, unique = {}, {}, []
    for old, batch in enumerate(payload['batches']):
        representative = representatives.get(batch['id'], batch['id'])
        prior = selected.get(representative)
        if prior is not None and sorted(signatures[old]) == sorted(signatures[prior]):
            target = unique[remap[prior]]
            target['sources'] = list(dict.fromkeys(target.get('sources', []) + batch.get('sources', [])))
            target['source_ids'] = sorted(set(target.get('source_ids', [target['id']]) + batch.get('source_ids', [batch['id']])))
            continue
        selected[representative] = old
        remap[old] = len(unique)
        unique.append(batch)
    rows = {}
    for no, matches in payload['rows'].items():
        records = [[remap[m[0]], *m[1:]] for m in matches if m[0] in remap]
        if records:
            rows[no] = records
    payload.update(batches=unique, rows=rows)
    return normalize_round_ranks(payload)


def selected_events(session, district="", days=900, max_batches=6):
    """Use the same event window and mirror grouping for builds and change checks."""
    seen_at = func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)
    statement = select(HousingNotice).where(HousingNotice.category == "lottery_result",
        HousingNotice.is_target.is_(True), seen_at >= utcnow() - timedelta(days=days))
    if district: statement = statement.where(HousingNotice.region_code.like(f"%{district}%"))
    return event_groups(list(session.scalars(statement.order_by(seen_at.desc(), HousingNotice.id))))[:max_batches]


def attach_inventory(batch, payload):
    if not payload:
        batch["opportunity"] = {"complete": False, "rule": {"verified": False}, "projects": []}
        return
    batch["opportunity"] = {"complete": payload["complete"], "rule": payload["rule"],
                            "projects": [{"id": p.get("id", ""), "name": p["name"], "address": p["address"],
                                          "rows": p["rows"]} for p in payload["projects"] if p["rows"]],
                            "version": payload["version"]}


def normalize_round_ranks(payload):
    groups = defaultdict(list)
    for batch in payload['batches']:batch['R']={}
    for identifier,matches in payload['rows'].items():
        for m in matches:
            if payload['batches'][m[0]].get('review_only'):continue
            groups[(m[0],m[1],m[2])].append((identifier,m))
    for (b,c,q), records in groups.items():
        by_round=defaultdict(list)
        for identifier,m in records:by_round[m[6] if len(m)>6 else 0].append(m)
        repeated_people=len({no for no,_ in records})<len(records)
        all_orders=[m[3] for _,m in records]
        # A public lottery may generate 1000 sequential numbers per machine
        # round. Unique applicants with globally increasing numbers still share
        # ONE queue ranking; those machine rounds must never reset the rank.
        independent=repeated_people and len(set(all_orders))<len(all_orders) and len(by_round)>1
        partitions=by_round.items() if independent else [(None,[m for _,m in records])]
        for round_no,matches in partitions:
            orders=sorted(m[3] for m in matches)
            for m in matches:m[4]=bisect_right(orders,m[3])
            key=f'{c}|{q}'+(f'|{round_no}' if independent else '')
            payload['batches'][b].setdefault('R',{})[key]=orders
        if repeated_people:payload['batches'][b]['c'][c]['rank_scope']='multiple-records'
    return payload


def parse_final_tables(tables):
    """Only explicitly named FINAL selection-rank columns; never infer from 序号."""
    records = {}
    conflicts = set()
    for table in tables:
        for start, header in enumerate(table[:10]):
            names = [re.sub(r"\s+", "", str(c or "")) for c in header]
            no = next((i for i,s in enumerate(names) if s in {"认租编号", "认租申请编号", "申请编号"}), None)
            rank = next((i for i,s in enumerate(names) if s in {"选房排位", "选房排序", "选房顺序号"}), None)
            if no is None or rank is None: continue
            for row in table[start + 1:]:
                if len(row) <= max(no, rank): continue
                identifier = re.sub(r"\s+", "", str(row[no] or "")).upper()
                number = re.sub(r"\s+", "", str(row[rank] or ""))
                if not re.fullmatch(r"[A-Z]{1,5}\d[A-Z0-9]{5,}", identifier) or not number.isdigit() or int(number) < 1: continue
                if identifier in records and records[identifier] != int(number): conflicts.add(identifier)
                records[identifier] = int(number)
            break
    return {key:value for key,value in records.items() if key not in conflicts}


async def final_selection(session, client, key):
    from app.housing.reporting import notice_stage
    candidates = [n for n in session.scalars(select(HousingNotice).where(HousingNotice.batch_key_v2 == key))
                  if notice_stage(n) == "selection" and not re.search(r"预告|预计|选房结果", n.title or "")]
    for notice in sorted(candidates, key=lambda n: (n.published_at or n.first_seen_at, n.id), reverse=True):
        attachments = [a for a in notice.attachments or [] if re.search(r"选房.*(?:名单|排位|排序)|(?:名单|排位).*选房", a.get("name", ""))]
        if not attachments: continue
        result, errors = {}, []
        for item in attachments[:8]:
            try:
                data = await client.get_bytes(item["url"], referer=notice.canonical_url)
                kind = item.get("kind", "")
                if kind == "pdf":
                    from app.housing.qualification import parse_pdf
                    fast=parse_pdf(data,'qualified',item['url'],item.get('name',''))
                    if fast and all(r.get('selection_rank') for r in fast):
                        parsed={r['apply_no']:r['selection_rank'] for r in fast}
                        if len(parsed)!=len(fast):raise ValueError('正式名单存在重复编号，需核对轮次')
                        for identifier,rank in parsed.items():
                            if identifier in result:raise ValueError('正式名单不同附件编号冲突')
                            row=next(r for r in fast if r['apply_no']==identifier)
                            result[identifier]={'rank':rank,'url':item['url'],'notice_id':notice.id,
                                'published_at':(notice.published_at or notice.first_seen_at).isoformat(),
                                'queue':row['queue'],'band':row['band']}
                        continue
                    import pdfplumber
                    with pdfplumber.open(io.BytesIO(data)) as pdf:
                        if len(pdf.pages) > 600: raise ValueError("最终名单超过安全页数")
                        tables = [t for page in pdf.pages for t in page.extract_tables()]
                elif kind in {"xls", "xlsx", "et"}:
                    from app.housing.lottery import _read_table
                    tables = [_read_table(data, "xls" if kind == "et" else kind)]
                else: raise ValueError("尚不支持该最终排序附件类型")
                parsed = parse_final_tables(tables)
                if not parsed: raise ValueError("未找到明确的选房排序列")
                for identifier, rank in parsed.items():
                    if identifier in result: raise ValueError("同编号存在多份最终排序，需核对轮次")
                    result[identifier] = {"rank": rank, "url": item["url"], "notice_id": notice.id,
                                          "published_at":(notice.published_at or notice.first_seen_at).isoformat()}
            except Exception as exc: errors.append(type(exc).__name__)
        if len(attachments) > 8: errors.append("附件超过安全上限")
        return ({} if errors else result), {"url": notice.canonical_url, "verified": bool(result) and not errors,
                                            "issue": "最终排序未完整核实，直接查看官方选房公告" if errors else ""}
    return {}, None
