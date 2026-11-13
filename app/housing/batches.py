"""批次追踪：把同一批配租的多个通告（配租→摇号→选房→签约）串成一条时间线。

没有这个视角，用户看到的是散落的通告；有了它，才知道"我报的那批现在走到哪一步"。
批次由启动公告锚点和持久化关联确定；投递记录独立保存在每群事件中。
"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.housing.models import HousingNotice, HousingBatchAnchor, utcnow

STAGE_RANK = {
    "announced": 1,
    "applying": 2,
    "review": 3,
    "lottery": 4,
    "selection": 5,
    "signing": 6,
    "closed": 7,
}
STAGE_LABELS = {
    "announced": "已发布",
    "applying": "认租中",
    "review": "资格核查",
    "lottery": "摇号结果",
    "selection": "选房",
    "signing": "签约",
    "closed": "已结束",
}
# 买家关心的完整流程；早报和进度推送都按这个顺序展示。
STAGE_FLOW = ("applying", "review", "lottery", "selection", "signing")

CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
BATCH_SEQ_RE = re.compile(r"第\s*([一二三四五六七八九十\d]{1,3})\s*批(?:次)?")
YEAR_RE = re.compile(r"(20\d{2})\s*年")

SIGNING_TERMS = ("签约", "办理入住", "签订", "签署")
LOTTERY_TERMS = ("摇号",)
SELECTION_TERMS = ("选房", "入围", "排序")
REVIEW_TERMS = ("资格核查", "核查结果", "审核结果", "初审结果", "资格审查", "终审", "复核", "认租资格")


@dataclass
class Batch:
    key: str
    region: str
    title: str
    stage: str
    stage_notice: HousingNotice
    launch: HousingNotice | None = None
    notices: list[HousingNotice] = field(default_factory=list)

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    @property
    def application_end(self) -> datetime | None:
        return self.launch.application_end if self.launch else None


def parse_batch_seq(value: str) -> int | None:
    # A second/third selection round is not the original allocation batch.
    match = next((m for m in BATCH_SEQ_RE.finditer(value)
                  if not re.match(r"\s*(?:选房|签约)", value[m.end():])), None)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    total = 0
    for char in token:
        if char == "十":
            total = (total or 1) * 10
        elif char in CN_DIGITS:
            total += CN_DIGITS[char]
        else:
            return None
    return total or None


def batch_key(notice: HousingNotice) -> str | None:
    """Only persisted, unambiguous links are eligible for forecasts."""
    if notice.batch_key_v2:
        return notice.batch_key_v2
    if notice.category == "rent_launch" and notice.canonical_key:
        return "v2:" + hashlib.sha256(notice.canonical_key.encode()).hexdigest()[:32]
    return None


def _signature(notice):
    title = notice.title or ""
    seq = parse_batch_seq(title)
    year_match = YEAR_RE.search(title)
    year = int(year_match.group(1)) if year_match else None
    if year is None and notice.published_at:
        year = notice.published_at.year
    return year, seq


def rebuild_links(session: Session, *, force: bool = False) -> dict:
    """Resolve against launch anchors. Ambiguity is visible, never guessed away."""
    from app.housing.qualify import audience
    notices = list(session.scalars(select(HousingNotice).where(HousingNotice.is_target.is_(True))))
    launches = [n for n in notices if n.category == "rent_launch"]
    anchors = {a.launch_id: a for a in session.scalars(select(HousingBatchAnchor))}
    for launch in launches:
        key = "v2:" + hashlib.sha256(launch.canonical_key.encode()).hexdigest()[:32]
        anchor = anchors.get(launch.id)
        if anchor is None:
            anchor = HousingBatchAnchor(key=key, launch_id=launch.id)
            session.add(anchor)
        anchor.region = launch.region_code
        anchor.housing_type = _primary_type(launch)
        anchor.audience = audience(launch)
        anchor.year, anchor.seq = _signature(launch)
        anchor.doc_number = launch.doc_number
        launch.batch_key_v2, launch.batch_link_status = key, "anchor"
    session.flush()
    linked = unresolved = 0
    for notice in notices:
        if notice.category == "rent_launch":
            continue
        if notice.batch_link_status == "manual":
            continue
        key, reason = resolve_launch(notice, launches)
        previous = notice.batch_key_v2
        notice.batch_key_v2, notice.batch_link_status = key, reason
        if previous != key:
            from app.housing.models import HousingLotteryStat
            session.execute(update(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id)
                            .values(batch_key=key or ""))
        linked += bool(key)
        unresolved += not bool(key)
    session.flush()
    return {"anchors": len(launches), "linked": linked, "unresolved": unresolved}


def resolve_launch(notice: HousingNotice, launches: list[HousingNotice]):
    from app.housing.qualify import audience
    kind = audience(notice)
    year, seq = _signature(notice)
    candidates = [n for n in launches if n.region_code == notice.region_code
                  and _primary_type(n) == _primary_type(notice)]
    # Same-day, same-title official mirror launches are one anchor candidate.
    # Different documents, audiences and publication episodes remain separate.
    from app.housing.dedup import normalize_title, clean_title
    mirrors={}
    for candidate in sorted(candidates,key=lambda n:n.id):
        attachment_ids=[]
        for item in candidate.attachments or []:
            url=item.get('url','');file_id=re.search(r'/attachment/\d+/\d+/(\d+)/',url)
            attachment_ids.append(file_id[1] if file_id else url)
        proof=('document',candidate.doc_number) if candidate.doc_number else ('attachments',tuple(sorted(attachment_ids))) if attachment_ids else ('unproven',candidate.id)
        identity=(candidate.region_code,_primary_type(candidate),audience(candidate),candidate.doc_number,
            normalize_title(clean_title(candidate.title)),candidate.published_at.date() if candidate.published_at else ('unknown',candidate.id),proof)
        mirrors.setdefault(identity,candidate)
    candidates=list(mirrors.values())
    # An explicit launch reference may resolve an unknown audience, but never a conflict.
    text = (notice.title or "") + "\n" + (notice.body_text or "")
    explicit = [n for n in candidates if n.doc_number and n.doc_number in text
                and (kind == "unknown" or audience(n) in {kind, "unknown"})]
    if len(explicit) == 1:
        return batch_key(explicit[0]), "reference"
    if explicit:
        return None, "ambiguous"
    # Many official follow-up notices quote the complete launch title, without
    # repeating its document number. This is direct evidence, not fuzzy matching.
    from app.housing.dedup import normalize_title
    cited = []
    citation_conflict = False
    for match in re.finditer(r"《([^》]{8,500})》", text):
        title = normalize_title(match.group(1)).removeprefix("深圳市")
        prefix = re.sub(r"\s+", "", text[max(0, match.start() - 60):match.start()])
        stamp = re.search(r"(20\d{2}年\d{1,2}月\d{1,2}日)(?:发布|印发|公布)(?:了|的)?$", prefix)
        try:
            published = datetime.strptime(stamp.group(1), "%Y年%m月%d日").date() if stamp else None
        except ValueError:
            continue  # Invalid reference dates are not reliable association evidence.
        for launch in candidates:
            if normalize_title(launch.title).removeprefix("深圳市") != title:
                continue
            if kind != "unknown" and audience(launch) not in {kind, "unknown"}:
                citation_conflict = True
                continue
            if published and (not launch.published_at or launch.published_at.date() != published):
                continue
            if notice.published_at and launch.published_at and launch.published_at > notice.published_at:
                continue
            if launch not in cited:
                cited.append(launch)
    if citation_conflict:
        return None, "ambiguous"
    if len(cited) == 1:
        return batch_key(cited[0]), "title-reference"
    if cited:
        return None, "ambiguous"
    if kind == "unknown" or _primary_type(notice) == "unknown":
        return None, "unresolved"
    candidates = [n for n in candidates if audience(n) == kind]
    if seq is not None and year is not None:
        matches = [n for n in candidates if _signature(n) == (year, seq)]
        return (batch_key(matches[0]), "signature") if len(matches) == 1 else (None, "ambiguous" if matches else "unresolved")
    if seq is None and BATCH_SEQ_RE.search(notice.title or ""):
        return None, "unresolved"  # Only a selection/signing round was present.
    stamp = notice.published_at
    scored = []
    for launch in candidates:
        if not stamp or not launch.published_at or abs((stamp - launch.published_at).days) > 180:
            continue
        score = SequenceMatcher(None, notice.title or "", launch.title or "").ratio()
        scored.append((score, launch))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored and scored[0][0] >= .8 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= .1):
        return batch_key(scored[0][1]), "similarity"
    return None, "unresolved"


def assign_batch(session, notice_id: int, launch_id: int):
    notice, launch = session.get(HousingNotice, notice_id), session.get(HousingNotice, launch_id)
    if not notice or not launch or launch.category != "rent_launch":
        raise ValueError("必须指定存在的公告和配租启动公告")
    from app.housing.qualify import audience
    if notice.region_code != launch.region_code or not _compatible_type(notice, launch):
        raise ValueError("不能跨区域或住房类型关联")
    if audience(notice) != "unknown" and audience(launch) != "unknown" and audience(notice) != audience(launch):
        raise ValueError("不能关联个人和企业批次")
    notice.batch_key_v2, notice.batch_link_status = batch_key(launch), "manual"
    from app.housing.models import HousingLotteryStat
    for stat in session.scalars(select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice_id)):
        stat.batch_key = notice.batch_key_v2
    session.flush()
    return {"notice_id": notice_id, "launch_id": launch_id, "batch": notice.batch_key_v2}


def detect_stage(notice: HousingNotice, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    title = notice.title or ""
    if any(term in title for term in LOTTERY_TERMS):
        return "lottery"
    # "选房名单及选房签约"当下是选房；把签约排在前面会把进度多说一步。
    if any(term in title for term in SELECTION_TERMS):
        return "selection"
    if any(term in title for term in SIGNING_TERMS):
        return "signing"
    if any(term in title for term in REVIEW_TERMS):
        return "review"
    if notice.category == "rent_launch":
        # 认租窗口结束不等于批次结束（后面还有核查、选房、签约），
        # 所以这里只判"认租中"，"已结束"留给批次整体判断。
        if notice.application_start and notice.application_start > now:
            return "announced"
        return "applying"
    return None


def build_batches(
    session: Session, since_days: int = 180, now: datetime | None = None
) -> list[Batch]:
    now = now or utcnow()
    since = now - timedelta(days=since_days)
    seen_at = func.coalesce(HousingNotice.published_at, HousingNotice.first_seen_at)
    notices = list(
        session.scalars(
            select(HousingNotice)
            .where(HousingNotice.is_target.is_(True), seen_at >= since)
            .order_by(seen_at.asc())
        ).all()
    )

    grouped: dict[str, list[HousingNotice]] = {}
    orphans: list[HousingNotice] = []
    for notice in notices:
        key = batch_key(notice)
        if key is None:
            orphans.append(notice)
        else:
            grouped.setdefault(key, []).append(notice)

    # Unresolved announcements remain visible, but cannot borrow another batch's units.
    for notice in orphans:
        grouped[f"unresolved:{notice.id}"] = [notice]

    batches: list[Batch] = []
    for key, items in grouped.items():
        staged = [(detect_stage(item, now), item) for item in items]
        staged = [(stage, item) for stage, item in staged if stage]
        if not staged:
            continue
        stage, stage_notice = max(staged, key=lambda pair: STAGE_RANK[pair[0]])
        launch = next((item for item in items if item.category == "rent_launch"), None)
        if stage == "applying" and launch and launch.application_end:
            if launch.application_end < now - timedelta(days=60):
                stage = "closed"
        batches.append(
            Batch(
                key=key,
                region=items[0].region_code or "",
                title=(launch or stage_notice).title or "",
                stage=stage,
                stage_notice=stage_notice,
                launch=launch,
                notices=items,
            )
        )
    return batches


ORPHAN_SIMILARITY = 0.6


def _attach_orphans(
    grouped: dict[str, list[HousingNotice]], orphans: list[HousingNotice]
) -> None:
    """没有批次号也没有文号的通告，按标题相似度挂到同区最近的批次上。

    同批次的标题相似度实测都在 0.6 以上（核查/选房通告只是换了动作词），
    不同批次基本在 0.5 以下，所以 0.6 是够用的分界线。
    """
    pool = dict(grouped)
    for notice in orphans:
        key = _best_match(notice, pool)
        if key is None:
            # 连批次号都没有的配租通告，用自己当锚点，后续同批次通告再挂上来。
            key = f"title:{notice.merge_key or notice.canonical_key}"
        pool.setdefault(key, []).append(notice)
    grouped.clear()
    grouped.update(pool)


def _best_match(notice: HousingNotice, pool: dict[str, list[HousingNotice]]) -> str | None:
    best_key: str | None = None
    best_score = 0.0
    for key, items in pool.items():
        head = items[0]
        if (head.region_code or "") != (notice.region_code or ""):
            continue
        if not _compatible_type(notice, head):
            continue
        score = max(
            SequenceMatcher(None, notice.title or "", item.title or "").ratio()
            for item in items
        )
        if score > best_score:
            best_key, best_score = key, score
    return best_key if best_score >= ORPHAN_SIMILARITY else None


def _compatible_type(left: HousingNotice, right: HousingNotice) -> bool:
    left_types, right_types = left.housing_types or [], right.housing_types or []
    if not left_types or not right_types:
        return True
    return bool(set(left_types) & set(right_types))


def _primary_type(notice: HousingNotice) -> str:
    types = notice.housing_types or []
    if not types:
        return "unknown"
    return types[0]
