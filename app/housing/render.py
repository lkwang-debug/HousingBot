from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.housing.classify import stage_for_title, stage_label
from app.housing.config import get_housing_settings
from app.housing.forecast import notice_label
from app.housing.models import (
    HousingExitRecord,
    HousingNotice,
    HousingSource,
    HousingSourceState,
    utcnow,
)
from app.housing.qualify import summary_lines

TYPE_LABELS = {"baozu": "保租房", "gongzu": "公租房"}
CATEGORY_LABELS = {
    "rent_launch": "配租启动",
    "lottery_result": "摇号结果",
    "vacancy_exit": "腾退/存量",
    "process": "过程公示",
    "policy": "政策",
    "other_housing": "其他",
}
MAX_CONTENT_CHARS = 18_000
VACANCY_WORDS = ("腾退", "存量房源", "存量住房", "剩余房源", "递补", "再次配租")


def render_instant(
    notices: list[HousingNotice],
    now: datetime | None = None,
    scope: str | None = None,
    detail: bool = False,
    lottery: dict | None = None,
    city_wide: bool = False,
    session: Session | None = None,
) -> tuple[str, str]:
    """scope 是群组名（如"光明区"），只影响标题，用于让买家一眼看出这是推给他的订阅。

    detail=True 是管家版：额外附上资格粗筛。
    lottery 是 notice_id -> LotterySummary，用于摇号结果的"多少人抢多少套"。
    city_wide=True 表示这个群收全市通告（一个总群），标题里必须补上区名，
    否则"深圳 · 管家版 · 摇号结果已公布"看不出是哪个区的事。
    """
    now = now or utcnow()
    prefix = f"{scope} · " if scope else ""
    with_region = city_wide or not scope
    if len(notices) > 1:
        title = f"{prefix}住房通告 {len(notices)} 条"
    elif notices[0].category == "lottery_result":
        # 标题先说"公布了什么"，多少人抢多少套是正文第一行——那是细节，不是标题。
        # 带上批次号，同一个区两个月内两次摇号时买家才分得清是哪一批。
        batch = _batch_label_from_title(notices[0])
        region = (notices[0].region_code or "") if city_wide else ""
        # 区名和批次连写：「光明区2026第1批」是一个批次标识，不是群名。
        head = f"{region}{batch}" if region and batch else (region or batch)
        title = f"{prefix}{head + ' ' if head else ''}公证摇号结果已公布"
    elif stage_for_title(notices[0].title):
        # 流程节点：标题直接写清走到哪一步，买家不用点开才知道。
        stage = stage_for_title(notices[0].title)
        label = notice_label(notices[0], with_region=with_region)
        title = f"{prefix}【{stage_label(stage)}】{label}"[:98]
    else:
        title = f"{prefix}{notices[0].title[: 98 - len(prefix)]}"
    lines = [f"## 🔔 新通告（{len(notices)} 条）", ""]
    if scope:
        # 也写进正文：PushPlus 对"相同内容"有 1 小时 3 条的限制，各群内容必须能区分开。
        lines.extend([f"> 订阅范围：{scope}", ""])
    for index, notice in enumerate(notices, start=1):
        lines.append(f"**{index}. {_link(notice)}**")
        if notice.category == "lottery_result":
            lines.extend(_lottery_bullets(notice, (lottery or {}).get(notice.id), now, session=session))
        elif stage_for_title(notice.title):
            # 核查/选房/签约这些流程节点，买家最想知道"我报的那批走到哪一步了"。
            lines.extend(_stage_bullets(notice, now, detail=detail, session=session))
        else:
            lines.extend(
                f"- {bullet}"
                for bullet in _detail_bullets(notice, now, include_vacancy=True, detail=detail)
            )
            if notice.category == "rent_launch":
                lines.extend(_action_block("applying", notice, now=now, session=session))
        lines.append("")
    return title, _cap("\n".join(lines))


def render_lottery(
    notice: HousingNotice,
    summary=None,
    scope: str | None = None,
) -> tuple[str, str]:
    """单独发一条摇号结果（手动补发或单独订阅时用）。"""
    prefix = f"{scope} · " if scope else ""
    lines = ["## 🎲 摇号结果公布", ""]
    if scope:
        lines.extend([f"> 订阅范围：{scope}", ""])
    lines.append(f"**{_link(notice)}**")
    lines.extend(_lottery_bullets(notice, summary))
    lines.extend(["", "> 点击标题查看官方原文；名单以原文附件为准"])
    return f"{prefix}{_lottery_title(notice, summary, with_region=not scope)}"[:98], _cap(
        "\n".join(lines)
    )


def _batch_label_from_title(notice: HousingNotice) -> str:
    """从标题里抽「2026第1批」；抽不到就返回空，不硬凑。"""
    from app.housing.batches import parse_batch_seq

    title = notice.title or ""
    seq = parse_batch_seq(title)
    year = re.search(r"(20\d{2})\s*年", title)
    if not seq or not year:
        return ""
    return f"{year.group(1)}第{seq}批"


def _lottery_title(notice: HousingNotice, summary, *, with_region: bool = True) -> str:
    """标题要一眼看出"多少人抢多少套"，这是买家点开的唯一理由。

    with_region=False 用于群推送：标题前面已经有"光明区 · 管家版"，再加一次区名是噪音。
    """
    units = None
    applicants = 0
    if summary is not None and getattr(summary, "categories", None):
        known = [item for item in summary.categories if item.units]
        if known:
            units = sum(item.units for item in known)
        applicants = sum(item.applicants for item in summary.categories)
    parts = ["🎲 摇号结果公布"]
    if with_region and notice.region_code:
        parts.append(notice.region_code)
    if units:
        parts.append(f"{units} 套")
    if applicants:
        parts.append(f"{applicants:,} 户")
    parts.append("快看你排第几位")
    if units or applicants:
        return " · ".join(parts)
    return f"🎲 摇号结果公布 · {notice.title[:70]}"


def _lottery_bullets(
    notice: HousingNotice, summary, now: datetime | None = None, session=None
) -> list[str]:
    lines: list[str] = []
    categories = list(getattr(summary, "categories", []) or [])
    if getattr(summary, "parse_error", None):
        lines.append("- 数据提示：名单或批次存在未核实项，估算仅对已核实档位展示")
    if not categories:
        lines.append("- 摇号名单见下方附件；名单以官方原文为准")
        lines.extend(_attachment_lines(notice))
        lines.extend(_action_block("lottery", notice, now=now, session=session))
        return lines

    known = [item for item in categories if item.units]
    total_applicants = sum(item.applicants for item in categories)
    headline = f"- 本批认租 **{total_applicants:,} 户**"
    if known:
        headline += f" · 房源 **{sum(item.units for item in known):,} 套**"
    headline += f" · {len(categories)} 档户型"
    lines.append(headline)
    if known:
        lines.append(
            "- 分档房源："
            + " · ".join(f"{item.label.split('（')[0].strip()} {item.units} 套" for item in known[:5])
        )
    for item in categories[:4]:
        if item.units:
            lines.append(
                f"- {item.label}：房源 {item.units} 套 / 认租 {item.applicants:,} 户"
                f" → 供需比例 {_pct(item.odds)}（不是个人成功概率）"
            )
        else:
            lines.append(f"- {item.label}：认租 {item.applicants:,} 户")
        odds = _queue_lines(item)
        lines.extend(f"  - {line}" for line in odds)
    if len(categories) > 4:
        lines.append(f"- 另有 {len(categories) - 4} 档名单，见附件")
    lines.extend(_attachment_lines(notice))
    lines.extend(_action_block("lottery", notice, now=now, session=session))
    lines.extend(["", "> 摇号顺序不是最终选房顺序：先按第一→第二→第三队列，同队列内再按顺序号"])
    return lines


def _queue_lines(item) -> list[str]:
    from app.housing.forecast import queue_odds

    odds = queue_odds(item.units, item.queue_counts)
    if not odds:
        return []
    lines = []
    for entry in odds[:3]:
        if entry.chance <= 0:
            lines.append(f"{entry.queue} {entry.applicants:,} 户 → 假设无淘汰、无放弃，前序队列已占用全部名额")
        else:
            lines.append(
                f"{entry.queue} {entry.applicants:,} 户 → 假设下的名额比例 {_pct(entry.chance)}"
            )
    return lines


# 批次全流程。买家看通告时缺的正是"这一步之后还有几步"。
FLOW_STEPS = (
    ("applying", "认租"),
    ("review", "资格核查"),
    ("lottery", "摇号"),
    ("selection", "选房"),
    ("signing", "签约"),
)
STAGE_HINTS = {
    "review": "下一步：核查结果里有你，才会进入摇号和选房名单",
    "lottery": "下一步：按队列看待顺序号，选房通告随后发布；顺序号不是最终选房顺序",
    "selection": "下一步：按通告时间参加选房，逾期视为放弃",
    "signing": "下一步：按通告时间签约并办理入住，逾期视为放弃",
}
# 早报里每个在途批次的一行"今天要做什么"。
DIGEST_TODOS = {
    "announced": "等开始，别错过",
    "applying": "没交的尽快交",
    "review": "在名单里等下一步，不在的尽快复核",
    "lottery": "用认租编号查排位",
    "selection": "按场次到场，逾期视为放弃",
    "signing": "已选房的去签约",
    "closed": "本批已结束",
}


def _flow_steps(housing_types) -> tuple[tuple[str, str], ...]:
    """公租房按轮候排序选房，没有公证摇号这一步；保租房才有。"""
    if "gongzu" in (housing_types or []):
        return tuple(step for step in FLOW_STEPS if step[0] != "lottery")
    return FLOW_STEPS


def _stage_timeline(stage: str, housing_types=None) -> str:
    steps = _flow_steps(housing_types)
    index = next(
        (position for position, (key, _) in enumerate(steps) if key == stage), -1
    )
    marks = []
    for position, (_, label) in enumerate(steps):
        mark = "🔵" if position == index else ("✅" if position < index else "⚪")
        marks.append(f"{label}{mark}")
    return " → ".join(marks)


def _is_gongzu(notice: HousingNotice, housing_types=None) -> bool:
    types = list(housing_types or notice.housing_types or [])
    if "gongzu" in types:
        return True
    # 分类没抽出房型时用标题兜底：公租房没有公证摇号，查询页里没有它的数据。
    return "公共租赁住房" in (notice.title or "")


def lookup_url_for(now: datetime | None = None) -> str:
    """Use a signed Beijing calendar-day bearer after verified cloud activation."""
    settings = get_housing_settings()
    url = (getattr(settings, "lookup_url", "") or "").strip()
    if not url:
        return ""
    day = (now or utcnow()).strftime("%Y%m%d")
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key not in {"d", "t"}]
    if getattr(settings, 'lookup_access_mode', 'legacy') == 'signed':
        from app.housing.lookup_tokens import issue_token
        secret = settings.lookup_signing_secret.get_secret_value()
        query.append(('t', issue_token(secret, now)))
    else:
        query.append(("d", day))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _lookup_link(
    stage: str,
    notice: HousingNotice,
    housing_types=None,
    now: datetime | None = None,
    session=None,
) -> str:
    """查询页只有保租房的公证摇号数据；公租房按轮候排序，没有摇号，给了也查不到。"""
    from app.housing.lookup import published_ready
    if not published_ready(notice, session=session):
        return ""
    url = lookup_url_for(now)
    if not url:
        return ""
    if stage == "selection":
        # 只有保租房的选房名单和摇号排位对得上；公租房、出售型都不给。
        types = list(housing_types or notice.housing_types or [])
        if "baozu" not in types:
            return ""
    return url


def _action_block(
    stage: str,
    notice: HousingNotice,
    housing_types=None,
    now: datetime | None = None,
    session=None,
) -> list[str]:
    """同一个群里有没认租的、认租没摇号的、摇完的、进了选房名单的。

    广播做不到按人发，就把"你现在要做的"写成带条件的几条：对号入座，不相关就跳过。
    """
    is_gongzu = _is_gongzu(notice, housing_types)
    url = _lookup_link(stage, notice, housing_types, now, session=session)
    lines = ["", "**你现在要做的**"]
    if stage == "applying":
        lines += [
            "- 符合条件 → 尽快提交，提交后截图保存**认租编号**（后面查排位要用）",
            "- 不确定条件 → 先看原文里的配租对象和项目清单",
            "- 不打算申请 → 忽略本条，不影响你现有的轮候排位",
        ]
    elif stage == "review":
        lines.append(
            "- 在合格名单里 → 不用操作，等" + ("选房名单" if is_gongzu else "摇号结果")
        )
        lines.append("- 不在名单里 → 按通告写的截止时间提交复核，逾期视为放弃")
        lines.append("- 没参加本批认租 → 忽略本条")
    elif stage == "lottery":
        if url:
            lines.append(
                f"- 认租了 → 输入**认租编号**，看队列/顺序号/排位：[点这里查询]({url})"
            )
        else:
            lines.append("- 认租了 → 点开附件名单，用**认租编号**找自己的顺序号")
        lines.append("- 没认租 → 忽略本条，下批通告会单独推")
        lines.append("- 已收到选房通知 → 按选房通告的时间到场")
    elif stage == "selection":
        lines.append("- 在选房名单里 → 按附件场次到场，提前 30 分钟，带身份证和认租回执")
        if url:
            lines.append(f"- 想知道自己排第几 → [点这里查询]({url})")
        lines.append("- 不在名单里 → 本批不再补录，等下一批通告")
        lines.append("- 没参加本批认租 → 忽略本条")
    elif stage == "signing":
        lines.append("- 已选房 → 按通告时间签约、办理入住，逾期视为放弃")
        lines.append("- 不在本批选房名单里 → 忽略本条")
    else:
        return []
    return lines


def _stage_bullets(
    notice: HousingNotice, now: datetime, detail: bool = False, session=None
) -> list[str]:
    stage = stage_for_title(notice.title)
    if not stage:
        return [f"- {bullet}" for bullet in _detail_bullets(notice, now, detail=detail)]
    steps = _flow_steps(notice.housing_types)
    step = next(
        (position + 1 for position, (key, _) in enumerate(steps) if key == stage), 0
    )
    lines = [
        f"- 批次进度：**{stage_label(stage)}**（第 {step}/{len(steps)} 步）",
        f"- 流程：{_stage_timeline(stage, notice.housing_types)}",
    ]
    lines.extend(f"- {bullet}" for bullet in _detail_bullets(notice, now, detail=detail))
    hint = STAGE_HINTS.get(stage)
    if hint:
        lines.append(f"- {hint}")
    lines.extend(_action_block(stage, notice, now=now, session=session))
    return lines


def _attachment_lines(notice: HousingNotice, limit: int = 6) -> list[str]:
    lines: list[str] = []
    for attachment in (notice.attachments or [])[:limit]:
        url = str(attachment.get("url") or "")
        name = str(attachment.get("name") or "")
        if not url or not name:
            continue
        safe = name.replace("[", "【").replace("]", "】")
        lines.append(f"- [📄 {safe[:60]}]({url})")
    return lines


def _pct(value: float | None) -> str:
    if value is None:
        return "未知"
    value = max(0.0, min(1.0, value))
    if value >= 1:
        return "100%"
    if value >= 0.1:
        return f"{value * 100:.0f}%"
    return f"{value * 100:.1f}%"


def render_deadline(
    notice: HousingNotice,
    days_left: int,
    now: datetime | None = None,
    scope: str | None = None,
    session=None,
) -> tuple[str, str]:
    """截止前提醒。这是整条产品线里时效价值最高的消息。"""
    now = now or utcnow()
    when = "今天" if days_left <= 0 else ("明天" if days_left == 1 else f"{days_left} 天后")
    end_text = notice.application_end.strftime("%m-%d %H:%M") if notice.application_end else "未写明"
    title = f"⏰ {when}截止 · {_glue_region(notice.region_code or '', notice.title[:60])}"
    lines = [f"## ⏰ 认租{when}截止", ""]
    if scope:
        lines.extend([f"> 订阅范围：{scope}", ""])
    lines.append(f"**{_link(notice)}**")
    facts = [f"截止 {end_text}"]
    if notice.units_count:
        facts.append(f"{notice.units_count} 套")
    projects = _projects_text(notice)
    if projects:
        facts.append(f"项目：{projects}")
    lines.extend(f"- {line}" for line in facts)
    if not notice.application_start:
        lines.append("- 开始时间未写明，以原文为准")
    lines.extend(["- 还没提交的，今天就要办", "", "> 点击标题查看原文和申请入口"])
    return title[:98], _cap("\n".join(lines))


def render_stage(
    batch,
    notice: HousingNotice,
    scope: str | None = None,
    now: datetime | None = None,
    session=None,
) -> tuple[str, str]:
    """批次进度推进：告诉买家"你报的那批走到下一步了"。"""
    now = now or utcnow()
    # 原文标题基本都以区名开头，再加一次就成了「南山区 【选房】南山区…」。
    prefix = f"{scope} · " if scope else ""
    head = _glue_region(batch.region or "", f"【{batch.stage_label}】{notice.title[:56]}")
    title = f"{prefix}📌 {head}"
    lines = [f"## 📌 批次进度：{batch.stage_label}", ""]
    if scope:
        lines.extend([f"> 订阅范围：{scope}", ""])
    lines.append(f"**{_link(notice)}**")
    lines.append(f"- 批次：{_short_title(batch.title)}")
    batch_types = (
        (batch.launch.housing_types if batch.launch else None)
        or batch.stage_notice.housing_types
    )
    lines.append(f"- 流程：{_stage_timeline(batch.stage, batch_types)}")
    if batch.launch and batch.launch.application_end:
        lines.append(
            f"- 认租已于 {batch.launch.application_end:%m-%d %H:%M} 结束"
            f"（{batch.launch.units_count or '若干'} 套）"
        )
    stage_hint = STAGE_HINTS.get(batch.stage)
    if stage_hint:
        lines.append(f"- {stage_hint}")
    lines.extend(_action_block(batch.stage, notice, batch_types, now=now, session=session))
    lines.extend(["", "> 点击标题查看原文，名单和时间以原文为准"])
    return title[:98], _cap("\n".join(lines))


def render_digest(
    session: Session,
    now: datetime | None = None,
    scope: str | None = None,
    detail: bool = False,
    regions: tuple[str, ...] = (),
) -> tuple[str, str]:
    now = now or utcnow()
    today = now.date()
    lines = [f"## 🏠 深圳住房保障早报 {today:%Y-%m-%d}", ""]
    if scope:
        lines.extend([f"> 订阅范围：{scope}", ""])

    open_notices = _open_notices(session, now, regions)
    lines.append(f"**⏰ 正在认租（{len(open_notices)} 批）**")
    if open_notices:
        for index, notice in enumerate(open_notices, start=1):
            lines.append(f"{index}. {_link(notice)}")
            lines.extend(f"   - {bullet}" for bullet in _detail_bullets(notice, now))
    else:
        lines.append("- 暂无正在接受认租的批次")
    lines.append("")

    if detail:
        tracked = _tracked_batches(session, now, regions)
        if tracked:
            lines.append(f"**📌 在途批次（{len(tracked)} 批）**")
            for batch in tracked:
                lines.append(_batch_progress_line(batch, now, session=session))
            lines.append("")

    lotteries = _recent_lotteries(session, now - timedelta(days=7), regions)
    if lotteries:
        from app.housing.lottery import summary_map

        summaries = summary_map(session, lotteries)
        lines.append(f"**🎲 摇号结果（近 7 天 {len(lotteries)} 批）**")
        for notice in lotteries:
            lines.append(f"- {_link(notice)}{_lottery_digest_extra(summaries.get(notice.id))}")
        lines.append("")

    recent = _recent_notices(session, now - timedelta(hours=24), regions)
    lines.append(f"**📢 近 24 小时新增（{len(recent)} 条）**")
    if recent:
        for notice in recent:
            label = CATEGORY_LABELS.get(notice.category, notice.category)
            lines.append(f"- {_link(notice)} · {label}")
    else:
        lines.append("- 无")
    lines.append("")

    vacancies = _recent_vacancies(session, now - timedelta(days=30), regions)
    exit_rows = _exit_summary(session, regions)
    lines.append(f"**♻️ 腾退与存量（近 30 天 {len(vacancies)} 条）**")
    for notice in vacancies:
        lines.append(f"- {_link(notice)}")
        evidence = _vacancy_line(notice)
        if evidence:
            lines.append(f"   - {evidence.lstrip('- ')}")
    if exit_rows:
        lines.append("- 历史退出信息按小区汇总（仅社区级，不含个人信息）：")
        for community, housing_type, count in exit_rows:
            lines.append(f"   - {community}（{housing_type or '房型未标注'}）：{count} 套/条")
    if not vacancies and not exit_rows:
        lines.append("- 未发现腾退类通告；市级退出信息更新至 2024 上半年，不作为实时信号")
    lines.append("")

    lines.append(f"**📶 数据源**：{_source_status(session).lstrip('- ')}")
    lines.append("")

    # 查询入口：早报是唯一每天必发的消息，入口放这里才不会漏。
    # 只在早报里加——截止提醒时还没摇号，给了链接也查不到。
    from app.housing.lookup import published_ready
    lookup_url = lookup_url_for(now) if published_ready(session=session) else ""
    if lookup_url:
        lines.append(f"🔎 认租编号查排位：[点这里查询]({lookup_url})")
        if lotteries:
            ready = "、".join(
                _queryable_label(notice) for notice in lotteries if published_ready(notice, session=session)
            )
            if ready:
                lines.append(f"   已登记发布可查：{ready}")
            else:
                lines.append("   新批次查询页尚待人工上传，先看官方附件")
        else:
            lines.append("   入口为最近一次登记发布的版本；新批次以可查提示为准")
        lines.append("")

    lines.append("> 点击标题查看原文")
    return f"深圳住房保障早报 {today:%m-%d}", _cap("\n".join(lines))


def _queryable_label(notice: HousingNotice) -> str:
    """“光明区 2026第1批”：让人一眼对上自己那批。"""
    batch = _batch_label_from_title(notice)
    region = notice.region_code or ""
    return f"{region} {batch}".strip() or region or notice.title[:20]


def _glue_region(region: str, text: str) -> str:
    """区名只在标题里还没有的时候补一次，并且和后面的批次/年份连写。

    政府原文标题基本都以区名开头（「深圳市龙华区住房和建设局关于…」），
    再前置一次就成了「龙华区 深圳市龙华区…」；而「光明区 2026第1批」这种
    分开写会让区名看着像群名，连成「光明区2026第1批」才是批次标识。
    """
    region = (region or "").strip()
    text = (text or "").strip()
    if not region or not text or region in text:
        return text
    return f"{region}{text}" if text[0].isdigit() else f"{region} {text}"


def _link(notice: HousingNotice) -> str:
    """每条通告都必须是可点链接，微信里点标题直接看原文。"""
    title = (notice.title or "(无标题)").replace("[", "【").replace("]", "】")
    return f"[{title}]({notice.canonical_url})"


def _projects_text(notice: HousingNotice) -> str:
    """历史入库的 projects 里混过“具体”“详见附件”这类词，渲染时再过滤一次。"""
    from app.housing.parsers.table import filter_projects

    names = filter_projects(notice.projects)
    if not names:
        return ""
    shown = "、".join(names[:3])
    if len(names) > 3:
        shown += f" 等 {len(names)} 个"
    return shown


def _short_title(title: str | None, limit: int = 40) -> str:
    text = (title or "(无标题)").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _detail_bullets(
    notice: HousingNotice,
    now: datetime,
    include_vacancy: bool = False,
    detail: bool = False,
) -> list[str]:
    """认租批次的要点：类型/套数、截止时间、项目名。"""
    facts: list[str] = []
    types = "、".join(TYPE_LABELS.get(item, item) for item in (notice.housing_types or []))
    if types:
        facts.append(types)
    if notice.region_code:
        facts.append(notice.region_code)
    if notice.units_count:
        facts.append(f"{notice.units_count} 套")
    if notice.doc_number:
        facts.append(notice.doc_number)

    bullets: list[str] = []
    if facts:
        bullets.append(" · ".join(facts))
    if notice.application_end:
        start = notice.application_start.strftime("%m-%d %H:%M") if notice.application_start else "开始时间未核实"
        end = notice.application_end.strftime("%m-%d %H:%M")
        bullets.append(f"认租 {start} → {end}（{_remaining_text(notice.application_end, now)}）")
    elif notice.category == "rent_launch":
        bullets.append("认租时间：见原文/附件")
    projects = _projects_text(notice)
    if projects:
        bullets.append(f"项目：{projects}")
    if include_vacancy:
        vacancy = _vacancy_line(notice)
        if vacancy:
            bullets.append(vacancy.lstrip("- "))
    if detail:
        bullets.extend(summary_lines(notice))
    return bullets


def _meta_line(notice: HousingNotice) -> str:
    types = "、".join(TYPE_LABELS.get(item, item) for item in (notice.housing_types or [])) or "未标注"
    parts = [f"区域：{notice.region_code or '未标注'}", f"类型：{types}"]
    if notice.doc_number:
        parts.append(f"文号：{notice.doc_number}")
    if notice.units_count:
        parts.append(f"套数：{notice.units_count}")
    projects = _projects_text(notice)
    if projects:
        parts.append(f"项目：{projects}")
    return "- " + " | ".join(parts)


def _window_line(notice: HousingNotice, now: datetime) -> str:
    if not notice.application_start and not notice.application_end:
        if notice.category == "rent_launch":
            return "- 认租时间：见原文/附件"
        return ""
    start = notice.application_start.strftime("%m-%d %H:%M") if notice.application_start else "开始时间未核实"
    end = notice.application_end.strftime("%m-%d %H:%M") if notice.application_end else "未写明"
    suffix = f"（{_remaining_text(notice.application_end, now)}）" if notice.application_end else ""
    inferred = "（时间为推断）" if notice.time_inferred else ""
    return f"- 认租：{start} ~ {end}{suffix}{inferred}"


def _remaining_text(end: datetime | None, now: datetime) -> str:
    if end is None:
        return "截止时间未写明"
    days = (end.date() - now.date()).days
    if days < 0:
        return "已截止"
    if days == 0:
        return "今日截止"
    if days == 1:
        return "明日截止"
    return f"剩余 {days} 天"


def _vacancy_line(notice: HousingNotice) -> str:
    body = notice.body_text or ""
    if not body:
        return ""
    for word in VACANCY_WORDS:
        index = body.find(word)
        if index < 0:
            continue
        snippet = " ".join(body[max(0, index - 40) : index + 60].split())
        if len(snippet) > 90:
            snippet = snippet[:90] + "…"
        return f"- 腾退/存量：…{snippet}"
    return ""


def _region_scope(regions: tuple[str, ...] | list[str] | None):
    """早报按群覆盖的区过滤；口径和 SubscribeGroup.covers() 一致。

    市级通告面向全市，所有区群都要发——否则只订一个区的人会漏掉全市批次。
    """
    names = tuple(name for name in (regions or ()) if name)
    if not names:
        return None
    return or_(HousingNotice.region_code.in_(names), HousingNotice.level == "city")


def _open_notices(
    session: Session, now: datetime, regions: tuple[str, ...] = ()
) -> list[HousingNotice]:
    statement = select(HousingNotice).where(
        HousingNotice.is_target.is_(True),
        HousingNotice.category == "rent_launch",
        HousingNotice.application_end.is_not(None),
        HousingNotice.application_end >= now,
        or_(HousingNotice.application_start.is_(None), HousingNotice.application_start <= now),
    )
    scope = _region_scope(regions)
    if scope is not None:
        statement = statement.where(scope)
    return list(
        session.scalars(
            statement.order_by(HousingNotice.application_end.asc()).limit(15)
        ).all()
    )


def _batch_progress_line(batch, now: datetime | None = None, session=None) -> str:
    """一人一行：先找到自己那一批，再看今天要不要动。"""
    label = _batch_short_label(batch)
    notice = batch.stage_notice
    url = getattr(notice, "canonical_url", "") or ""
    head = f"[{label}]({url})" if url else label
    todo = DIGEST_TODOS.get(batch.stage, "看原文")
    # 走到摇号/选房这一步才有名单可查；查不到就不给死链。
    lookup = _lookup_link(batch.stage, notice, now=now, session=session)
    if lookup and batch.stage in {"lottery", "selection"}:
        todo = f"[{todo}]({lookup})"
    return f"- {head}｜{batch.stage_label}｜{_batch_when(batch)}｜{todo}"


def _batch_short_label(batch):
    from app.housing.batches import parse_batch_seq
    from app.housing.qualify import audience
    notice = batch.launch or batch.stage_notice
    year = re.search(r"(20\d{2})\s*年", notice.title or "")
    seq = parse_batch_seq(notice.title or "")
    types = "、".join(TYPE_LABELS.get(t, t) for t in notice.housing_types or [])
    target = {"personal": "个人", "enterprise": "企业", "unknown": "对象待核实"}[audience(notice)]
    return " ".join(p for p in [notice.region_code, year.group(1) if year else "",
        f"第{seq}批" if seq else "", types, target] if p)


def _batch_when(batch) -> str:
    if batch.stage in {"announced", "applying"} and batch.application_end:
        return f"{batch.application_end:%m-%d %H:%M} 截止"
    notice_end = getattr(batch.stage_notice, "application_end", None)
    if notice_end:
        return f"{notice_end:%m-%d} 前"
    return "时间见原文"


def _tracked_batches(session: Session, now: datetime, regions: tuple[str, ...] = ()) -> list:
    """只列还在流程里、且最近有动作的批次；已结束的不占版面。"""
    from app.housing.batches import build_batches

    names = tuple(name for name in (regions or ()) if name)
    limit_at = now - timedelta(days=30)
    tracked = []
    for batch in build_batches(session, since_days=120, now=now):
        if batch.stage not in {"review", "lottery", "selection", "signing"}:
            continue
        stamp = batch.stage_notice.published_at or batch.stage_notice.first_seen_at
        if stamp and stamp >= limit_at:
            if names and (batch.stage_notice.level or "") != "city":
                if (batch.stage_notice.region_code or "") not in names:
                    continue
            tracked.append(batch)
    tracked.sort(key=lambda item: item.stage_notice.published_at or now, reverse=True)
    return tracked[:6]


def _published_or_seen():
    """按发布日期判断“新增”，首次采集的历史回填不应算进当日新增。"""
    return func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)


def _recent_notices(
    session: Session, since: datetime, regions: tuple[str, ...] = ()
) -> list[HousingNotice]:
    statement = select(HousingNotice).where(
        HousingNotice.is_target.is_(True),
        _published_or_seen() >= since,
        HousingNotice.published_unverified.is_(False),
    )
    scope = _region_scope(regions)
    if scope is not None:
        statement = statement.where(scope)
    return list(
        session.scalars(
            statement.order_by(
                HousingNotice.priority.desc(), HousingNotice.published_at.desc()
            ).limit(20)
        ).all()
    )


def _recent_lotteries(
    session: Session, since: datetime, regions: tuple[str, ...] = ()
) -> list[HousingNotice]:
    statement = select(HousingNotice).where(
        HousingNotice.category == "lottery_result",
        HousingNotice.is_target.is_(True),
        _published_or_seen() >= since,
        HousingNotice.published_unverified.is_(False),
    )
    scope = _region_scope(regions)
    if scope is not None:
        statement = statement.where(scope)
    return list(
        session.scalars(statement.order_by(HousingNotice.published_at.desc()).limit(6)).all()
    )


def _lottery_digest_extra(summary) -> str:
    if summary is None:
        return ""
    known = [item for item in summary.categories if item.units]
    if not known:
        return ""
    item = known[0]
    return f" · {item.label.split('（')[0].strip()} {item.units} 套 / {item.applicants:,} 户"


def _recent_vacancies(
    session: Session, since: datetime, regions: tuple[str, ...] = ()
) -> list[HousingNotice]:
    statement = select(HousingNotice).where(
        HousingNotice.is_target.is_(True),
        HousingNotice.category == "vacancy_exit",
        _published_or_seen() >= since,
        HousingNotice.published_unverified.is_(False),
    )
    scope = _region_scope(regions)
    if scope is not None:
        statement = statement.where(scope)
    return list(
        session.scalars(statement.order_by(HousingNotice.published_at.desc()).limit(10)).all()
    )


def _exit_summary(
    session: Session, regions: tuple[str, ...] = ()
) -> list[tuple[str, str | None, int]]:
    statement = select(
        HousingExitRecord.community_name,
        HousingExitRecord.housing_type,
        func.count(HousingExitRecord.id),
    )
    names = tuple(name for name in (regions or ()) if name)
    if names:
        statement = statement.where(HousingExitRecord.region_code.in_(names))
    rows = session.execute(
        statement.group_by(HousingExitRecord.community_name, HousingExitRecord.housing_type)
        .order_by(func.count(HousingExitRecord.id).desc())
        .limit(10)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows if row[0]]


def _source_status(session: Session) -> str:
    rows = session.execute(
        select(HousingSource.key, HousingSourceState.last_verify_status, HousingSourceState.last_error)
        .join(HousingSourceState, HousingSourceState.source_id == HousingSource.id, isouter=True)
        .where(HousingSource.enabled.is_(True))
    ).all()
    failed = [row for row in rows if row[1] and row[1] != "ok"]
    ok_count = len(rows) - len(failed)
    if not failed:
        return f"- 全部 {ok_count} 个源正常"
    detail = "；".join(f"{row[0]}（{row[1]}）" for row in failed[:5])
    return f"- 正常 {ok_count} 个，异常 {len(failed)} 个：{detail}"


def _cap(content: str) -> str:
    if len(content) <= MAX_CONTENT_CHARS:
        return content
    return content[: MAX_CONTENT_CHARS - 20] + "\n\n…（内容过长已截断）"


# Classified city-wide layouts used by the three scheduled delivery paths.
# Earlier single-notice render helpers remain available for compatibility.
def render_instant_parts(notices, *, session, now=None, updated_notice_ids=(), **_):
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_report_parts
    return render_report_parts(build_snapshot(session, "instant", now=now, notices=notices, updated_notice_ids=updated_notice_ids))


def render_digest_parts(session, now=None):
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_report_parts
    return render_report_parts(build_snapshot(session, "digest", now=now))


def render_evening_parts(session, now=None):
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_report_parts
    return render_report_parts(build_snapshot(session, "evening", now=now))


def render_group_digest(session, *, evening=False, max_chars=1000, now=None):
    """群聊短版：纯文本，可直接粘贴到有字数上限的群聊。"""
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_group_text
    kind = "evening" if evening else "digest"
    return render_group_text(build_snapshot(session, kind, now=now), max_chars=max_chars)


def render_group_full(session, *, evening=False, max_chars=1000, keep_links=False, now=None):
    """群聊全量版：完整报表拆成多条，逐条发送，不丢内容。"""
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_group_full as _full
    kind = "evening" if evening else "digest"
    return _full(build_snapshot(session, kind, now=now), max_chars=max_chars, keep_links=keep_links)
