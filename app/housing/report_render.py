"""Mobile Markdown layouts. Counts precede pagination; clauses/links stay intact."""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.housing.reporting import CATEGORY_NAMES, REGIONS, STAGE_LABELS, summarize

from app.housing.message_limits import MAX_CONTENT_CHARS, MAX_CONTENT_BYTES
HOUSING_LABELS = {"baozu": "保租房", "gongzu": "公租房", "mixed": "混合房源", "other":"其他住房", "unknown": "住房类型待核实"}
AUDIENCE_LABELS = {"personal": "个人", "enterprise": "单位定向", "unknown": "对象待核实"}


def _safe(text):
    return str(text or "").replace("[", "【").replace("]", "】").replace("<", "〈").replace(">", "〉")


def _link(reference):
    # Do not break a long title across packets, or interpolate Markdown into it.
    label = _safe(reference.title)
    if len(label) > 220:
        label = label[:219] + "…"
    url = reference.url or ""
    if not url.startswith(("https://", "http://")) or len(url) > 4000:
        return label + "（原文链接待核实）"
    return f"[{label}]({url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"


@dataclass(frozen=True)
class ReportPart:
    title: str
    content: str
    notice_ids: tuple[int, ...]
    batch_codes: tuple[str, ...]
    wire_html: str | None = None
    deadline_notice_ids: tuple[int, ...] = ()


def _summary_lines(label, summary, *, show_examples=False):
    lines = [f"**{label}**",
        f"认租中：{summary.open_batches}批｜本期新增：{summary.new_notices}条｜本期腾退：{summary.vacancy_notices}条",
        f"本期摇号公布：{summary.lottery_batches}批｜本期选房通知：{summary.selection_batches}批｜在途：{summary.in_transit_batches}批",
        f"配租对象概览：个人{summary.personal_batches}批、单位{summary.enterprise_batches}批、待核实{summary.unknown_batches}批"]
    if summary.updated_notices:
        lines.append(f"本期更新：{summary.updated_notices}条（不重复算作新增）")
    if show_examples:
        examples = [value for value in summary.object_examples if len(value) <= 180][:2]
        lines.extend("对象原文示例：" + _safe(value) for value in examples)
        if summary.object_examples and not examples:
            lines.append("具体配租对象见下方各批次原文条款")
        elif not summary.object_examples:
            lines.append("暂无已确认的对象条款；不推定通用申请条件")
    return lines


def _overview(snapshot):
    total = snapshot.totals
    window_label = ("统计范围：本次实际推送的公告事件集合（不将其他存量计为本次新增）" if snapshot.kind == "instant"
                    else f"统计范围：{snapshot.window_start:%Y-%m-%d %H:%M}—{snapshot.generated_at:%m-%d %H:%M}（北京时间）")
    lines = [window_label,
        f"生成时刻：{snapshot.generated_at:%Y-%m-%d %H:%M:%S}",
        f"全市去重：认租中{total.open_batches}批｜在途{total.in_transit_batches}批｜本期新增{total.new_notices}条｜更新{total.updated_notices}条", ""]
    if snapshot.kind == "instant":
        lines.append("本次事件统计：仅统计本条通知实际包含的公告；当前状态以生成时刻为准。")
    elif not total.new_notices:
        lines.append("今日无新增公告" if snapshot.kind == "evening" else "近24小时无新增公告")
    lines.extend(["", "### 📊 四类保租房概览"])
    for key in ("city_baozu", "district_baozu", "personal_baozu", "enterprise_baozu"):
        lines.extend(_summary_lines(CATEGORY_NAMES[key], snapshot.categories[key], show_examples=True)); lines.append("")
    lines.append("※ 层级与对象统计存在交叉，四类不能直接相加；新增、腾退及节点统计也可能交叉，总计已去重。")
    lines.extend(["", "### 📊 公租房独立概览"])
    lines.extend(_summary_lines("公租房", snapshot.categories["gongzu"], show_examples=True))
    lines.extend(["", "### ⏰ 今晚重点" if snapshot.kind == "evening" else "", "### ⏰ 需要关注" if snapshot.kind != "evening" else ""])
    urgent = [c for c in snapshot.cards if c.is_open and c.end
              and 0 <= (c.end.date() - snapshot.generated_at.date()).days <= 3]
    if not urgent:
        lines.append("未来三天没有已核实的认租截止事项")
    for card in urgent:
        days = (card.end.date() - snapshot.generated_at.date()).days
        when = "今天" if days == 0 else "明天" if days == 1 else f"{days}天后"
        lines.append(f"- {card.profile.region or '层级待核实'}[{card.code}]：{when} {card.end:%H:%M} 认租截止，详见对应批次")
    for stage, label in (("review", "核查公告"), ("lottery", "摇号公布"), ("selection", "选房通知"), ("signing", "签约安排")):
        changed = [c.code for c in snapshot.cards if any(r.is_new and r.stage == stage for r in c.references)]
        if changed:
            lines.append(f"- 本期{label}：" + "、".join(changed))
    return "\n".join(line for i, line in enumerate(lines) if line or (i and lines[i - 1]))


def _action(card):
    if card.stage == "applying":
        return "拟申请本批的个人或单位先核对配租对象与条件，按原文入口提交并保存认租编号。"
    if card.stage == "announced":
        return "核对原文的认租开始时间和申请条件；开始时间未核实时不推定已经开放。"
    if card.stage == "unverified_window":
        return "先核对官方认租时间与受理状态，缺少时间不表示尚未开始，也不表示仍能申请。"
    if card.stage == "waiting": return "认租已结束，关注资格核查或后续官方安排；尚未认租者不要按旧窗口提交。"
    if card.stage == "review": return "已认租者核对资格名单及复核安排；复核期限、方式以原文为准。"
    if card.stage == "lottery": return "已认租者用编号核对名单和队列，摇号顺序不是最终选房顺序。"
    if card.stage == "selection": return "仅按本批官方名单、场次和材料要求办理；不要根据推算替代选房通知。"
    if card.stage == "signing": return "已选房的个人或单位核对签约通知中的期限、材料及办理地点。"
    if card.stage == "closed": return "本批已明确结束；后续申请以新批次公告为准。"
    return "先阅读本条公告；原配租批次尚未可靠关联，不借用其他批次的资格或房源。"


def _status_label(card, snapshot):
    label = STAGE_LABELS[card.stage]
    if card.stage == "review" and card.review and card.review.start and card.review.end:
        today = snapshot.generated_at.date()
        state = "尚未开始" if today < card.review.start else "已结束，待后续公告" if today > card.review.end else "中"
        label += f"（{card.review.label}{state}）"
    return label


def _review_lines(card):
    info = card.review
    if info is None:
        return []
    lines = []
    if info.start and info.end:
        lines.append(f"{info.label}期：{info.start:%Y-%m-%d}—{info.end:%Y-%m-%d}")
    counts = []
    if info.eligible is not None: counts.append(f"合格{info.eligible}户")
    if info.ineligible is not None: counts.append(f"不合格{info.ineligible}户")
    if counts: lines.append("核查结果：" + "／".join(counts))
    return lines


def _card_text(card, snapshot):
    p = card.profile
    root = next((r for r in card.references if r.id == card.batch_id), card.references[0])
    lines = [f"**【{HOUSING_LABELS[p.housing_type]}｜{AUDIENCE_LABELS[p.audience]}｜{card.code}】**",
             _link(root), f"- 组织配租机构：{_safe(p.organizer) or '待核实'}"]
    if p.project_regions:
        lines.append("- 项目所在地：" + "、".join(p.project_regions) + "（不改变组织配租层级）")
    if p.object_text:
        lines.extend("- 配租对象原文：" + _safe(text) for text in p.object_text)
    else:
        lines.append("- 配租对象：未取得明确条款，请核对原文")
    lines.extend("- 资格原文：" + _safe(text) for text in p.qualifications)
    if p.qualifications:
        lines.append("- 其余资格条件及例外见原文，不据此判定具体家庭一定符合")
    else:
        lines.append("- 资格条件：见原文，不推定市级或区级通用门槛")
    lines.extend("- 员工条件原文：" + _safe(text) for text in p.employee_conditions)
    lines.append(f"- 当前状态：{_status_label(card, snapshot)}")
    lines.extend("- " + value for value in _review_lines(card))
    if card.start:
        lines.append(f"- 认租开始：{card.start:%Y-%m-%d %H:%M}")
    elif card.batch_id is not None:
        lines.append("- 认租开始：开始时间未核实，以原文为准")
    if card.end:
        lines.append(f"- 认租截止：{card.end:%Y-%m-%d %H:%M}")
    if card.stage in {"review", "selection", "signing"}:
        lines.append("- 本阶段办理时间：见该阶段官方通知，不将旧认租截止时间当作办理期限")
    if card.units is not None:
        lines.append(f"- 原文明确房源总量：{card.units}套")
    lines.append("- 流程记录：" + " → ".join(f"{label}（{state}）" for label, state in card.timeline))
    changes = [r for r in card.references if r.is_new or r.is_update]
    if changes:
        lines.append(f"- 本期变化：新增{sum(r.is_new for r in changes)}条，更新{sum(r.is_update for r in changes)}条")
    else:
        lines.append("- 本期无新增；保留当前在途进度")
    lines.append("- 现在要做：" + _action(card))
    if card.code in snapshot.queryable_codes and snapshot.lookup_url:
        lines.append(f"- 已登记发布的排位查询：[认租编号查排位]({snapshot.lookup_url})")
    if p.issues:
        lines.append("- 数据提示：" + "；".join(p.issues))
    # Keep all in-window originals, plus one latest original for each historical node.
    links = {r.id: r for r in changes}
    for stage in ("applying", "review", "lottery", "selection", "signing"):
        refs = [r for r in card.references if r.stage == stage]
        if refs:
            latest = max(refs, key=lambda r: (r.published_at or snapshot.window_start, r.id))
            links[latest.id] = latest
    link_lines = ["- 官方原文：" + _link(r) for r in links.values() if r.id != root.id]
    from app.housing.report_inventory import additions
    extra = additions(card, snapshot)
    if extra: lines.append(extra)
    return "\n".join(lines), link_lines


def render_report_parts(snapshot, *, max_chars=MAX_CONTENT_CHARS, max_bytes=MAX_CONTENT_BYTES):
    """Daily reports are exactly one payload; instant packets may still paginate."""
    if snapshot.kind in {"digest", "evening"}:
        from app.housing.daily_render import render_daily
        return [render_daily(snapshot, max_chars=max_chars, max_bytes=max_bytes)]
    from app.housing.instant_render import render_instant
    return render_instant(snapshot,max_chars=max_chars,max_bytes=max_bytes)


def _short_title(card):
    # Remove only repeated publisher/boilerplate, never cut a batch/project name.
    title = card.title
    for prefix in (card.profile.organizer, "深圳市" + card.profile.organizer):
        if prefix and title.startswith(prefix):
            title = title[len(prefix):].removeprefix("关于")
            break
    title = re.sub(r"(?:及)?(?:有关事项|相关事项)的通告(?=[（(]|$)", "", title)
    return _safe(title)


def _compact_link(reference, label):
    from types import SimpleNamespace
    return _link(SimpleNamespace(title=label, url=reference.url))


def _compact_flow(card):
    states = {"配租通告已收录": "已", "原配租公告未关联": "未关联", "未关联": "未关联",
        "已有官方公告": "已", "当前节点，已有公告": "当前", "未采集到后续公告": "待",
        "认租已截止": "已截止", "认租中": "进行中", "认租时间待核实": "时间待核实", "尚未开始认租": "未开始"}
    return "→".join(("核查" if label == "资格核查" else label) + states.get(state, state)
                    for label, state in card.timeline)


def _daily_card(card, snapshot, *, qualifications):
    p = card.profile
    root = next((r for r in card.references if r.id == card.batch_id), card.references[0])
    lines = [f"**【{HOUSING_LABELS[p.housing_type]}｜{AUDIENCE_LABELS[p.audience]}｜{card.code}】** "
        + _compact_link(root, _short_title(card))]
    if p.project_regions:
        lines.append("项目所在地：" + "、".join(p.project_regions))
    lines.append("配租对象：" + ("；".join(_safe(s) for s in p.object_text) or "待核实，见原文"))
    if qualifications:
        clauses = tuple(dict.fromkeys(p.qualifications + p.employee_conditions))
        lines.extend("条件原文：" + _safe(s) for s in clauses)
    status = "状态：" + _status_label(card, snapshot)
    if card.units is not None:
        status += f"｜房源{card.units}套"
    lines.append(status)
    lines.extend(_review_lines(card))
    if card.start or card.end:
        start = card.start.strftime("%Y-%m-%d %H:%M") if card.start else "开始时间未核实"
        end_format = "%m-%d %H:%M" if card.start and card.end and card.start.year == card.end.year else "%Y-%m-%d %H:%M"
        end = card.end.strftime(end_format) if card.end else "截止待核实"
        lines.append(f"认租：{start}—{end}")
    elif card.batch_id is not None:
        lines.append("认租：开始、截止时间未核实")
    lines.append("流程：" + _compact_flow(card))
    changed = [r for r in card.references if r.is_new or r.is_update]
    if changed:
        lines[-1] += f" 本期：新增{sum(r.is_new for r in changed)}条／更新{sum(r.is_update for r in changed)}条"
    # Keep every in-window source and the latest official record for each phase.
    refs = {r.id: r for r in changed}
    for stage in ("applying", "review", "lottery", "selection", "signing"):
        candidates = [r for r in card.references if r.stage == stage]
        if candidates:
            latest = max(candidates, key=lambda r: (r.published_at or snapshot.window_start, r.id))
            refs[latest.id] = latest
    links = []
    labels = {"applying": "认租", "review": "核查", "lottery": "摇号", "selection": "选房", "signing": "签约"}
    for ref in refs.values():
        if ref.id == root.id:
            continue
        label = labels.get(ref.stage, "公告")
        if ref.published_at:
            label += ref.published_at.strftime("%m-%d")
        if ref.is_new: label += "·新增"
        elif ref.is_update: label += "·更新"
        links.append(_compact_link(ref, label))
    if links: lines.append("官方节点：" + "｜".join(links))
    if p.issues:
        lines.append("待核实：" + "；".join(p.issues))
    # Markdown soft breaks collapse into spaces in the actual PushPlus page.
    # Each field must be a paragraph; keep the batch tag and title together.
    return "\n\n".join(lines)


def _single_daily_report(snapshot, *, max_chars, max_bytes, allow_overflow=False):
    if not 2000 <= max_chars <= MAX_CONTENT_CHARS or not 6000 <= max_bytes <= MAX_CONTENT_BYTES:
        raise ValueError("单条消息预算超出允许范围")
    title = f"{'🏠 深圳住房管家早报' if snapshot.kind == 'digest' else '🌙 深圳住房管家晚报'} {snapshot.generated_at:%m-%d}"

    def build(qualifications):
        # Entire qualification clauses are either included or referred to the
        # official source. Never truncate a negation/exception to fit the payload.
        total = snapshot.totals
        lines = [f"## {title}",
            "", f"统计：{snapshot.window_start:%m-%d %H:%M}—{snapshot.generated_at:%m-%d %H:%M}（北京时间）",
            "", f"全市去重：认租中{total.open_batches}批｜在途{total.in_transit_batches}批｜本期新增{total.new_notices}条｜更新{total.updated_notices}条",
            "", "在途＝尚未明确结束、仍在跟踪的批次，含待开始、认租中及等待后续公告；不代表仍可申请。"]
        if not total.new_notices:
            lines.extend(["", "今日无新增公告" if snapshot.kind == "evening" else "近24小时无新增公告"])
        lines.extend(["", "### 📊 四类保租房概览"])
        for key in ("city_baozu", "district_baozu", "personal_baozu", "enterprise_baozu"):
            s = snapshot.categories[key]
            lines.extend(["", f"**{CATEGORY_NAMES[key]}**",
                f"认租中：{s.open_batches}批｜新增{s.new_notices}条｜更新{s.updated_notices}条｜腾退{s.vacancy_notices}条",
                f"摇号公布{s.lottery_batches}批｜选房通知{s.selection_batches}批｜在途{s.in_transit_batches}批",
                f"对象：个人{s.personal_batches}批／单位{s.enterprise_batches}批／待核实{s.unknown_batches}批"])
        lines.extend(["", "※ 层级与对象存在交叉，四类不能直接相加；新增、腾退及节点也可能交叉。具体配租对象逐批列出。"])
        s = snapshot.categories["gongzu"]
        lines.extend(["", "### 📊 公租房概览",
            f"认租中：{s.open_batches}批｜新增{s.new_notices}条｜更新{s.updated_notices}条｜腾退{s.vacancy_notices}条",
            f"摇号公布{s.lottery_batches}批｜选房通知{s.selection_batches}批｜在途{s.in_transit_batches}批",
            f"对象：个人{s.personal_batches}批／单位{s.enterprise_batches}批／待核实{s.unknown_batches}批",
            "", "### ⏰ 需要关注"])
        urgent = [c for c in snapshot.cards if c.is_open and c.end
                  and 0 <= (c.end.date() - snapshot.generated_at.date()).days <= 3]
        for card in urgent:
            days = (card.end.date() - snapshot.generated_at.date()).days
            when = "今天" if days == 0 else "明天" if days == 1 else f"{days}天后"
            lines.extend(["", f"{card.profile.region or '层级待核实'}[{card.code}]：{when}{card.end:%H:%M}认租截止"])
        if not urgent: lines.append("未来三天无已核实的认租截止事项")
        for stage, label in (("review", "核查"), ("lottery", "摇号"), ("selection", "选房"), ("signing", "签约")):
            codes = [c.code for c in snapshot.cards if any(r.is_new and r.stage == stage for r in c.references)]
            if codes: lines.extend(["", f"本期{label}公告：" + "、".join(codes)])
        lines.extend(["", "流程标记：已＝已有官方记录；当前＝已公布的当前节点；待＝未采集到后续公告。",
            "", "资格、员工条件及例外" + ("以原文全部条款为准；以下仅摘录。" if qualifications else "请点批次标题查看完整官方条款；不据对象标签判定个人资格。"),
            "", "认租截止后不能按旧窗口申请；核查、摇号、选房、签约请核对该阶段的名单及办理期限。",
            "", "未标记本期变化的记录，本期无新增。"])
        shown = set()
        sections = [("city", "深圳市", "### 一、市级配租")]
        sections += [("district", r, "### 📍" + r) for r in REGIONS]
        districts_started = False
        for level, region, heading in sections:
            selected = [c for c in snapshot.cards if c.profile.level == level and c.profile.region == region]
            if level == "city" and not selected:
                lines.extend(["", heading, "暂无符合本期条件的市级批次记录"])
            if not selected: continue
            if level == "district" and not districts_started:
                lines.extend(["", "### 二、各区配租"]); districts_started = True
            s = summarize(selected)
            lines.extend(["", heading, f"认租中{s.open_batches}批｜本期新增{s.new_notices}条｜在途{s.in_transit_batches}批"])
            organizers = list(dict.fromkeys(c.profile.organizer for c in selected if c.profile.organizer))
            lines.append("")
            lines.append("组织配租机构：" + "、".join(organizers) if organizers else "组织配租机构：待核实")
            for housing in ("baozu", "gongzu", "mixed", "unknown"):
                for card in selected:
                    if card.profile.housing_type != housing: continue
                    lines.extend(["", _daily_card(card, snapshot, qualifications=qualifications)])
                    shown.add(card.code)
        if not districts_started:
            lines.extend(["", "### 二、各区配租", "暂无符合本期条件的区级记录"])
        lines.extend(["", "### 三、待核实与查询"])
        for card in snapshot.cards:
            if card.code not in shown:
                lines.extend(["", _daily_card(card, snapshot, qualifications=qualifications)])
        uncertain = [c.code for c in snapshot.cards if c.profile.issues or c.batch_id is None]
        lines.append("待核实编号：" + "、".join(uncertain) + "（原因见对应记录）" if uncertain else "本期无待核实分类信息")
        if snapshot.lookup_url:
            lines.extend(["", f"🔎 [已登记发布的查询入口]({snapshot.lookup_url})"])
            lines.append("")
            lines.append("本期已核对可查：" + "、".join(snapshot.queryable_codes) if snapshot.queryable_codes else "本期没有已核对可查的批次，入口为上次登记版本")
        lines.extend(["", "📶 " + snapshot.source_status,
            "", "来源最近检查：" + (snapshot.source_updated_at.strftime("%m-%d %H:%M") if snapshot.source_updated_at else "暂无检查记录"),
            "", "未列出的区域不代表没有房源；来源异常时可能不完整。公告发布不等于申请人已完成该阶段。"])
        return "\n".join(lines)

    content = ""
    for qualifications in (True, False):
        content = build(qualifications)
        if len(content) <= max_chars and len(content.encode("utf-8")) <= max_bytes:
            return ReportPart(title, content, tuple(sorted({n for c in snapshot.cards for n in c.notice_ids})),
                              tuple(c.code for c in snapshot.cards))
    if allow_overflow:
        # 群聊版按条拆分发，不受单条推送容量约束。
        return ReportPart(title, content, tuple(sorted({n for c in snapshot.cards for n in c.notice_ids})),
                          tuple(c.code for c in snapshot.cards))
    # User requires one notification: never silently drop batches or send a
    # partial report followed by extra notifications when the account grows.
    raise ValueError(f"完整单条报表超出安全容量（{len(content.encode('utf-8'))}字节／上限{max_bytes}）；未截断、未拆条，请检查报表容量")


def _render_detailed_parts(snapshot, *, max_chars=MAX_CONTENT_CHARS, max_bytes=MAX_CONTENT_BYTES):
    if max_chars > MAX_CONTENT_CHARS or max_chars < 2000:
        raise ValueError("消息长度必须介于2000与18000字符之间")
    if max_bytes > MAX_CONTENT_BYTES or max_bytes < 6000:
        raise ValueError(f"UTF-8消息预算必须介于6000与{MAX_CONTENT_BYTES}字节之间")
    titles = {"instant": "🔔 深圳住房通告", "digest": "🏠 深圳住房管家早报", "evening": "🌙 深圳住房管家晚报"}
    title = f"{titles[snapshot.kind]} {snapshot.generated_at:%m-%d}"
    header = f"## {title}\n\n"
    continuation = f"## {title}（续）\n\n统计时间与全市总览见第1部分；批次编号保持不变。\n\n"
    # Reserve space for final part labels, which are known only after packing.
    budget = max_chars - 80
    byte_budget = max_bytes - 400
    def fits(text):
        return len(text) <= budget and len(text.encode("utf-8")) <= byte_budget
    chunks = []
    content, ids, codes = header, set(), []

    def append(block, notice_ids=(), code=None):
        nonlocal content, ids, codes
        if not fits(continuation + block + "\n\n"):
            raise ValueError(f"单个完整条款或链接超过消息长度上限：{code or '概览'}；拒绝截断")
        if not fits(content + block + "\n\n"):
            chunks.append((content, tuple(sorted(ids)), tuple(codes)))
            content, ids, codes = continuation, set(), []
        content += block + "\n\n"
        ids.update(notice_ids)
        if code and code not in codes: codes.append(code)

    # Overview sections may be split at whole paragraphs, never midway through a clause.
    for block in _overview(snapshot).split("\n\n"):
        append(block)
    shown = set()
    sections = [("city", "深圳市", "一、市级配租")]
    sections.extend(("district", region, f"📍{region}") for region in REGIONS)
    district_heading = False
    for level, region, heading in sections:
        selected = [c for c in snapshot.cards if c.profile.level == level and c.profile.region == region]
        if level == "city" and not selected:
            append("### 一、市级配租\n本期无新增，暂无符合本期展示条件的在途批次记录。")
        if not selected: continue
        if level == "district" and not district_heading:
            append("### 二、各区配租"); district_heading = True
        summary = summarize(selected)
        section_header = f"### {heading}\n认租中{summary.open_batches}批｜本期新增{summary.new_notices}条｜在途{summary.in_transit_batches}批"
        if not summary.new_notices: section_header += "\n本期无新增"
        append(section_header)
        for housing in ("baozu", "gongzu", "mixed", "unknown"):
            for card in selected:
                if card.profile.housing_type != housing: continue
                text, links = _card_text(card, snapshot)
                context = f"**{region} · {HOUSING_LABELS[housing]}**\n"
                whole = context + text + ("\n" + "\n".join(links) if links else "")
                if fits(continuation + whole + "\n\n"):
                    append(whole, card.notice_ids, card.code)
                else:
                    # Exceptionally long cards continue at complete field/clause boundaries.
                    # The main card heading is emitted once, never duplicated in its continuation.
                    block = context
                    for line in text.splitlines():
                        if not fits(continuation + block + line + "\n\n"):
                            append(block, card.notice_ids, card.code)
                            block = f"**{card.code} 详情续（完整条款）**\n"
                        block += line + "\n"
                    if block.strip(): append(block, card.notice_ids, card.code)
                    for link in links:
                        append(f"**{card.code} 官方原文续**\n{link}", card.notice_ids, card.code)
                shown.add(card.code)
    if not district_heading:
        append("### 二、各区配租\n本期无符合展示条件的区级信息；不据此推定没有可申请房源。")
    else:
        append("其余已覆盖区域本期未列出符合条件的信息；来源异常时可能不完整，以官方原文为准。")
    append("### 三、待核实信息")
    issues = [c for c in snapshot.cards if c.profile.issues or c.batch_id is None]
    if not issues: append("本期无待核实分类信息。")
    for card in snapshot.cards:
        if card.code not in shown:
            text, links = _card_text(card, snapshot)
            if fits(continuation + text + "\n\n"):
                append(text, card.notice_ids, card.code)
            else:
                block = ""
                for line in text.splitlines():
                    if not fits(continuation + block + line + "\n\n"):
                        append(block, card.notice_ids, card.code)
                        block = f"**{card.code} 详情续（完整条款）**\n"
                    block += line + "\n"
                if block.strip(): append(block, card.notice_ids, card.code)
            for link in links: append(f"**{card.code} 官方原文续**\n{link}", card.notice_ids, card.code)
            shown.add(card.code)
        elif card in issues:
            append(f"- [{card.code}] 分类待核实：" + "；".join(card.profile.issues or ("原配租批次未关联",)) + "。详情见对应区域。")
    footer = []
    if snapshot.lookup_url:
        footer.append(f"🔎 [已登记发布的查询入口]({snapshot.lookup_url})")
        footer.append("当前报表已核对可查：" + "、".join(snapshot.queryable_codes) if snapshot.queryable_codes else "入口为最近一次登记发布的版本；本期没有已核对可查的批次。")
    footer.append("📶 " + snapshot.source_status)
    footer.append("来源最近检查：" + (snapshot.source_updated_at.strftime("%Y-%m-%d %H:%M") if snapshot.source_updated_at else "暂无检查记录"))
    footer.append("公告已收录不等于所有申请人已完成该阶段；具体对象、资格、名单及办理时间以官方原文为准。")
    append("\n".join(footer))
    chunks.append((content, tuple(sorted(ids)), tuple(codes)))
    parts = []
    for i, (body, notice_ids, batch_codes) in enumerate(chunks, 1):
        suffix = f"（第{i}/{len(chunks)}部分）" if len(chunks) > 1 else ""
        body = body.replace("\n\n", f"\n{suffix}\n\n", 1) if suffix else body
        if len(body) > max_chars: raise ValueError("消息分段长度校验失败")
        if len(body.encode("utf-8")) > max_bytes: raise ValueError("消息UTF-8字节长度校验失败")
        parts.append(ReportPart((title + suffix)[:100], body.rstrip(), notice_ids, batch_codes))
    return parts


# ── 群聊短版（≤1000字，纯文本，可直接粘贴） ──────────────────────────

def _group_line(card, now):
    head = " · ".join(x for x in (
        card.profile.region or "层级待核实",
        card.code,
        HOUSING_LABELS.get(card.profile.housing_type, ""),
        f"{card.units}套" if card.units is not None else "",
    ) if x)
    if card.end:
        days = (card.end.date() - now.date()).days
        when = {0: "今天", 1: "明天"}.get(days) or (f"{days}天后" if days > 0 else "")
        head += f"｜{card.end:%m-%d %H:%M} 截止" + (f"（{when}）" if when else "")
    elif card.is_open:
        head += "｜截止时间待核实"
    return "· " + head


def render_group_text(snapshot, *, max_chars=1000):
    """群聊短版：只保留认租中批次、本期节点和总量，其余一律不展开。"""
    now = snapshot.generated_at
    label = "晚报" if snapshot.kind == "evening" else "早报"
    header = f"深圳保障房 {now:%m-%d} {label}"

    open_cards = sorted(
        (c for c in snapshot.cards if c.is_open),
        key=lambda c: (c.end is None, c.end or now),
    )
    open_lines = [_group_line(c, now) for c in open_cards]

    stage_names = {"review": "核查", "lottery": "摇号", "selection": "选房", "signing": "签约"}
    moved = []
    for card in snapshot.cards:
        if card.is_open:
            continue
        for stage, name in stage_names.items():
            hits = [r for r in card.references if r.is_new and r.stage == stage]
            if hits:
                stamp = max((r.published_at for r in hits if r.published_at), default=None)
                moved.append(f"· {card.profile.region or '层级待核实'} {card.code} "
                             f"{HOUSING_LABELS.get(card.profile.housing_type, '')} {name}公告"
                             + (f" {stamp:%m-%d}" if stamp else ""))
                break

    tail = ["",
            f"新增{snapshot.totals.new_notices}条｜更新{snapshot.totals.updated_notices}条"
            f"｜在途{snapshot.totals.in_transit_batches}批",
            "完整通告和原文链接见推送原文。",
            "信息来自各区政府官网，以官方原文为准。"]

    def assemble(shown, hidden=0):
        out = [header, ""]
        out.append(f"▍正在认租 {len(open_cards)} 批" if open_cards else "▍今天没有可申请的批次")
        out.extend(shown)
        if hidden:
            out.append(f"· 另有 {hidden} 批未列出，见完整版")
        if moved:
            out.extend(["", "▍本期节点"] + moved)
        out.extend(tail)
        return chr(10).join(out)

    for keep in range(len(open_lines), -1, -1):
        text = assemble(open_lines[:keep], len(open_lines) - keep)
        if len(text) <= max_chars:
            return text.strip()
    return assemble([], len(open_lines))[:max_chars].strip()


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def to_plain_text(text, *, keep_links=False):
    """把推送用的 markdown 转成可以直接粘进群聊的纯文本。

    群聊不渲染 markdown，`[标题](链接)` 会原样显示；链接本身在群里也不可点，
    默认只留标题，需要时用 keep_links 把裸地址放回标题后面。
    """
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} {m.group(2)}" if keep_links else m.group(1), text)
    text = re.sub(r"^#{1,6}\s*", "▍", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text


def split_messages(text, *, max_chars=1000, reserve=12):
    """按空行分块，贪心装进 max_chars 一条；单块超长再按行切。

    reserve 是给「(1/4) 」这种序号留的余量，序号在切完才知道总数。
    """
    budget = max_chars - reserve
    if budget < 100:
        raise ValueError("max_chars 太小")
    chunks, cur = [], ""
    for block in re.split(r"\n{2,}", text.strip()):
        block = block.strip()
        if not block:
            continue
        pieces = [block]
        if len(block) > budget:
            pieces, buf = [], ""
            for line in block.split("\n"):
                while len(line) > budget:
                    if buf:
                        pieces.append(buf); buf = ""
                    pieces.append(line[:budget])
                    line = line[budget:]
                if len(buf) + len(line) + 1 > budget and buf:
                    pieces.append(buf); buf = line
                else:
                    buf = f"{buf}\n{line}" if buf else line
            if buf:
                pieces.append(buf)
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > budget:
                chunks.append(cur); cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    total = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, 1):
        mark = f"({i}/{total}) " if total > 1 else ""
        out.append(mark + chunk)
    return out


def render_daily_plain(snapshot, *, keep_links=False):
    """完整日报的纯文本，不受单条推送容量限制。长图和拆条都从这里取。"""
    part = _single_daily_report(snapshot, max_chars=MAX_CONTENT_CHARS,
                                max_bytes=MAX_CONTENT_BYTES, allow_overflow=True)
    return to_plain_text(part.content, keep_links=keep_links)


def render_group_full(snapshot, *, max_chars=1000, keep_links=False):
    """群聊全量版：完整报表按条拆分，一条一条发，不丢任何内容。"""
    return split_messages(render_daily_plain(snapshot, keep_links=keep_links), max_chars=max_chars)


def render_group_brief(snapshot):
    """长图用的精选版：认租中批次全展开＋本期节点＋本期新增，不含交叉统计。"""
    now = snapshot.generated_at
    label = "晚报" if snapshot.kind == "evening" else "早报"
    out = [f"▍深圳保障房 {now:%m-%d} {label}", ""]
    open_cards = sorted((c for c in snapshot.cards if c.is_open),
                        key=lambda c: (c.end is None, c.end or now))
    if open_cards:
        out += [f"▍正在认租 {len(open_cards)} 批", ""]
        for card in open_cards:
            out += [_daily_card(card, snapshot, qualifications=False), ""]
    stage_names = {"review": "核查", "lottery": "摇号", "selection": "选房", "signing": "签约"}
    moved = []
    for card in sorted(snapshot.cards, key=lambda c: c.profile.region or ""):
        if card.is_open:
            continue
        for stage, name in stage_names.items():
            hits = [r for r in card.references if r.is_new and r.stage == stage]
            if hits:
                stamp = max((r.published_at for r in hits if r.published_at), default=None)
                moved.append(f"· {card.profile.region or '层级待核实'} {card.code} "
                             f"{HOUSING_LABELS.get(card.profile.housing_type, '')} {name}公告"
                             + (f" {stamp:%m-%d}" if stamp else ""))
                break
    if moved:
        out += ["▍本期节点"] + moved + [""]
    fresh, seen = [], set()
    for card in snapshot.cards:
        for ref in card.references:
            if ref.is_new and ref.id not in seen:
                seen.add(ref.id)
                fresh.append(ref)
    if fresh:
        fresh.sort(key=lambda r: r.published_at or now, reverse=True)
        out.append(f"▍近 24 小时新增 {len(fresh)} 条")
        for ref in fresh:
            stamp = f"{ref.published_at:%m-%d} " if ref.published_at else ""
            out.append(f"· {stamp}{_safe(ref.title)}")
    return to_plain_text("\n\n".join(out))
