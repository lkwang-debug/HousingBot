"""Presentation-only daily reports: every batch appears, with selective detail."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from app.housing.batches import parse_batch_seq
from app.housing.extract import extract_doc_number
from app.housing.reporting import BatchCard, CATEGORY_NAMES, REGIONS, local_time, summarize
from app.housing.report_render import (
    AUDIENCE_LABELS, HOUSING_LABELS, MAX_CONTENT_BYTES, MAX_CONTENT_CHARS,
    ReportPart, _compact_link, _review_lines, _safe, _short_title, _status_label,
)

LAYOUT = "inventory_daily_v2"
PHASE_LABELS = {"applying": "配租", "review": "核查", "lottery": "摇号", "selection": "选房", "signing": "签约"}
HOUSING_ORDER = {kind: index for index, kind in enumerate(HOUSING_LABELS)}


@dataclass(frozen=True)
class DisplayCard:
    card: BatchCard
    display_title: str
    object_summary: str
    focus_reasons: tuple[str, ...]

    @property
    def detail_level(self):
        return "focus" if self.focus_reasons else "brief"


def _root(card):
    return next((r for r in card.references if r.id == card.batch_id),
                card.references[0] if card.references else None)


def _current(card, snapshot):
    # Closed application windows link to the launch, not an unrelated later URL.
    refs = [r for r in card.references if r.stage == card.stage and r.published_at
            and local_time(r.published_at) <= snapshot.generated_at]
    return max(refs, key=lambda r: (local_time(r.published_at), r.id)) if refs else _root(card)


def focus_reasons(card, snapshot):
    now = snapshot.generated_at
    reasons = []
    if card.is_open:
        reasons.append("认租中")
    if card.start and now <= card.start <= now + timedelta(days=3) and card.stage != "closed":
        reasons.append("三天内开始认租")
    if card.changed:
        reasons.append("本期公告变化")
    review = card.review if card.stage == "review" else None
    known_period = bool(review and review.start and review.end)
    if known_period and review.start <= now.date() <= review.end:
        reasons.append("公示进行中")
    if card.stage in {"review", "selection", "signing"} and not known_period:
        current = _current(card, snapshot)
        if current and current.stage == card.stage and current.published_at and (
            now - timedelta(days=7) <= local_time(current.published_at) <= now
        ):
            reasons.append("近期节点办理期限待核实")
    return tuple(reasons)


def object_summary(card):
    """Name the evidenced audience, never condense eligibility into a threshold."""
    p = card.profile
    text = re.sub(r"\s+", "", "；".join(p.object_text))
    if not text or any("冲突" in issue for issue in p.issues):
        return "对象见原文"
    if p.audience=='enterprise' and re.search(r'配租对象为《[^》]*重点企业名录》内的企业',text):
        return '本批重点企业名录内企业'
    # Do not turn alternatives, exceptions or a negated requirement into a label.
    if re.search(r"[不无未非或]|除外|条件之一|任一", text):
        return "对象见原文"
    if p.audience == "enterprise":
        if '重点企业名录' in text:return '本批重点企业名录内企业'
        if "需求库" in text and re.search(r"企事业单位|企（事）业单位", text):
            return "住房需求库内企事业单位"
        for noun in ("企事业单位", "企（事）业单位", "企业（机构）", "企业(机构)"):
            if noun in text:
                return noun
        return "对象见原文"
    if p.audience == "personal" and re.search(r"在册轮候[人家庭]|户籍轮候家庭", text):
        regions = [r for r in REGIONS if r + "户籍" in text]
        prefix = regions[0] + "户籍" if len(regions) == 1 else ""
        noun = "在册轮候家庭" if "轮候家庭" in text else "在册轮候人"
        return prefix + noun
    # An explicitly named talent credential is meaningful even if personal vs
    # employer application is still unverified. Keep its original short wording.
    if "高层次人才" in text:
        named = re.search(r"对象(?:为|是)(持有[^。；，]{1,28}高层次人才)", text)
        return named.group(1) if named else "高层次人才"
    if p.audience == "personal" and re.search(r"面向个人|个人配租|个人申请|对象(?:为|是).{0,60}个人(?:$|[。，；])", text):
        return "个人"
    return "对象见原文"


def display_title(card):
    title = _short_title(card)
    # A project name or incomplete title cannot safely be replaced by a generic
    # allocation label. The original heading remains available in these cases.
    if re.search(r"\.\.\.|…", title) or card.batch_id is None:
        return title
    clean = re.sub(r"[（(]住保[租售]〔\d{4}〕\d+号[）)]", "", title).strip()
    clean = re.sub(r"\s*[［\[]20\d{2}-\d{2}-\d{2}\s*[］\]]$", "", clean).strip()
    clean = re.sub(r"[（(]20\d{2}年(?:度)?第[一二三四五六七八九十\d]+批(?:次)?[）)]", "", clean)
    if "项目" in clean or "小区" in clean:
        return title
    generic = re.fullmatch(r"(?:20\d{2}年(?:度)?)?(?:第[一二三四五六七八九十\d]+批(?:次)?)?"
        r"(?:面向[^，。；]{1,100})?配租(?:保障性租赁住房|公共租赁住房|住房)(?:的通告)?", clean)
    talent = re.fullmatch(r"面向高层次人才配租保障性租赁住房", clean)
    if not generic and not talent:
        return title
    root = _root(card)
    year_match = re.search(r"20\d{2}年", card.title)
    year = year_match.group(0) if year_match else f"{root.published_at.year}年" if root and root.published_at else ""
    region = "市级" if card.profile.level == "city" else card.profile.region
    seq = parse_batch_seq(card.title)
    if talent:
        stamp = f"（{root.published_at:%m-%d}发布）" if root and root.published_at else ""
        return region + year + "高层次人才配租" + stamp
    if not year:
        return title
    if seq is not None:
        return f"{region}{year}第{seq}批配租"
    doc = extract_doc_number(card.title)
    if doc:
        number = doc.split("〕", 1)[1]
        return f"{region}{year}配租（{number}）"
    if root and root.published_at:
        return f"{region}{year}配租（{root.published_at:%m-%d}发布）"
    return title


def present_cards(snapshot):
    return [DisplayCard(c, display_title(c), object_summary(c), focus_reasons(c, snapshot)) for c in snapshot.cards]


def presentation_audit(snapshot):
    return [{"code": d.card.code, "detail_level": d.detail_level, "focus_reasons": d.focus_reasons,
             "display_title": d.display_title, "object_summary": d.object_summary} for d in present_cards(snapshot)]


def _metrics(summary):
    values = (("新增", summary.new_notices, "条"), ("更新", summary.updated_notices, "条"),
              ("腾退", summary.vacancy_notices, "条"), ("摇号公布", summary.lottery_batches, "批"),
              ("选房通知", summary.selection_batches, "批"))
    return [f"{name}{value}{unit}" for name, value, unit in values if value]


def _summary(label, summary, *, audience=False):
    fields = [f"认租中：{summary.open_batches}批", f"在途{summary.in_transit_batches}批"]
    fields += _metrics(summary) or ["本期无变化"]
    if audience:
        fields.append(f"个人{summary.personal_batches}批／单位{summary.enterprise_batches}批／待核实{summary.unknown_batches}批")
    return f"**{label}** " + "｜".join(fields)


def _source_problem(snapshot):
    values = re.findall(r"(?:异常|未检查)\s*(\d+)", snapshot.source_status)
    return not values or any(int(v) for v in values)


def _header(display):
    c = display.card
    return f"**【{HOUSING_LABELS[c.profile.housing_type]}｜{AUDIENCE_LABELS[c.profile.audience]}｜{c.code}】** {_safe(display.display_title)}"


def _date(value, snapshot, *, clock=False):
    value = local_time(value)
    same_year = value.year == snapshot.generated_at.year
    return value.strftime(("%m-%d" if same_year else "%Y-%m-%d") + (" %H:%M" if clock else ""))


def _status(card, snapshot):
    if card.stage == "waiting":
        return (_date(card.end, snapshot) + "认租已截止，待后续公告") if card.end else "认租已截止，待后续公告"
    if card.stage == "closed":
        return "本批配租已明确结束"
    if card.stage in {"applying", "announced", "unverified_window"}:
        if card.is_open:
            return "认租中｜" + _date(card.start, snapshot, clock=True) + "开始—" + _date(card.end, snapshot, clock=True) + "截止"
        if card.stage == "announced" and card.start:
            return _date(card.start, snapshot, clock=True) + "开始认租｜" + (_date(card.end, snapshot, clock=True) + "截止" if card.end else "截止待核实")
        return "认租时间待核实" + ("｜截止" + _date(card.end, snapshot, clock=True) if card.end else "")
    status = _status_label(card, snapshot)
    current = _current(card, snapshot)
    if current and current.stage == card.stage and current.published_at:
        status += "｜" + _date(current.published_at, snapshot) + "公布"
    if card.stage in {"review", "selection", "signing"} and not (card.review and card.review.start and card.review.end):
        status += "｜办理时间见原文（未核实）"
    return status


def _details(display, snapshot):
    c = display.card
    status = _status(c, snapshot)
    review = _review_lines(c)
    # Date ranges remain complete and keep the original date precision.
    if review: status += "；" + "；".join(review)
    if c.units is not None: status += f"｜房源{c.units}套"
    objects = "对象概要：" + _safe(display.object_summary)
    if c.profile.project_regions:
        objects += "｜项目所在地：" + "、".join(c.profile.project_regions)
    if c.stage == "applying" and c.is_open: action = "拟申请者核对完整资格、按期认租并保存申请编号"
    elif c.stage == "announced": action = "拟申请者核对条件，待认租窗口开始后提交申请"
    elif c.stage == "review":
        action = ("公示期已结束，关注复核结果及后续公告" if c.review and c.review.end and snapshot.generated_at.date() > c.review.end
                  else "已认租家庭核对核查名单；有异议按原文时限申请复核")
    elif c.stage == "lottery": action = "核对编号、队列及后续公告；摇号顺序不等于最终选房顺序"
    elif c.stage == "selection": action = "核对本人名单、场次、材料和期限，按官方选房通知办理"
    elif c.stage == "signing": action = "核对本人签约名单、办理地点、材料及期限"
    else: action = "核对原文及后续公告"
    changes = [r for r in c.references if r.is_new or r.is_update]
    changed = []
    for r in changes:
        words = ("更新" if r.is_update else "新增") + PHASE_LABELS.get(r.stage, "公告")
        if r.vacancy: words += "·涉及腾退房源"
        if words not in changed: changed.append(words)
    action = ("变化：" + "、".join(changed) + "｜" if changed else "") + "办理提示：" + action
    root, current = _root(c), _current(c, snapshot)
    refs = {r.id: r for r in [root, current, *changes] if r is not None}
    links = []
    for ref in refs.values():
        label = "配租原文" if ref.id == c.batch_id else PHASE_LABELS.get(ref.stage, "公告") + "原文"
        links.append(_compact_link(ref, label))
    if not links: links.append("官方原文待核实")
    action += "｜" + "｜".join(links)
    paragraphs = [_header(display), status, objects, action]
    if c.profile.issues: paragraphs[-1] += "；待核实：" + "；".join(c.profile.issues)
    if c.code in snapshot.queryable_codes and snapshot.lookup_url:
        paragraphs[-1] += f"｜[已发布排位查询]({snapshot.lookup_url})"
    return "\n\n".join(paragraphs)


def _brief(display, snapshot):
    c = display.card
    parts = [_header(display), "对象概要：" + _safe(display.object_summary), _status(c, snapshot)]
    if c.profile.project_regions: parts.append("项目所在地：" + "、".join(c.profile.project_regions))
    if c.units is not None: parts.append(f"房源{c.units}套")
    current = _current(c, snapshot)
    if current:
        parts.append(_compact_link(current, "官方原文"))
        if current.published_at and c.stage in {"waiting", "unverified_window", "closed", "unknown"}:
            parts.append("最近公告" + _date(current.published_at, snapshot))
    else: parts.append("官方原文待核实")
    if c.profile.issues: parts.append("待核实：" + "；".join(c.profile.issues))
    if c.review and c.review.start and c.review.end:
        parts.extend(_review_lines(c))
    return "｜".join(parts)


def render_compact_report(snapshot, *, max_chars=MAX_CONTENT_CHARS, max_bytes=MAX_CONTENT_BYTES):
    if snapshot.kind not in {"digest", "evening"}:
        raise ValueError("精简日报只支持早报和晚报")
    if not 2000 <= max_chars <= MAX_CONTENT_CHARS or not 6000 <= max_bytes <= MAX_CONTENT_BYTES:
        raise ValueError("单条消息预算超出允许范围")
    displays = present_cards(snapshot)
    if len({d.card.code for d in displays}) != len(displays):
        raise ValueError("批次编号重复，拒绝生成可能重复的报表")
    title = f"{'🏠 深圳住房管家早报' if snapshot.kind == 'digest' else '🌙 深圳住房管家晚报'} {snapshot.generated_at:%m-%d}"
    total = snapshot.totals
    blocks = [f"## {title}",
        f"统计：{snapshot.window_start:%m-%d %H:%M}—{snapshot.generated_at:%m-%d %H:%M}（北京时间）",
        f"全市去重：认租中{total.open_batches}批｜在途{total.in_transit_batches}批｜新增{total.new_notices}条｜更新{total.updated_notices}条"]
    if _source_problem(snapshot):
        blocks.append("⚠ 来源异常或未完成检查，统计仅代表已采集信息，覆盖可能不全。")
    if not total.new_notices:
        blocks.append("已采集范围内" + ("今日无新增公告" if snapshot.kind == "evening" else "近24小时无新增公告"))
    blocks.append("### 📊 四类保租房概览")
    for key in ("city_baozu", "district_baozu", "personal_baozu", "enterprise_baozu"):
        blocks.append(_summary(CATEGORY_NAMES[key], snapshot.categories[key], audience=key in {"city_baozu", "district_baozu"}))
    blocks.append("※ 层级与对象存在交叉，四类不能直接相加；公告变动项未列出表示0，变动项也可能交叉。")
    blocks.extend(["### 📊 公租房概览", _summary("公租房", snapshot.categories["gongzu"], audience=True)])
    urgent = []
    for c in snapshot.cards:
        if c.is_open and c.end and 0 <= (c.end.date() - snapshot.generated_at.date()).days <= 3:
            days = (c.end.date() - snapshot.generated_at.date()).days
            when = "今天" if days == 0 else "明天" if days == 1 else f"{days}天后"
            urgent.append(f"{c.profile.region}[{c.code}] {when}{c.end:%H:%M}认租截止")
    if urgent: blocks.extend(["### ⏰ 近期截止", "；".join(urgent)])
    shown = set()
    def section(selected, heading):
        blocks.append(heading)
        counts = summarize([d.card for d in selected])
        blocks.append(f"认租中{counts.open_batches}批｜在途{counts.in_transit_batches}批")
        organizers = list(dict.fromkeys(d.card.profile.organizer for d in selected if d.card.profile.organizer))
        if organizers: blocks.append("组织配租机构：" + "、".join(organizers))
        ordered = sorted(enumerate(selected), key=lambda pair: (
            pair[1].detail_level != "focus", HOUSING_ORDER[pair[1].card.profile.housing_type], pair[0]))
        for _, d in ordered:
            blocks.append(_details(d, snapshot) if d.detail_level == "focus" else _brief(d, snapshot))
            from app.housing.report_inventory import additions
            extra = additions(d.card, snapshot)
            if extra: blocks.append(extra)
            shown.add(d.card.code)
    city = [d for d in displays if d.card.profile.level == "city" and d.card.profile.region == "深圳市"]
    if city: section(city, "### 一、市级配租")
    else: blocks.extend(["### 一、市级配租", "暂无符合本期条件的市级记录"])
    blocks.append("### 二、各区配租")
    any_district = False
    for region in REGIONS:
        selected = [d for d in displays if d.card.profile.level == "district" and d.card.profile.region == region]
        if selected:
            section(selected, "### 📍" + region); any_district = True
    if not any_district: blocks.append("暂无符合本期条件的区级记录")
    other = [d for d in displays if d.card.code not in shown]
    if other: section(other, "### 三、分类待核实")
    blocks.append("### 查询与说明")
    if snapshot.lookup_url:
        blocks.append(f"🔎 [已登记发布的查询入口]({snapshot.lookup_url})" +
            ("｜本期可查：" + "、".join(snapshot.queryable_codes) if snapshot.queryable_codes else "（本期暂无已核对可查批次）"))
    blocks.append("在途指仍在跟踪，不代表仍可申请。对象为概要；户籍时点、完整条件及否定／例外见官方原文。")
    blocks.append("📶 " + snapshot.source_status + "｜最近检查：" +
        (snapshot.source_updated_at.strftime("%m-%d %H:%M") if snapshot.source_updated_at else "暂无记录"))
    blocks.append("未列出的区域不代表没有房源；公告发布不等于申请人已完成该阶段。")
    content = "\n\n".join(blocks)
    if len(content) > max_chars or len(content.encode("utf-8")) > max_bytes:
        # A busy day can have many changed batches. Compact only labels and
        # redundant regional counters; keep all classifications, values, dates,
        # actions, warnings, source links and memberships intact.
        short_labels = {"对象概要：": "对象：", "组织配租机构：": "配租机构：",
                        "办理提示：": "提示：", "最近公告": "公告"}
        tight = []
        region_context = None
        for block in blocks:
            if block == "### 一、市级配租": region_context = "深圳市"
            elif block.startswith("### 📍"): region_context = block.removeprefix("### 📍")
            elif block == "### 三、分类待核实": region_context = None
            if re.fullmatch(r"认租中\d+批｜在途\d+批", block):
                continue
            if block.startswith("组织配租机构："):
                names = block.split("：", 1)[1].split("、")
                if region_context:
                    for index, name in enumerate(names):
                        for prefix in ("深圳市" + region_context, region_context):
                            if name.startswith(prefix):
                                names[index] = name.removeprefix(prefix)
                                break
                # The region heading supplies the omitted organization prefix.
                block = "机构：" + "、".join(names)
            for full, short in short_labels.items():
                block = re.sub(r"(^|｜)" + full, lambda m: m.group(1) + short, block, flags=re.M)
            block = block.replace("提示：核对原文及后续公告", "提示：核对原文")
            tight.append(block)
        content = "\n\n".join(tight)
    if len(content) > max_chars or len(content.encode("utf-8")) > max_bytes:
        raise ValueError(f"完整单条报表超出安全容量（{len(content.encode('utf-8'))}字节／上限{max_bytes}）；未截断、未拆条")
    return ReportPart(title, content, tuple(sorted({n for c in snapshot.cards for n in c.notice_ids})),
                      tuple(c.code for c in snapshot.cards))
