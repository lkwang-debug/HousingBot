"""生成"自助查询页"：买家输入认租编号，自己看队列、顺序号、排位和大概机会。

为什么要有这个文件：

- 推送里没法让买家输入编号，PushPlus 只出不进；总不能每次都让人私聊群主。
- 政府站的 PDF 在手机上打不开搜索，几千行肉眼找不到自己。

做法：把最近的摇号名单重新读一遍，生成一个**纯静态 HTML**（数据内嵌，不发请求），
放到任意静态托管上就是一个查询页。页面里只有「认租编号 → 队列/顺序号/排位」，
**不含姓名、不含证件号**——和入库规则一致。

用法：

    python -m tools.build_lottery_lookup --all-districts --days 900   # 所有区 + 落地页
    python -m tools.build_lottery_lookup --district 光明区 --out lookup/guangming.html
    python -m tools.build_lottery_lookup --index          # 只重建 lookup/index.html 落地页

推送里只放落地页网址，买家自己选区；`HOUSING_LOOKUP_URL` 指向它即可。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from bisect import bisect_right
from html import escape

from sqlalchemy import func, select

from app.db import SessionLocal
from app.housing.batches import batch_key
from app.housing.fetch import HousingHttpClient
from app.housing.forecast import notice_label, queue_rank
from app.housing.lottery import (
    QUEUE_ORDER,
    _queue_name,
    category_display,
    category_key,
    queue_hint,
    queue_prefix,
    read_attachment_rows,
)
from app.housing.models import HousingBatchUnit, HousingNotice, utcnow

MAX_BATCHES = 6
# 过期页上的引导文案。留空则不渲染；填上微信号即可把"被转发的人"变成线索。
EXPIRED_CTA = ""

# 日期门禁：推送里的链接带 ?d=YYYYMMDD，含标注当天共7个北京时间自然日有效。
# 纯静态托管没有服务端，这段判断在前端，改 URL 参数能绕过——挡的是转发传播，不是技术对抗。
_GATE_JS = r"""
<script>
(function () {
  var CTA = __CTA__;
  var day = new URLSearchParams(location.search).get('d') || '';
  var today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' })
    .format(new Date()).replace(/-/g, '');
  var VALID_DAYS = 7, DAY_MS = 86400000;
  function calendarDay(value) {
    if (!/^\d{8}$/.test(value)) return NaN;
    var y = Number(value.slice(0, 4)), m = Number(value.slice(4, 6)), d = Number(value.slice(6, 8));
    var stamp = Date.UTC(y, m - 1, d), parsed = new Date(stamp);
    return parsed.getUTCFullYear() === y && parsed.getUTCMonth() === m - 1 && parsed.getUTCDate() === d ? stamp : NaN;
  }
  var start = calendarDay(day), age = (calendarDay(today) - start) / DAY_MS;
  var startLabel = isNaN(start) ? '' : new Date(start).toISOString().slice(0, 10);
  var endLabel = isNaN(start) ? '' : new Date(start + (VALID_DAYS - 1) * DAY_MS).toISOString().slice(0, 10);
  var gate = document.getElementById('gate');
  var app = document.getElementById('app');
  var title = '', body = '';
  if (!day) {
    title = '请从推送里的链接打开';
    body = '请使用推送中带日期的查询链接，含标注当天共7个北京时间自然日有效。';
  } else if (isNaN(start)) {
    title = '链接日期无效';
    body = '请使用完整的推送查询链接，日期应为有效的8位年月日。';
  } else if (age < 0) {
    title = '链接尚未生效';
    body = '链接有效期：' + startLabel + '至' + endLabel + '（北京时间）。请核对链接及设备日期。';
  } else if (age >= VALID_DAYS) {
    title = '本链接已过期';
    body = '链接有效期：' + startLabel + '至' + endLabel + '（北京时间）。请使用最新推送中的查询链接。';
    if (CTA) { body += '<div class="cta">' + CTA + '</div>'; }
  }
  if (title) {
    gate.innerHTML = '<h1>' + title + '</h1><div class="sub">' + body + '</div>';
    gate.hidden = false;
    return;
  }
  var validity = document.getElementById('link-validity');
  if (validity) validity.textContent = '链接有效期：' + startLabel + '至' + endLabel + '（北京时间，含标注当天共7天）';
  // 保留原链接日期，跨区跳转不能偷偷延长有效期。
  document.querySelectorAll('a.btn').forEach(function (a) {
    a.href += (a.href.indexOf('?') >= 0 ? '&' : '?') + 'd=' + day;
  });
  app.hidden = false;
})();
</script>
"""


def _gate_script() -> str:
    return _GATE_JS.replace("__CTA__", json.dumps(EXPIRED_CTA, ensure_ascii=False))
DISTRICT_NAMES = (
    "福田区",
    "罗湖区",
    "南山区",
    "盐田区",
    "宝安区",
    "龙华区",
    "龙岗区",
    "坪山区",
    "光明区",
    "大鹏新区",
)


DISTRICT_SLUGS = {
    "福田区": "futian",
    "罗湖区": "luohu",
    "南山区": "nanshan",
    "盐田区": "yantian",
    "宝安区": "baoan",
    "龙华区": "longhua",
    "龙岗区": "longgang",
    "坪山区": "pingshan",
    "光明区": "guangming",
    "大鹏新区": "dapeng",
    "深圳市": "shenzhen",
}


def _district_slug(name: str) -> str:
    """文件名用拼音，微信里显示的网址短一点；认不出来的区退回中文名。"""
    for label, slug in DISTRICT_SLUGS.items():
        if label in name or name in label:
            return slug
    return re.sub(r"\W+", "", name) or "other"


def _district_display(raw: str, fallback: str = "") -> str:
    """命令行常写"光明"，库里是"光明区"；落地页统一显示全名。"""
    text = (raw or "").strip()
    if not text:
        return fallback
    for name in DISTRICT_NAMES:
        if text in name:
            return name
    return text


def _queue_names(attachment_rows, prefix: str, hint: str = "") -> list[str]:
    return [_queue_name(row.queue, prefix, hint) for row in attachment_rows]


_QUEUE_WORD_RE = re.compile(r"(第[一二三四五六七八九十]队列|(?:优先|其他)配租队列)")
_SIZE_RE = re.compile(r"(\d{2,3}(?:[、,，]\d{2,3})?)\s*(?:㎡|平方米|平米)")
_LAYOUT_RE = re.compile(r"(单身公寓|小三房|三房|两房|二房|一房)")


def _category_label(label: str, key: str) -> str:
    """附件名是「华章新筑一房户型第一队列摇号结果」，队列要单独选，户型名里去掉。"""
    text = _QUEUE_WORD_RE.sub("", category_display(label, key))
    text = re.sub(r"^\s*\d+[.、．](?!\d)\s*", "", text)
    text = re.sub(r"(?:摇号对象结果|摇号结果|结果)$", "", text)
    return text.strip(" ：:、") or key


def _category_signature(label: str) -> str:
    """附件是不是同一档户型，只看面积/户型字样——附件名里的队列不算差别。

    category_key 对 35㎡、70㎡ 这类档位认不出来（统一回 "all"），光靠它会把
    「意向三」「意向四」并成一个户型，所以再按面积兜一层。
    """
    size = _SIZE_RE.search(label or "")
    if size:
        return size.group(1)
    layout = _LAYOUT_RE.search(label or "")
    return layout.group(1) if layout else ""


def _queue_sort_key(name: str) -> tuple:
    """名单里队列的物理顺序不可信（见过第三队列排在前的 PDF），按规则重排。"""
    from app.housing.lottery import queue_sort_key
    return queue_sort_key(name)


async def _build(district: str, days: int, max_batches: int) -> dict:
    seen_at = func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)
    with SessionLocal() as session:
        statement = select(HousingNotice).where(
            HousingNotice.category == "lottery_result",
            HousingNotice.is_target.is_(True),
            seen_at >= utcnow() - timedelta(days=days),
        )
        if district:
            # 库里存的是"光明区"，命令行常写"光明"，用包含匹配省得用户记全名。
            statement = statement.where(HousingNotice.region_code.like(f"%{district}%"))
        from app.housing.lookup_data import selected_events, attach_inventory, final_selection
        from app.housing.inventory import batch_inventory
        groups_of_events = selected_events(session, district, days, max_batches)
        notices = [group[0] for group in groups_of_events]
        batches: list[dict] = []
        rows: dict[str, list[list]] = {}
        async with HousingHttpClient() as client:
            for event_aliases in groups_of_events:
                notice = event_aliases[0]
                attachments = await read_attachment_rows(client, notice)
                from app.housing.lottery import _pick_files, LOTTERY_NAME_TERMS
                if not attachments or len(attachments) != len(_pick_files(notice, LOTTERY_NAME_TERMS)):
                    raise ValueError(f"公告 {notice.id} 的附件未完整读取，保留旧页面")
                key = batch_key(notice) or ""
                batch_index = len(batches)
                categories: list[dict] = []
                # 按户型归并：华章新筑一个户型拆成三个队列附件、福田按优先/其他拆两份，
                # 它们是同一个户型的几个队列组，各自当成一个"户型"会把排位算到别的队里去。
                groups: dict[str, dict] = {}
                seen_queues: set[str] = set()
                for label, attachment_rows in attachments:
                    prefix = queue_prefix(label)
                    hint = queue_hint(label)
                    names = _queue_names(attachment_rows, prefix, hint)
                    cat = category_key(label)
                    group = groups.setdefault(
                        cat,
                        {
                            "cat": cat,
                            "label": _category_label(label, cat),
                            "rows": [],
                            "names": [],
                            "counts": {},
                        },
                    )
                    group["rows"].extend(attachment_rows)
                    group["names"].extend(names)
                    for queue in names:
                        group["counts"][queue] = group["counts"].get(queue, 0) + 1
                    seen_queues.update(names)
                queues = sorted(seen_queues, key=_queue_sort_key)
                distributions = {}
                for index, group in enumerate(groups.values()):
                    unit = session.scalar(
                        select(HousingBatchUnit).where(
                            HousingBatchUnit.batch_key == key,
                            HousingBatchUnit.category_key == group["cat"],
                        )
                    )
                    attachment_rows, names = group["rows"], group["names"]
                    from app.housing.ingestion import row_issues
                    for row, name in zip(attachment_rows, names):
                        row.queue = name
                    record_issues = row_issues(attachment_rows)
                    issues = list(record_issues)
                    if not key:
                        issues.append("批次未核实，暂停房源估算")
                    categories.append(
                        {
                            "key": group["cat"],
                            "l": group["label"],
                            "n": len(attachment_rows),
                            "households": len({row.apply_no.upper() for row in attachment_rows if row.apply_no}),
                            "u": int(unit.units) if unit and unit.units and unit.verified and not issues else 0,
                            "issue": "; ".join(issues),
                            "q": group["counts"],
                            "has_annotations": any(getattr(row,'annotated',False) for row in attachment_rows),
                            "roster_complete": True,
                            "queue_complete": not record_issues,
                        }
                    )
                    # 逐队列算排位：同一队列里顺序号不比自己的大的个数，就是名次。
                    by_queue: dict[str, list[int]] = {}
                    for queue, row in zip(names, attachment_rows):
                        by_queue.setdefault(queue, []).append(row.order)
                    for queue, values in by_queue.items():
                        values.sort()
                        distributions[f"{index}|{queues.index(queue)}"] = values
                    for queue, row in zip(names, attachment_rows):
                        if not row.apply_no:
                            continue
                        rank = bisect_right(by_queue[queue], row.order)
                        rows.setdefault(row.apply_no.upper(), []).append(
                            [batch_index, index, queues.index(queue), row.order, rank, queues.index(queue), row.round]
                        )
                from app.housing.lookup import source_version
                batch_payload = {
                        "id": notice.id, "version": source_version(session, notice), "R": distributions,
                        "lottery_base_version":source_version(session,notice,include_related=False),
                        "batch_key": key,
                        "source_ids": [n.id for n in event_aliases],
                        "sources": list(dict.fromkeys(n.canonical_url for n in event_aliases)),
                        "t": notice_label(notice),
                        "u": notice.canonical_url or "",
                        "d": (notice.published_at or notice.first_seen_at or utcnow()).strftime(
                            "%Y-%m-%d"
                        ),
                        "Q": queues,
                        "c": categories,
                    }
                attach_inventory(batch_payload, batch_inventory(session, key))
                final_rows, final_notice = await final_selection(session, client, key) if key else ({}, None)
                # A published ordinal is not automatically the number competing
                # ahead of this applicant: other bands / reset queue ordinals exist.
                final_bands = {}
                for identifier in final_rows:
                    matches = [m for m in rows.get(identifier, []) if m[0] == batch_index]
                    if len(matches) == 1: final_bands[identifier] = matches[0][1]
                if len(final_bands) == len(final_rows):
                    for identifier, record in final_rows.items():
                        peers = [r for no,r in final_rows.items() if final_bands[no] == final_bands[identifier]]
                        if len({r['rank'] for r in peers}) == len(peers):
                            record['ahead_same_band'] = sum(r['rank'] < record['rank'] for r in peers)
                            record['scope_verified'] = True
                batch_payload["final_rows"], batch_payload["final_notice"] = final_rows, final_notice
                if final_rows:
                    from app.housing.daily_lifecycle import action_windows
                    final_node=session.get(HousingNotice,next(iter(final_rows.values()))['notice_id'])
                    batch_payload['selection_handling']=action_windows(final_node)
                    batch_payload['selection_info']={'url':final_node.canonical_url,'published_at':(final_node.published_at or final_node.first_seen_at).isoformat()}
                    from app.housing.models import HousingMeta
                    cached=session.get(HousingMeta,f'selection:{final_node.id}')
                    if cached:
                        from app.housing.qualification import cache_root
                        saved=json.loads((cache_root()/cached.value['file']).read_text(encoding='utf-8'))
                        batch_payload['queue_updates']=saved.get('queue_updates',{})
                batches.append(batch_payload)
    name = district or "深圳"
    if notices and notices[0].region_code:
        name = notices[0].region_code
    from app.housing.lookup_data import normalize_round_ranks
    payload = normalize_round_ranks({"district": _district_display(name, "深圳"), "batches": batches, "rows": rows})
    from app.housing.competition import attach_competition
    with SessionLocal() as session:
        attach_competition(session, payload)
        from app.housing.qualification import attach_reviews
        attach_reviews(session,payload)
        from app.housing.review_only import payloads as add_reviews
        add_reviews(session,{_district_slug(district or name)+'.html':payload})
    return payload


def _render_html(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    directory = Path(__file__).resolve().parents[1] / "app/housing"
    template = (directory / "lookup_template.html").read_text(encoding="utf-8")
    return (template.replace("__DISTRICT__", escape(payload["district"]))
            .replace("__DATA__", data)
            .replace("__OPPORTUNITY_JS__", (directory / "opportunity.js").read_text(encoding="utf-8"))
            .replace("__LOOKUP_UI_JS__", (directory / "lookup_ui.js").read_text(encoding="utf-8"))
            .replace("__GATE_SCRIPT__", _gate_script()))


INDEX_TITLE_RE = re.compile(r"<title>(.*?)\s*保障房摇号排位查询</title>")


def _render_index(pages: list[dict]) -> str:
    """落地页：一个链接发给所有群，买家自己选区。

    不内嵌名单，只有几 KB；顺带承担"这是什么、安不安全"的说明。
    """
    buttons = "\n".join(
        f'    <a class="btn" href="{page["file"]}">{page["district"]}<span>→</span></a>'
        for page in pages
    )
    names = "、".join(page["district"] for page in pages)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<title>深圳保障房 · 认租编号自助查询</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 16px; background: #f6f7f9; color: #1f2329; }}
  .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 12px;
           box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  .sub {{ color: #6b7280; font-size: 14px; line-height: 1.7; }}
  .btn {{ display: flex; justify-content: space-between; align-items: center;
          padding: 14px 16px; margin-bottom: 10px; border-radius: 10px;
          background: #0b6bcb; color: #fff; text-decoration: none; font-size: 16px; }}
  .btn:last-of-type {{ margin-bottom: 0; }}
  .btn span {{ opacity: .8; }}
  .note {{ color: #6b7280; font-size: 12px; line-height: 1.8; }}
  .cta {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid #eef0f3;
          color: #1f2329; font-size: 13px; line-height: 1.8; }}
  b {{ color: #1f2329; }}
</style>
</head>
<body>
<div class="card" id="gate" hidden></div>
<div id="app" hidden>
<div class="card">
  <h1>深圳保障房 · 认租编号自助查询</h1>
  <div class="sub" id="link-validity"></div>
  <div class="sub">选择你所在的区，输入<b>认租编号</b>查看公开名单记录，并按申请系统或最新核查确认当前队列。</div>
</div>
<div class="card">
{buttons}
  <div class="note" style="margin-top:12px">
    目前收录：{names}。未列出的区域表示当前页面尚未收录相应名单，不代表没有房源或不符合申请条件。
  </div>
</div>
<div class="card note">
  本页只有「认租编号 → 队列/顺序号/排位」，<b>不含姓名和证件号</b>。<br>
  数据来自各区政府官网公布的公证摇号名单，本页为个人整理，<b>非官方</b>，以官方原文为准。<br>
  规则：<b>先队列、后顺序号</b>——第一队列走完才轮到第二队列，同队列内才按顺序号；
  摇号顺序号不是最终选房顺序，资格核查和放弃都会改变实际结果。
</div>
</div>
{_gate_script()}
</body>
</html>
"""


def _build_all(days, max_batches, directory):
    from app.housing.lookup import build_regions
    with SessionLocal() as session:
        regions = list(session.scalars(select(HousingNotice.region_code).where(
            HousingNotice.category == "lottery_result").distinct()))
    if not regions:
        return 1
    result = asyncio.run(build_regions(regions, days=days, max_batches=max_batches, directory=directory))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _build_index(directory: Path) -> dict:
    """扫描已生成的区页，按页面标题里的区名生成入口。"""
    from app.housing.lookup import build_directory
    directory = build_directory(directory)
    pages: list[dict] = []
    for path in sorted(directory.glob("*.html")):
        if path.name == "index.html":
            continue
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
        match = INDEX_TITLE_RE.search(head)
        raw = match.group(1).strip() if match else path.stem
        pages.append(
            {
                "district": _district_display(raw, path.stem),
                "file": path.name,
                "kb": round(path.stat().st_size / 1024, 1),
            }
        )
    if not pages:
        return {"pages": []}
    out = directory / "index.html"
    out.write_text(_render_index(pages), encoding="utf-8")
    return {"out": str(out), "pages": pages}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成认租编号自助查询页")
    parser.add_argument("--district", default="", help="区名，如 光明区；留空=全部")
    parser.add_argument("--days", type=int, default=240, help="回看天数")
    parser.add_argument("--max-batches", type=int, default=MAX_BATCHES)
    parser.add_argument("--out", default="", help="输出路径，默认 lookup/<district>.html")
    parser.add_argument(
        "--index", action="store_true", help="扫描 --dir 下已生成的区页，生成落地页 index.html"
    )
    parser.add_argument(
        "--all-districts",
        action="store_true",
        help="给库里所有有摇号名单的区各生成一页，并自动重建入口页",
    )
    parser.add_argument("--dir", default="lookup", help="区页所在目录（配合 --index）")
    args = parser.parse_args()

    if args.all_districts:
        return _build_all(args.days, args.max_batches, Path(args.dir))

    if args.index:
        result = _build_index(Path(args.dir))
        if not result["pages"]:
            print(f"{args.dir} 里还没有已生成的区页，先生成区页再跑 --index", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    from app.housing.lookup import build_regions
    directory = Path(args.out).parent if args.out else Path(args.dir)
    result = asyncio.run(build_regions([args.district], days=args.days,
        max_batches=args.max_batches, directory=directory,
        filenames={args.district: Path(args.out).name} if args.out else None))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
