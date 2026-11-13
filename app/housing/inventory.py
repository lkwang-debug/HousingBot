"""Official project inventory, fees and rule evidence. Unknown values stay unknown."""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import timedelta

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.housing.batches import batch_key
from app.housing.models import HousingMeta, HousingNotice, utcnow

SCHEMA = 1
FEE_LABELS = {"market": "市场参考租金", "rent": "执行租金", "maintenance": "专项房屋维修基金",
              "property": "物业管理费", "parking": "停车费"}


def clean(value):
    return re.sub(r"\s+", "", str(value or "")).replace("\u3000", "")


def html_tables(html):
    """Expand merged cells without forwarding unrelated columns / previous tables."""
    for table in BeautifulSoup(html or "", "html.parser").find_all("table"):
        grid, spans = [], {}
        for tr in table.find_all("tr"):
            row, col = [], 0
            def advance():
                nonlocal col
                while col in spans:
                    value, left = spans[col]
                    row.append(value)
                    if left == 1: del spans[col]
                    else: spans[col] = (value, left - 1)
                    col += 1
            for cell in tr.find_all(["td", "th"], recursive=False):
                advance()
                value = cell.get_text(" ", strip=True)
                colspan, rowspan = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
                for _ in range(colspan):
                    row.append(value)
                    if rowspan > 1: spans[col] = (value, rowspan - 1)
                    col += 1
            advance()
            if row: grid.append(row)
        if grid: yield grid


def parse_table(table):
    """Header-driven parsing, including PDF / Excel merged project columns."""
    projects = {}
    for start in range(min(8, len(table))):
        width = max((len(r) for r in table[start:start + 2]), default=0)
        head = [clean(table[start][i]) if i < len(table[start]) else "" for i in range(width)]
        def column(terms):
            return next((i for i, h in enumerate(head) if any(t in h for t in terms)), None)
        name = column(("小区名称", "项目名称", "房源项目"))
        if name is None: continue
        address = column(("项目地址", "项目所在位置", "小区地址", "项目位置"))
        layout, area = column(("户型", "房型")), column(("建筑面积", "面积"))
        units = column(("套数", "住房套数", "供应数量", "房源数量"))
        # Some official headers use two rows for market vs discounted rent.
        body_at = start + 1
        subhead = []
        if body_at < len(table) and any(any(t in clean(v) for t in ("市场参考租金", "按原政策", "352号政府令", "按352号令", "特困", "低保")) for v in table[body_at]):
            lower = table[body_at]
            subhead = [clean(v) for v in lower]
            head = [head[i] + clean(lower[i] if i < len(lower) else "") for i in range(width)]
            body_at += 1
        fees = {}
        rents = []
        for i, h in enumerate(head):
            if "市场参考" in h: fees["market"] = i
            elif any(t in h for t in ("维修基金", "维修资金")): fees["maintenance"] = i
            elif "物业" in h: fees["property"] = i
            elif "停车" in h: fees["parking"] = i
            elif any(t in h for t in ("租金", "租赁价格", "租赁单价")): rents.append(i)
        if rents: fees["rent"] = rents if len(rents) > 1 else rents[0]
        if units is None and not fees: continue
        previous_name, previous_address, previous_layout = "", "", ""
        for cells in table[body_at:]:
            def take(i): return clean(cells[i]) if i is not None and i < len(cells) else ""
            if any(clean(v) in {"合计", "小计", "总计"} for v in cells[:name + 1]): break
            current = take(name)
            if current in {"合计", "小计", "总计"}: break
            if current:
                if any(t in current for t in ("名称", "市场参考", "备注", "保障性租赁住房租金")): continue
                if units is None and current.startswith(("（", "(")) and previous_name in projects:
                    old = projects.pop(previous_name)
                    current = previous_name + current
                    old["name"] = current
                    projects[current] = old
                previous_name = current
                previous_address = take(address)
                previous_layout = ""
            if not previous_name: continue
            project = projects.setdefault(previous_name, {"name": previous_name, "address": previous_address,
                                                         "rows": [], "fees": {}})
            if take(address): project["address"] = previous_address = take(address)
            values = {}
            for key, i in fees.items():
                if isinstance(i, list):
                    options = [{"label":subhead[j] if j<len(subhead) else head[j],"value":take(j)+"元/㎡·月"} for j in i if take(j)]
                    if options: values[key] = {"options": options}
                    continue
                value = take(i)
                if not value or value in {"-", "—", "/"}: continue
                unit = "元/月" if key == "parking" else "元/㎡·月"
                # A column explicitly billed by household / suite must keep that unit.
                if "元/套" in head[i] or "元／套" in head[i]: unit = "元/套·月"
                values[key] = value + unit if re.fullmatch(r"\d+(?:\.\d+)?", value) else value
            if units is not None:
                amount = re.match(r"^(\d+)(?:\.0)?(?:[（(].*)?$", take(units))
                actual_area = take(area)
                actual_layout = take(layout) or previous_layout
                if layout == area:
                    combined = re.fullmatch(r"([^（(]+)[（(](?:建筑面积)?([^）)]+)[）)]", actual_area)
                    if combined: actual_layout, actual_area = combined[1], combined[2]
                if amount and actual_area and actual_layout and "合计" not in actual_layout:
                    previous_layout = actual_layout
                    entry = {"layout": actual_layout, "area": actual_area,
                             "units": int(amount.group(1)), "band": "", "fees": values}
                    if take(units) != amount.group(1): entry["notes"] = take(units)
                    project["rows"].append(entry)
            elif values:
                project["fees"].update(values)
        break
    return list(projects.values())


def prose_projects(body):
    """Explicit project paragraphs, not guessed names / room-count-to-area conversions."""
    start = re.search(r"[一二三四五六七八九十]+、[^\n]*(?:房源概况|房源情况|房源基本情况)", body or "")
    if not start: return []
    section = body[start.end():]
    section = re.split(r"\n\s*[一二三四五六七八九十]+、", section, maxsplit=1)[0]
    basic=re.search(r'[（(]一[）)]\s*项目基本情况',section)
    if basic:
        basic_text=re.split(r'\n\s*(?:注[：:]|[（(]二[）)])',section[basic.end():],maxsplit=1)[0]
        numbered=list(re.finditer(r'(?:^|\n)\s*\d+[.．、]\s*([^，,\n]{2,45})[，,]',basic_text))
        if numbered:
            projects=[]
            area=r'(?:约)?\d+(?:\.\d+)?(?:[-—–～~至]\d+(?:\.\d+)?)?(?:平方米|㎡)?(?:左右)?'
            layout=r'(?:单身公寓|单房|[一二两三四五六]房(?:[一二两三四]厅)?)'
            for i,heading in enumerate(numbered):
                content=clean(basic_text[heading.end():numbered[i+1].start() if i+1<len(numbered) else len(basic_text)])
                address=re.search(r'位于([^，。]+)',content)
                total=re.search(r'本次配租(\d+)套',content)
                if not address or not total:continue
                rows=[{'layout':m[2],'area':m[1].replace('平方米','㎡'),'units':int(m[3]),'band':'','fees':{}}
                      for m in re.finditer(rf'(?:建筑面积)({area})的?({layout})(\d+)套',content)]
                if not rows:
                    uniform=re.search(rf'均为建筑面积({area})的?({layout})',content)
                    if uniform:rows=[{'layout':uniform[2],'area':uniform[1].replace('平方米','㎡'),'units':int(total[1]),'band':'','fees':{}}]
                projects.append({'name':heading[1].strip(),'address':address[1],'rows':rows,'fees':{},'declared_total':int(total[1])})
            if projects:return projects
    pieces = re.split(r"(?:^|\n)\s*[（(][一二三四五六七八九十]+[）)]\s*([^\n]+)\n", section)
    result = []
    layout = r"(?:单身公寓|单房|[一二两三四五六]房(?:[一二两三四]厅)?)"
    area = r"(?:约)?\d+(?:\.\d+)?(?:平方米|㎡)?(?:至\d+(?:\.\d+)?(?:平方米|㎡)?)?(?:左右)?"
    for i in range(1, len(pieces) - 1, 2):
        heading, text = clean(pieces[i]), re.sub(r"\s+", "", pieces[i + 1])
        name = re.sub(r"[（(]\d+套[）)]$", "", heading)
        address = re.search(r"(?:项目)?位于([^，。]+)", text)
        total = re.search(r"本次供应(?:房源)?(\d+)套", text)
        rows = []
        pattern_a = rf"(?:建筑面积)?({area})的?({layout})(\d+)套"
        pattern_b = rf"({layout})[（(]建筑面积({area})[）)](\d+)套"
        for match in re.finditer(pattern_a, text):
            rows.append({"layout": match[2], "area": match[1].replace("平方米", "㎡"), "units": int(match[3]), "band": "", "fees": {}})
        for match in re.finditer(pattern_b, text):
            rows.append({"layout": match[1], "area": match[2].replace("平方米", "㎡"), "units": int(match[3]), "band": "", "fees": {}})
        if not rows and total:
            uniform = re.search(rf"均为建筑面积({area})的?({layout})", text)
            other = re.search(rf"户型均为({layout})[（(]建筑面积({area})[）)]", text)
            if total[1] == "1":
                uniform = uniform or re.search(rf"为建筑面积({area})的?({layout})", text)
                other = other or re.search(rf"户型为({layout})[（(]建筑面积({area})[）)]", text)
            if uniform or other:
                a, l = (uniform[1], uniform[2]) if uniform else (other[2], other[1])
                rows.append({"layout": l, "area": a.replace("平方米", "㎡"), "units": int(total[1]), "band": "", "fees": {}})
        if name and (rows or address):
            result.append({"name": name, "address": address[1] if address else "", "rows": rows, "fees": {}})
    return result


def pdf_tables(data):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) > 100: raise ValueError("房源附件超过100页，未截断解析")
        return [table for page in pdf.pages for table in page.extract_tables()]


def rule_evidence(body):
    """Preserve whole sections and exceptions, not keyword-derived eligibility."""
    sections = re.split(r"(?:^|\n)\s*([一二三四五六七八九十]+[、．.][^\n]{2,35})\s*\n", body or "")
    evidence = []
    for i in range(1, len(sections) - 1, 2):
        heading, text = sections[i], sections[i + 1]
        if any(term in heading for term in ("配租对象", "申请条件", "配租面积", "申请材料", "配租方式", "选房排位规则")):
            value = re.sub(r"\s+", " ", text).strip()
            if value: evidence.append({"heading": heading, "text": value})
    # Several districts put the area standard under 房源概况 as a subsection.
    # Do not lose it merely because the parent heading is not a rule heading.
    nested=re.compile(r'(?:^|\n)\s*([（(][一二三四五六七八九十]+[）)]\s*(?:配租面积标准|房源面积标准))\s*\n')
    for match in nested.finditer(body or ''):
        tail=(body or '')[match.end():]
        end=re.search(r'\n\s*(?:[（(][一二三四五六七八九十]+[）)]\s*[^\n]+\n|[一二三四五六七八九十]+、)',tail)
        text=re.sub(r'\s+',' ',tail[:end.start()] if end else tail).strip()
        if text and not any(clean(e['text'])==clean(text) for e in evidence):
            evidence.append({'heading':match[1],'text':text})
    return evidence


def reviewed_rule(notice, body_text=None):
    """A batch-specific, source-reviewed adapter; never enabled by district alone."""
    text = (notice.title or "") + (body_text if body_text is not None else notice.body_text or "")
    if (notice.category == "rent_launch" and notice.region_code == "光明区"
        and re.search(r"住保租[〔\[（(]2026[〕\]）)]0?38号", text)
        and "100" in text and "同队列" in text):
        anchors = ("单身居民可配租建筑面积40平方米左右", "二至三人家庭可配租建筑面积50平方米左右",
                   "有2个未成年子女的4人家庭", "申请人不能同时认租不同面积档位的户型")
        return {"id": "gm-2026-038-v2", "verified": all(a in clean(text) for a in anchors),
                "inventory_sha256": "4381de0a8c775f464f6786e160de2194060ca259b9a16ec5844b7acd364d2fb9",
                "proof_sha256": "4eee7fab10a7865099f32f52a91f2f0855df62d01b40268d81a245c0fe401c3f",
                "source": "https://www.szgm.gov.cn/xxgk/xqgwhxxgkml/gzgg/content/post_13007760.html",
                "priority_source": "https://www.szgm.gov.cn/xxgk/xqgwhxxgkml/gzgg/content/post_13024633.html",
                "proof_source": "https://www.szgm.gov.cn/attachment/1/1753/1753755/13021117.pdf",
                "band_by_area": {"35": "40", "45": "40", "50": "50", "80": "90", "85": "90", "90": "90",
                                 "100": "100+", "105": "100+", "110": "100+", "118": "100+", "125": "100+"},
                "minimum_people": {"40": 1, "50": 2, "90": 4, "100+": 4},
                "special_priority_band": "100+", "allows_lower_area": True,
                "evidence": "100㎡及以上：同队列中3个以上未成年子女家庭、5人以上且2个未成年子女家庭、4人且2个未成年子女家庭享有同等优先资格；最终以官方核查及排序为准。",
                "proofs": ["第一队列：居民身份证未显示光明户籍的，需提供户口簿；同时核对本批户籍认定时点。",
                           "第二队列：需另外提供社保缴纳单位的营业执照等单位有效证件；未上传企业营业执照等材料的视为不符合第二队列条件。",
                           "第三队列：核对本市正常参保和累计缴费条件，按清单提供对应有效证明。"],
                "proofs_common": "社保类证明需体现累计缴纳月数和最新缴纳月份；仅缴纳居民医疗保险的按清单提供缴费清单。本市退休人员按清单提交退休证明材料。以上为队列佐证，完整申请材料及分支要求以官方清单为准。",
                "rank_stage": "lottery", "priority_group_counts_public": False}
    return {"verified": False, "id": "unreviewed", "source": notice.canonical_url or ""}


def merge_projects(payload, additional):
    """Same source rows replace on refresh; within a source duplicate rows are not accumulated."""
    for item in additional:
        name_key = lambda n: re.sub(r"[（(]无学位[）)]$", "", n)
        match = next((p for p in payload["projects"] if name_key(p["name"]) == name_key(item["name"])), None)
        if match is None:
            payload["projects"].append(item); continue
        if not match["address"]: match["address"] = item["address"]
        match["fees"].update(item["fees"])
        if not match["rows"]: match["rows"] = item["rows"]
        # Tables are authoritative; prose may use a synonymous layout or rounded area.


def finalize(payload):
    payload["issues"] = [v for v in payload["issues"] if v != "已解析项目套数与公告批次总量不一致，暂不据此演算"]
    fee_only = [p for p in payload["projects"] if p["fees"] and not p["rows"]]
    fee_catalog = list({p["name"]:p for p in payload["projects"] if p["fees"]}.values())
    actual = [p for p in payload["projects"] if p["rows"]]
    for project in actual:
        matches = [p for p in fee_catalog if p["name"] == project["name"] or
                   (len(project["name"]) >= 6 and project["name"] in p["name"]) or p["name"] in project["name"]]
        if len(matches) == 1:
            project["name"] = matches[0]["name"]
            project["fees"].update(matches[0]["fees"])
        project["id"] = hashlib.sha256(project["name"].encode()).hexdigest()[:16]
        # Shared tariffs belong once at project level, not once per apartment row.
        for fee in FEE_LABELS:
            vals = [r["fees"].get(fee) for r in project["rows"]]
            if vals and all(v is not None and v == vals[0] for v in vals):
                if fee not in project["fees"] or project["fees"][fee] == vals[0]:
                    project["fees"][fee] = vals[0]
                    for row in project["rows"]: row["fees"].pop(fee,None)
        for row in project["rows"]:
            area = re.fullmatch(r"约?(\d+(?:\.\d+)?)(?:㎡|平方米)?", row["area"])
            if area:
                row["band"] = payload["rule"].get("band_by_area", {}).get(area.group(1), "area:" + area.group(1))
        project["total"] = sum(r["units"] for r in project["rows"])
    deduped = {}
    for p in actual:
        if p["name"] not in deduped: deduped[p["name"]] = p
        else:
            previous = deduped[p["name"]]
            for row in p["rows"]:
                if row not in previous["rows"]: previous["rows"].append(row)
            previous["total"] = sum(r["units"] for r in previous["rows"])
    actual = list(deduped.values())
    # Fee-only projects remain visible with explicit unverified housing details.
    payload["projects"] = actual + [p for p in fee_only if not any(p["name"] == a["name"] for a in actual)]
    total = sum(p["total"] for p in actual)
    if payload.get("declared_total") is not None and total != payload["declared_total"]:
        payload["issues"] = list(dict.fromkeys(payload["issues"] + ["已解析项目套数与公告批次总量不一致，暂不据此演算"]))
    payload["parsed_total"] = total
    if payload.get('declared_projects') is not None and len(actual)!=payload['declared_projects']:
        payload['issues']=list(dict.fromkeys([*payload['issues'],'已解析项目数与公告声明不一致，待核实']))
    for p in actual:
        if p.get('declared_total') is not None and p['total']!=p['declared_total']:
            payload['issues']=list(dict.fromkeys([*payload['issues'],p['name']+'分项套数与项目声明不一致，待核实']))
    payload["complete"] = bool(actual) and not payload["issues"]
    payload["version"] = hashlib.sha256(json.dumps(
        [payload["projects"], payload["evidence"], payload["rule"], payload["sources"], payload["issues"]],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return payload


def inventory_key(notice): return f"inventory:{notice.id}"


def capture_detail(session, notice, html, *, body_text=None, preserve_existing=True):
    if notice.category != "rent_launch" or not batch_key(notice): return None
    if preserve_existing:
        previous = load_inventory(session, notice)
        if previous and previous.get("attachments_checked"): return previous
    body = notice.body_text if body_text is None else body_text
    flat = re.sub(r"\s+", "", body or "")
    totals = [re.search(p, flat) for p in [r"共有房源(\d+)套", r"本次配租共\d+个项目(\d+)套房源", r"本次供应房源(\d+)套", r"本次配租(?:共计|共)(\d+)套", r"本次[^。]{0,60}共推出公共租赁住房(\d+)套"]]
    total = next((int(m[1]) for m in totals if m), None)
    declaration=re.search(r'本次配租[^。]{0,160}?(\d+)个项目(?:共|合计)?(\d+)套(?:住房|房源)',flat)
    if declaration:total=int(declaration[2])
    payload = {"schema": SCHEMA, "notice_id": notice.id, "batch_key": batch_key(notice),
               "notice_version": notice.business_version, "projects": [], "sources": [{"url": notice.canonical_url, "kind": "detail"}],
               "rule": reviewed_rule(notice, body), "evidence": rule_evidence(body), "issues": [], "declared_total": total,
               "checked_at": utcnow().isoformat(), "attachments_checked": False}
    if declaration:payload['declared_projects']=int(declaration[1])
    for table in html_tables(html): merge_projects(payload, parse_table(table))
    merge_projects(payload, prose_projects(body))
    save_inventory(session, notice, finalize(payload))
    return payload


def save_inventory(session, notice, payload):
    record = session.get(HousingMeta, inventory_key(notice))
    if record is None:
        record = HousingMeta(key=inventory_key(notice)); session.add(record)
    record.value = payload
    session.flush()


def load_inventory(session, notice):
    if notice is None: return None
    record = session.get(HousingMeta, inventory_key(notice))
    if not record: return None
    payload = record.value
    if payload.get("notice_version") != notice.business_version or payload.get("batch_key") != batch_key(notice): return None
    return payload


def batch_inventory(session, key):
    if not key: return None
    candidates = list(session.scalars(select(HousingNotice).where(HousingNotice.batch_key_v2 == key,
        HousingNotice.category == "rent_launch").order_by(HousingNotice.id)))
    values = [v for n in candidates if (v := load_inventory(session, n))]
    return max(values, key=lambda v: (v.get("complete", False), len(v["projects"]), v["checked_at"])) if values else None


async def enrich_inventory(session, client, notice, *, force=False):
    if notice.category != "rent_launch" or not batch_key(notice): return
    payload = load_inventory(session, notice)
    if payload and payload.get("attachments_checked") and not force:
        elapsed = utcnow() - __import__('datetime').datetime.fromisoformat(payload["checked_at"])
        if elapsed < timedelta(hours=24): return
    session.commit() if not session.info.get("housing_dry_run") else None
    response = await client.get(notice.canonical_url)
    from app.housing.parsers.detail import parse_detail
    detail = parse_detail(response.text, notice.canonical_url)
    payload = capture_detail(session, notice, detail.content_html, body_text=detail.body_text, preserve_existing=False)
    payload["evidence"] = rule_evidence(detail.body_text)
    errors = []
    for attachment in notice.attachments or []:
        material = "材料清单" in attachment.get("name", "")
        if not material and not re.search(r"房源(?:概况|情况|信息|基本情况)|配租房源", attachment.get("name", "")): continue
        kind = attachment.get("kind", "").lower()
        if kind not in {"pdf", "xls", "xlsx", "et"}: continue
        try:
            session.commit() if not session.info.get("housing_dry_run") else None
            data = await client.get_bytes(attachment["url"], referer=notice.canonical_url)
            digest = hashlib.sha256(data).hexdigest()
            expected = payload["rule"].get("proof_sha256" if material else "inventory_sha256")
            if expected and digest != expected:
                payload["rule"]["verified"] = False
                errors.append("官方附件内容与已复核规则版本不同，需重新核对后开放演算")
            if material:
                payload["sources"].append({"url":attachment["url"],"sha256":digest,"kind":"proof"})
                continue
            if kind == "pdf": tables = pdf_tables(data)
            else:
                from app.housing.lottery import _read_table
                tables = [_read_table(data, "xls" if kind == "et" else kind)]
            for table in tables: merge_projects(payload, parse_table(table))
            payload["sources"].append({"url": attachment["url"], "sha256": hashlib.sha256(data).hexdigest(), "kind": kind})
        except Exception as exc:
            errors.append(f"房源附件未完整核实：{type(exc).__name__}")
    payload["issues"] = errors
    payload["attachments_checked"] = not errors
    save_inventory(session, notice, finalize(payload))


def inventory_text(payload, housing_type="baozu"):
    if not payload: return "房源项目明细、费用及材料：尚未完成逐项核实，请查看配租原文及附件。"
    lines = ["**房源明细与官方费用**\n\n费用单位：市场参考租金、执行租金、物业管理费（简称物业）、专项房屋维修基金或专项维修资金（简称专项维修）为元/㎡·月；停车费为元/月。另标单位的按单项。待核实项不按0计算。"]
    policies = []
    for p in payload["projects"]:
        for fees in [p["fees"], *(r["fees"] for r in p["rows"])]:
            for value in fees.values():
                if isinstance(value,dict):
                    for option in value["options"]:
                        if option["label"] not in policies: policies.append(option["label"])
    if policies: lines.append("租金适用口径："+"；".join(f"口径{i+1}={label}" for i,label in enumerate(policies)))
    def price(value, key):
        if isinstance(value,dict): return "／".join("口径"+str(policies.index(o["label"])+1)+" "+price(o["value"],key) for o in value["options"])
        unit = "元/月" if key == "parking" else "元/㎡·月"
        return str(value).removesuffix(unit)
    for p in payload["projects"]:
        lines.append(f"**房源项目：{p['name']}**")
        lines.append("项目地址：" + (p["address"] or "尚未核实，见官方房源表"))
        for row in p["rows"]:
            area = row['area'].replace('平方米','㎡')
            if '㎡' not in area: area += '㎡'
            lines.append(f"{row['layout']}｜建筑面积{area}｜{row['units']}套")
            if row.get("notes"): lines.append("房源备注：" + row["notes"])
            if row["fees"]: lines.append("该户型费用：" + "；".join(FEE_LABELS[k] + "：" + price(v,k) for k,v in row["fees"].items()))
        if not p["rows"]: lines.append("实际户型、建筑面积、套数：尚未逐项核实，见官方房源表")
        shared = []
        for key,label in {"market":"市场参考","rent":"执行租金","property":"物业","maintenance":"专项维修","parking":"停车"}.items():
            if key == "rent": label = "公租房执行租金" if housing_type == "gongzu" else "保租房执行租金" if housing_type == "baozu" else "执行租金"
            # If a row has a tariff, do not label the shared value as its actual tariff.
            value = p["fees"].get(key)
            shared.append(label + "：" + (price(value,key) if value is not None else ("见户型费用" if any(key in r["fees"] for r in p["rows"]) else "待核实")))
        lines.append("费用标准：" + "；".join(shared))
    if not payload["projects"]: lines.append("项目房源明细及费用尚未核实，见配租原文及附件。")
    # These ordinary Markdown headings also delimit native HTML folding at the
    # transport. Saved text / image exports keep the entire inventory as text.
    lines.append('**申请条件与办理规则**')
    from app.housing.rule_briefs import rule_brief
    source = next((s['url'] for s in payload['sources'] if s.get('kind')=='detail'),next((s['url'] for s in payload['sources']),''))
    for item in payload["evidence"]:
        brief=rule_brief(item)
        if brief:
            label=re.sub(r'^[一二三四五六七八九十]+[、．.]','',item['heading'])
            link=f'[查看{label}原文]({source})' if source.startswith(('https://','http://')) else '原文链接待核实'
            lines.append(f'**{label}（概要）**\n\n{brief}\n\n{link}')
        else: lines.append("**" + item["heading"] + "原文**\n\n" + item["text"])
    if payload["rule"].get("verified") and payload["rule"].get("proofs"):
        lines.append("队列证明材料核对：" + "；".join(payload["rule"]["proofs"]) + payload["rule"]["proofs_common"])
        lines.append(f"[官方申请材料清单]({payload['rule']['proof_source']})")
    if payload["rule"].get("verified"): lines.append("特殊优先规则：" + payload["rule"]["evidence"])
    if payload["issues"]: lines.append("数据提示：" + "；".join(payload["issues"]))
    links = list(dict.fromkeys(s["url"] for s in payload["sources"]))
    lines.append("房源与规则来源：" + "｜".join(f"[官方资料{i}]({url})" for i,url in enumerate(links,1)))
    return "\n\n".join(lines)
