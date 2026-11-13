"""Read-only allocation classification and complete, deduplicated report snapshots.

These views never change batch links or business versions. Website ownership is
deliberately not evidence of the organization responsible for an allocation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import select

from app.housing.classify import detect_housing_types
from app.housing.dedup import clean_title, normalize_title
from app.housing.extract import ReviewInfo, extract_review_info
from app.housing.models import HousingBatchAnchor, HousingMeta, HousingNotice, HousingSource, HousingSourceState, utcnow

REGIONS = ("福田区", "罗湖区", "南山区", "盐田区", "宝安区", "龙岗区", "龙华区", "坪山区", "光明区", "大鹏新区")
DISTRICT_ORG = re.compile(r"(?:深圳市)?(?P<region>" + "|".join(REGIONS) + r")(?:住房和建设局|住房建设局|住房保障中心|住房保障署|住建局|建设局|管理委员会)")
CITY_ORG = re.compile(r"深圳市(?:住房和建设局|住房建设局|住房保障署|住房保障中心|住建局)")
OBJECT_TERMS = ("配租对象", "申请对象", "供应对象", "配租范围")
QUALIFY_TERMS = ("户籍", "社保", "社会保险", "轮候", "收入", "财产", "住房", "劳动合同", "工作年限")
STAGES = ("announced", "applying", "waiting", "review", "lottery", "selection", "signing", "closed")
STAGE_LABELS = {"announced": "尚未开始认租", "applying": "认租中", "waiting": "认租已结束，待后续公告",
    "review": "资格核查公告", "lottery": "摇号结果已公布", "selection": "选房通知", "signing": "签约安排",
    "closed": "本批配租已明确结束", "unknown": "独立公告，批次待关联", "unverified_window": "认租时间待核实"}
CATEGORY_NAMES = {"city_baozu": "市级保租房", "district_baozu": "区级保租房",
    "personal_baozu": "面向个人配租", "enterprise_baozu": "面向单位定向配租", "gongzu": "公租房"}


def local_time(value):
    if value is not None and value.tzinfo is not None:
        from app.config import get_settings
        return value.astimezone(get_settings().tz).replace(tzinfo=None)
    return value


def _clauses(text):
    # Layout newlines are not legal clause boundaries. Preserve exceptions and alternatives.
    text = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"[。；]", text) if s.strip()]


def _org_hits(text):
    hits = [(m.group(0), "district", m.group("region")) for m in DISTRICT_ORG.finditer(text or "")]
    occupied = [m.span() for m in DISTRICT_ORG.finditer(text or "")]
    hits += [(m.group(0), "city", "深圳市") for m in CITY_ORG.finditer(text or "")
             if not any(a <= m.start() < b for a, b in occupied)]
    return list(dict.fromkeys(hits))


def _organizer(notice):
    hits = _org_hits(notice.title) + _org_hits(notice.department)
    if not hits:
        # A signature or an explicit organizing statement is evidence; a quoted law is not.
        tail = (notice.body_text or "")[-600:]
        for line in tail.splitlines():
            if len(line.strip()) <= 70 and re.fullmatch(r"[\u4e00-\u9fff\s]+", line.strip() or "0"):
                hits.extend(_org_hits(line))
        for clause in _clauses(notice.body_text):
            if re.search(r"(?:由|本次配租由|本批次配租由).{0,35}(?:组织|负责|实施)|(?:住房和建设局|住房保障署|住房保障中心)(?:现|拟|将|组织|负责)", clause):
                hits.extend(_org_hits(clause))
    hits = list(dict.fromkeys(hits))
    if not hits:
        return ("", "unknown", ""), []
    identities = {(level, region) for _, level, region in hits}
    if len(identities) != 1:
        return ("", "unknown", ""), ["组织配租机构证据冲突"]
    return hits[0], []


def _audience_of(text):
    text = text or ""
    personal = bool(re.search(r"面向个人|个人配租|个人申请|申请人应当以家庭为单位提出|(?:面向|对象(?:为|是)).{0,80}(?:家庭|轮候人|个人)|户籍.{0,30}(?:在册轮候人|轮候家庭)", text))
    enterprise = bool(re.search(r"面向(?:辖区|本区)?(?:企事业单位|企（事）业单位|企业[（(]机构[）)]|企业(?:定向|配租|申请)|单位)|单位定向|面向.{0,20}重点企业(?!人才)|(?:由|以).{0,30}(?:单位|企业).{0,12}(?:统一申请|统一认租|名义申请)|(?:单位|企业)统一(?:组织)?申请", text))
    enterprise |= bool(re.search(r"(?:配租对象|申请对象|供应对象)(?:为|是).{0,100}(?:企事业单位|企（事）业单位|企业[（(]机构[）)])(?:[。，；]|$)", text))
    if re.search(r"不(?:直接)?面向个人|个人不能申请|不接受个人申请", text):
        personal = False
    if personal and enterprise:
        return "conflict"
    return "personal" if personal else "enterprise" if enterprise else "unknown"


def _object_evidence(notice):
    body = notice.body_text or ""
    heading = re.search(r"(?:^|\n)\s*(?:[一二三四五六七八九十\d]+[、．.]\s*)?(?:本次|本批次)?(?:配租对象|申请对象|供应对象|配租范围)[^\n]{0,25}\n", body)
    section = body
    if heading:
        section = body[heading.end():]
        end = re.search(r"(?:^|\n)\s*[一二三四五六七八九十]+、[^\n]+", section)
        if end:
            section = section[:end.start()]
    clauses = [s for s in _clauses(section)
               if not re.search(r"网站首页|智能机器人|无障碍浏览|分享到|当前位置", s)]
    direct = [s for s in clauses if any(t in s for t in OBJECT_TERMS)
              or re.search(r"(?:本次|本批次).{0,35}面向|申请人应当以家庭为单位提出|(?:由|以).{0,25}(?:单位|企业).{0,12}(?:统一申请|名义申请)", s)]
    # Full long clauses remain in the official document; never amputate an exception.
    if heading:
        quoted = tuple(s for index, s in enumerate(clauses)
                       if len(s) <= 1500 and (index == 0 or any(t in s for t in OBJECT_TERMS)))[:2]
        direct = list(quoted) + direct
    else:
        quoted = tuple(s for s in direct if len(s) <= 1500 and not re.search(r"无障碍|网站首页|分享到|智能机器人", s))[:2]
    title_kind = _audience_of(notice.title)
    body_kinds = {_audience_of(s) for s in direct} - {"unknown"}
    kinds = body_kinds | ({title_kind} if title_kind != "unknown" else set())
    if "conflict" in kinds or len(kinds) > 1:
        kind, issues = "unknown", ["个人/单位配租对象证据冲突"]
    else:
        kind, issues = next(iter(kinds), "unknown"), []
    if not quoted and title_kind in {"personal", "enterprise"}:
        quoted = (notice.title,)
    elif not quoted and "面向" in (notice.title or "") and len(notice.title) <= 220:
        quoted = ("标题载明：" + notice.title,)
    qualifications = tuple(s for s in clauses if any(t in s for t in QUALIFY_TERMS if t != "住房")
                           or any(t in s for t in ("自有住房", "享受住房保障", "居住证")))
    qualifications = tuple(s for s in qualifications if s not in quoted and len(s) <= 1000)
    employees = tuple(s for s in clauses if re.search(r"员工|职工|工作人员|劳动合同", s)
                      and s not in quoted and len(s) <= 1000)[:2] if kind == "enterprise" else ()
    qualifications = tuple(s for s in qualifications if s not in employees)[:3]
    return kind, quoted, qualifications, employees, issues


@dataclass(frozen=True)
class AllocationProfile:
    level: str
    region: str
    organizer: str
    housing_type: str
    audience: str
    object_text: tuple[str, ...] = ()
    qualifications: tuple[str, ...] = ()
    employee_conditions: tuple[str, ...] = ()
    project_regions: tuple[str, ...] = ()
    evidence_ids: tuple[int, ...] = ()
    issues: tuple[str, ...] = ()


def allocation_profile(notice, launch=None):
    base = launch or notice
    org, issues = _organizer(base)
    if not base.body_text:
        issues.append("原配租详情尚未采集，资格与时间可能不完整" if launch or base.category == "rent_launch"
                      else "公告详情尚未采集，内容待核实")
    if launch is not None and notice.id != launch.id:
        current, current_issues = _organizer(notice)
        issues += current_issues
        if current[1] != "unknown" and org[1] != "unknown" and current[1:] != org[1:]:
            org = ("", "unknown", "")
            issues.append("当前公告与原配租公告的组织机构冲突")
        elif org[1] == "unknown" and not issues:
            org = current
    types = detect_housing_types(base.title or "") or list(base.housing_types or [])
    if re.search(r'人才(?:租赁)?住房',base.title or '') and 'talent' not in types:
        types.append('talent')
    current_types = detect_housing_types(notice.title or "")
    if current_types and types and set(current_types) != set(types):
        types = list(set(current_types) | set(types))
        issues.append("当前公告与原配租公告的住房类型冲突")
    if not types:
        for clause in _clauses(base.body_text):
            if re.search(r"本次配租|本批次配租|现面向|推出.{0,12}房源", clause):
                types = detect_housing_types(clause)
                if types:
                    break
    housing = types[0] if len(types) == 1 and types[0] in {"baozu", "gongzu"} else "mixed" if len(types) > 1 else "other" if types else "unknown"
    # A governing law is not the project's housing type. Explicit project labels
    # can establish a mixed pool; vague policy references cannot.
    if re.search(r"[（(]人才住房[）)]",base.body_text or '') and housing=='baozu':
        housing='mixed'
    kind, objects, qualifications, employees, object_issues = _object_evidence(base)
    issues += object_issues
    if launch is not None and notice.id != launch.id:
        current_kind, current_objects, current_qualifications, current_employees, more = _object_evidence(notice)
        issues += more
        if current_kind != "unknown" and kind != "unknown" and current_kind != kind:
            kind = "unknown"; issues.append("当前公告与原配租公告的配租对象冲突")
        elif kind == "unknown" and not object_issues:
            kind = current_kind
            objects = objects or current_objects
            qualifications = qualifications or current_qualifications
            employees = employees or current_employees
    if org[1] == "unknown": issues.append("组织配租层级待核实")
    if housing in {"unknown", "mixed"}: issues.append("住房类型待核实或涉及混合房源")
    if kind == "unknown": issues.append("配租对象待核实")
    project_regions = []
    for clause in _clauses(base.body_text):
        if re.search(r"(?:项目|房源|小区).{0,16}(?:位于|坐落|所在地)", clause):
            project_regions.extend(region for region in REGIONS if region in clause)
    return AllocationProfile(org[1], org[2], org[0], housing, kind, objects, qualifications,
        employees, tuple(dict.fromkeys(project_regions)), tuple(dict.fromkeys([base.id, notice.id])), tuple(dict.fromkeys(issues)))


def notice_stage(notice):
    title = notice.title or ""
    if notice.category == "rent_launch": return "applying"
    if re.search(r"(?:预告|预通知|后续|计划|拟于)", title) and not re.search(r"结果|名单", title):
        return None
    if "选房" in title: return "selection"
    if re.search(r"签约|办理入住|签订.{0,12}合同", title): return "signing"
    if re.search(r"摇号.{0,30}(?:结果|名单|排序|排位)", title): return "lottery"
    if re.search(r"认租资格|资格核查|核查结果|核查合格|审核结果|初审结果|复核结果|终审|资格审查|合格家庭", title): return "review"
    return None


def review_scope_note(notice, review):
    """Explain only the current document's verified review scope, not a district default."""
    pending={b['band']:b.get('not_reviewed') for b in review.get('bands',[])}
    if not any(n is not None and n>0 for n in pending.values()):return ''
    body=re.sub(r'\s+','',notice.body_text or '') if notice else ''
    evidence=('单身公寓类、一房类房源认租申请家庭','第一、第二队列申请人/家庭全部列入首批资格核查名单',
              '首轮选房结束后仍有可选房源','从第三队列名单内','依照摇号顺序启动第二轮资格核查及选房工作')
    if (notice and not re.search(r'第[二三四五六七八九十\d]+轮|递补',notice.title or '')
            and all(term in body for term in evidence) and all(pending.get(b) is not None for b in ('40','50'))):
        return ('公告按房源类型安排审核：90㎡、100㎡及以上三个队列均纳入首轮；40㎡、50㎡先审核第一、第二队列，'
                '后续须待首轮选房仍有房源，再按摇号顺序启动。原摇号队列与家庭当前队列可能不同，'
                '个人审核结果须按编号核对本次名单，不能仅凭队列推断。未纳入本轮不等于不合格，也不保证一定进入下一轮。')
    return '未纳入本轮审核的具体原因尚未核实，请查看本次核查公告；不据此认定资格不合格。'


def vacancy_notice(notice):
    title, body = notice.title or "", notice.body_text or ""
    if "退出轮候" in title and not re.search(r"腾退|退租|存量|空置", title): return False
    if re.search(r"腾退|退租|(?:存量|剩余|空置|递补).{0,5}(?:住房|房源)|(?:再次|重新)配租", title): return True
    if notice.category == "vacancy_exit": return True
    return any(term in body for term in ("腾退后再次配租", "腾退房源", "存量住房", "存量房源", "剩余房源", "空置房源"))


@dataclass
class NoticeReference:
    id: int
    title: str
    url: str
    published_at: datetime | None
    stage: str | None
    is_new: bool = False
    is_update: bool = False
    vacancy: bool = False
    first_seen_at: datetime | None = None


@dataclass
class BatchCard:
    code: str
    batch_id: int | None
    title: str
    profile: AllocationProfile
    notice_ids: list[int]
    references: list[NoticeReference]
    stage: str
    is_open: bool
    in_transit: bool
    start: datetime | None = None
    end: datetime | None = None
    units: int | None = None
    timeline: list[tuple[str, str]] = field(default_factory=list)
    applicable_lottery: bool = False
    review: ReviewInfo | None = None
    inventory: dict | None = None
    lottery_breakdown: list[dict] = field(default_factory=list)
    lottery_rules: list[str] = field(default_factory=list)
    lottery_progress: list[str] = field(default_factory=list)
    qualification: dict | None = None
    selection_progress: dict | None = None
    action_windows: list[dict] = field(default_factory=list)
    daily_status: str = "active"
    daily_reason: str = ""
    delta_reasons: list[str] = field(default_factory=list)

    @property
    def changed(self):
        return any(n.is_new or n.is_update for n in self.references)


@dataclass
class CategorySummary:
    open_batches: int = 0
    new_notices: int = 0
    updated_notices: int = 0
    vacancy_notices: int = 0
    lottery_batches: int = 0
    selection_batches: int = 0
    in_transit_batches: int = 0
    personal_batches: int = 0
    enterprise_batches: int = 0
    unknown_batches: int = 0
    object_examples: list[str] = field(default_factory=list)


@dataclass
class ReportSnapshot:
    kind: str
    generated_at: datetime
    window_start: datetime
    cards: list[BatchCard]
    totals: CategorySummary
    categories: dict[str, CategorySummary]
    source_status: str
    source_updated_at: datetime | None
    lookup_url: str = ""
    lookup_pending: bool = False
    queryable_codes: list[str] = field(default_factory=list)
    archived_cards: list[dict] = field(default_factory=list)
    history_events: list[BatchCard] = field(default_factory=list)
    baseline_key: str = ""
    baseline_at: datetime | None = None
    housing_totals: dict[str, CategorySummary] = field(default_factory=dict)
    transition_note: str = ""

    def audit_dict(self):
        return asdict(self)


def summarize(cards):
    batches = [c for c in cards if c.batch_id is not None]
    refs = {r.id: r for c in cards for r in c.references}
    return CategorySummary(
        open_batches=sum(c.is_open for c in batches),
        new_notices=sum(r.is_new for r in refs.values()),
        updated_notices=sum(r.is_update for r in refs.values()),
        vacancy_notices=sum(r.is_new and r.vacancy for r in refs.values()),
        lottery_batches=sum(any(r.is_new and r.stage == "lottery" for r in c.references) for c in batches),
        selection_batches=sum(any(r.is_new and r.stage == "selection" for r in c.references) for c in batches),
        in_transit_batches=sum(c.in_transit for c in batches),
        personal_batches=sum(c.profile.audience == "personal" for c in batches if c.in_transit),
        enterprise_batches=sum(c.profile.audience == "enterprise" for c in batches if c.in_transit),
        unknown_batches=sum(c.profile.audience == "unknown" for c in batches if c.in_transit),
        object_examples=list(dict.fromkeys(text for c in cards for text in c.profile.object_text)),
    )


def _identity(notice):
    return normalize_title(clean_title(notice.title or "")) or notice.canonical_key


def _closed(notices):
    pattern=r'(?:本批次|本批|本次)(?:住房)?(?:集中)?配租(?:工作)?(?:现已|已经|已)(?:全部)?(?:结束|完成|终止|撤销)|现(?:决定)?(?:终止|撤销)本(?:次|批次)(?:住房)?配租'
    closed=[n for n in notices if re.search(pattern,(n.title or '')+'\n'+(n.body_text or ''))]
    if not closed:return False
    last=max((n.published_at for n in closed if n.published_at and not n.published_unverified),default=None)
    if last and any(n.published_at and not n.published_unverified and n.published_at>last and
        re.search(r'恢复受理|重新启动|补充选房|递补选房|后续签约|选房安排',n.title or '') for n in notices):return False
    return True


def build_snapshot(session, kind, *, now=None, notices=None, regions=(), updated_notice_ids=(), baseline=None, include_history=False):
    """Build complete counts before pagination. No writes and no network requests."""
    if kind not in {"instant", "digest", "evening"}: raise ValueError("未知报表类型")
    now = local_time(now or utcnow())
    if kind in {'digest','evening'} and baseline is None:
        from app.housing.daily_delta import accepted_baseline
        baseline = accepted_baseline(session,now)
    since = local_time(datetime.fromisoformat(baseline['generated_at'])) if baseline else now-timedelta(hours=24)
    if kind=='instant':
        since=now-timedelta(hours=24)
    all_rows = list(session.scalars(select(HousingNotice).where(HousingNotice.is_target.is_(True)).order_by(HousingNotice.id)))
    # Historical replays must not import announcements which were both
    # published and first observed in the future of the requested snapshot.
    all_rows=[n for n in all_rows if not (n.first_seen_at and local_time(n.first_seen_at)>now
        and (not n.published_at or local_time(n.published_at)>now))]
    incoming_ids = {n.id for n in notices or []}
    updated_notice_ids = set(updated_notice_ids)
    # Exact copies share one canonical display ID, even if list dates/windows differ.
    title_groups = defaultdict(list)
    for notice in all_rows: title_groups[_identity(notice)].append(notice)
    all_ids = {n.id for n in all_rows}
    for notice in notices or []:
        if notice.id not in all_ids:
            title_groups[_identity(notice)].append(notice)
    duplicates = defaultdict(list)
    from app.housing.extract import extract_doc_number
    for title, copies in title_groups.items():
        documents = {n.doc_number or extract_doc_number(n.title) for n in copies} - {None, ""}
        episodes = {}
        for n in sorted(copies, key=lambda n: (local_time(n.published_at) or datetime.min, n.id)):
            document = (n.doc_number or extract_doc_number(n.title) or "unverified") if len(documents) > 1 else ""
            # Identical generic titles may be reissued in April/August or each
            # year. A title alone must never fuse their windows and histories.
            if len(documents) == 1 or document not in {"", "unverified"}:
                episode = "document"
            elif n.published_at and not n.published_unverified:
                stamp = local_time(n.published_at).date()
                dates = episodes.setdefault(document, [])
                episode = next((d for d in dates if abs((stamp - d).days) <= 7), None)
                if episode is None:
                    dates.append(stamp); episode = stamp
            else:
                episode = ("unverified-date", n.canonical_key)
            duplicates[(title, document, episode)].append(n)
    aliases, representatives, member_ids, copies_by_canonical = {}, {}, {}, {}
    for copies in duplicates.values():
        canonical = min(n.id for n in copies)
        richest = max(copies, key=lambda n: (bool(n.body_text), bool(n.application_end), len(n.body_text or ""), -n.id))
        representatives[canonical], member_ids[canonical] = richest, [n.id for n in copies]
        copies_by_canonical[canonical] = copies
        aliases.update({n.id: canonical for n in copies})
    anchors = {a.key: aliases.get(a.launch_id, a.launch_id) for a in session.scalars(select(HousingBatchAnchor))}
    for canonical, n in representatives.items():
        if n.category == "rent_launch":
            from app.housing.batches import batch_key
            if batch_key(n): anchors.setdefault(batch_key(n), canonical)
    groups = defaultdict(list)
    for canonical, notice in representatives.items():
        root = canonical if notice.category == "rent_launch" else anchors.get(notice.batch_key_v2)
        groups[("B", root) if root in representatives else ("N", canonical)].append((canonical, notice))
    revisions = defaultdict(list)
    for record in session.scalars(select(HousingMeta).where(HousingMeta.key.like("notice-revisions:%"))):
        notice_id = int(record.key.split(":", 1)[1])
        revisions[notice_id] = record.value.get("revisions", [])
    cards, archived, history_events = [], [], []
    for (prefix, root), entries in groups.items():
        launch = representatives.get(root) if prefix == "B" else None
        history = [n for _, n in entries]
        dated_history=[n for n in history if n.published_at and not n.published_unverified and local_time(n.published_at)<=now]
        latest = max(dated_history or ([launch] if launch else history), key=lambda n: (local_time(n.published_at or n.first_seen_at) or datetime.min, n.id))
        profile = allocation_profile(latest, launch)
        if regions and profile.level != "city" and profile.region not in regions: continue
        refs = []
        stage_evidence = {}
        for canonical, notice in entries:
            copies = copies_by_canonical[canonical]
            valid_dates = [local_time(n.published_at) for n in copies if n.published_at and not n.published_unverified
                           and local_time(n.published_at) <= now]
            published = min(valid_dates) if valid_dates else None
            stage = notice_stage(notice)
            in_packet = bool(incoming_ids.intersection(member_ids[canonical]))
            is_new = in_packet if kind == "instant" else bool(published and since <= published <= now)
            # A fetch/event timestamp is not evidence that the official text changed.
            updated = not is_new and bool(published) and any(
                revision.get("to") == n.business_version and
                since <= local_time(datetime.fromisoformat(revision["at"])) <= now
                for n in copies for revision in revisions[n.id])
            if kind == "instant": updated = in_packet and bool(updated_notice_ids.intersection(member_ids[canonical]))
            refs.append(NoticeReference(canonical, notice.title, notice.canonical_url, published, stage,
                                        is_new and not updated, updated, vacancy_notice(notice),
                                        min((local_time(n.first_seen_at) for n in copies if n.first_seen_at),default=None)))
            if stage and published:
                stage_evidence[stage] = True
            elif stage and not published:
                profile = replace(profile, issues=tuple(dict.fromkeys((*profile.issues, "存在发布日期待核实的公告，未据此推进阶段或计为新增"))))
        start = local_time(launch.application_start) if launch else None
        end = local_time(launch.application_end) if launch else None
        is_open = bool(launch and start and end and start <= now <= end)
        rank = {s: i for i, s in enumerate(STAGES)}
        # Follow dated official records. Some batches review before lottery,
        # others lottery before review; a global stage maximum loses that order.
        dated_nodes = [r for r in refs if r.stage and r.published_at]
        stage = max(dated_nodes, key=lambda r: (r.published_at, rank[r.stage], r.id)).stage if dated_nodes else "applying" if launch else "unknown"
        closed = _closed(dated_history)
        if closed:
            stage, is_open = "closed", False
        elif stage == "applying":
            if start and start > now: stage = "announced"
            elif end and end < now: stage = "waiting"
            elif not start or not end: stage = "unverified_window"
        timestamps = [local_time(n.published_at) for n in history if n.published_at and not n.published_unverified
                      and local_time(n.published_at) <= now]
        active = bool(launch and not closed and (is_open or (end and end >= now)
                      or (timestamps and max(timestamps) >= now - timedelta(days=120))))
        changed = any(r.is_new or r.is_update for r in refs)
        if kind == "instant":
            if not any(incoming_ids.intersection(member_ids[canonical]) for canonical, _ in entries): continue
            # Counts for an instant packet must not include other historical rows.
            for ref in refs:
                if not incoming_ids.intersection(member_ids[ref.id]): ref.is_new = ref.is_update = False
        applicable_lottery = "lottery" in stage_evidence or bool(launch and re.search(r"公证摇号|摇号方式|摇号确定", launch.body_text or ""))
        is_open=is_open and stage=='applying'
        timeline = [("发布", "配租通告已收录" if launch else "原配租公告未关联")]
        application_state = ("未关联" if not launch else "批次已明确结束" if closed else "尚未开始认租" if start and start > now else
            "认租已截止" if end and end < now else "认租中" if is_open else "认租时间待核实")
        timeline.append(("认租", application_state))
        for key, label in (("review", "资格核查"), ("lottery", "摇号"), ("selection", "选房"), ("signing", "签约")):
            if key == "lottery" and not applicable_lottery: continue
            timeline.append((label, "当前节点，已有公告" if stage == key else "已有官方公告" if key in stage_evidence else "未采集到后续公告"))
        explicit_total = re.search(r"(?:本次|本批次)(?:配租|供应|提供|推出)(?:的)?(?:公共租赁住房|保障性租赁住房|公租房|保租房|住房|房源)(?:共|合计|总计)?\s*(\d[\d,，]{0,6})\s*套", launch.body_text or "") if launch else None
        units = int(explicit_total.group(1).replace(",", "").replace("，", "")) if explicit_total else None
        review_info = None
        if stage == "review":
            review_rows = [n for n in history if notice_stage(n) == "review" and n.published_at
                           and not n.published_unverified and local_time(n.published_at) <= now]
            if review_rows:
                current_review = max(review_rows, key=lambda n: (local_time(n.published_at), n.id))
                review_info = extract_review_info(current_review.body_text, current_review.published_at)
                review_info.notice_id = current_review.id
        card=BatchCard(f"{prefix}{root}", root if launch else None, (launch or latest).title,
            profile, sorted({i for canonical, _ in entries for i in member_ids[canonical]}), refs, stage,
            is_open and stage=='applying', active, start, end, units, timeline, applicable_lottery, review_info)
        if kind=='instant':
            from app.housing.daily_lifecycle import action_windows
            card.action_windows=[w for n in dated_history for w in action_windows(n)]
        if kind!='instant':
            from app.housing.daily_lifecycle import classify_daily
            card.daily_status,card.daily_reason=classify_daily(card,history,now)
            card.in_transit=card.daily_status in {'active','review_needed'}
            if not card.in_transit:
                if card.batch_id is not None:
                    archived.append({'code':card.code,'notice_ids':card.notice_ids,'title':card.title,
                        'status':card.daily_status,'reason':card.daily_reason,'actions':card.action_windows})
                if any(r.is_new for r in refs):
                    if card.batch_id is None:
                        cards.append(card)
                        continue
                    history_events.append(card)
                if not include_history:continue
        cards.append(card)
    def sort_key(card):
        urgent = card.is_open and card.end and 0 <= (card.end.date() - now.date()).days <= 3
        return (0 if urgent else 1 if card.changed else 2,
                card.end if card.stage in {"applying", "announced"} and card.end and card.end >= now else datetime.max, card.code)
    cards.sort(key=sort_key)
    from app.housing.inventory import load_inventory, batch_inventory
    from app.housing.models import HousingLotteryStat
    from app.housing.batches import batch_key
    for card in cards:
        launch = representatives.get(card.batch_id)
        card.inventory = (load_inventory(session, launch) or batch_inventory(session, batch_key(launch))) if launch else None
        if card.inventory and card.inventory.get("complete") and card.inventory.get("declared_total") is not None:
            card.units = card.inventory["declared_total"]
        # Latest event only; never silently replace a changed/partial new list
        # with an older complete round. Mirrors share the same display event.
        results = [n for n in all_rows if n.id in card.notice_ids and n.category == "lottery_result"]
        for n in sorted(results, key=lambda n: (local_time(n.published_at) or datetime.min, -n.id), reverse=True):
            if n.published_unverified or not n.published_at or local_time(n.published_at) > now:
                continue
            from app.housing.competition import batch_summary, rule_summary
            card.lottery_breakdown = batch_summary(session, n, card.inventory)
            if not card.lottery_breakdown:
                card.lottery_breakdown = [{"category": "unparsed", "label": "本批摇号名单", "records": 0,
                    "households_verified": False, "queue_complete": False, "queues": {},
                    "published_at": n.published_at.isoformat(), "url": n.canonical_url,
                    "missing": ["名单尚未完整解析"]}]
            card.lottery_rules = rule_summary(session, n, launch, card.inventory)
            review_nodes = [r for r in card.references if r.stage == "review" and r.published_at]
            if review_nodes:
                current = max(review_nodes, key=lambda r: (r.published_at, r.id))
                review_notice = representatives[current.id]
                info = extract_review_info(review_notice.body_text, review_notice.published_at)
                counts = []
                if info.eligible is not None:
                    counts.append(f"合格{info.eligible}户")
                if info.ineligible is not None:
                    counts.append(f"不合格{info.ineligible}户")
                if counts:
                    card.lottery_progress.append("审核进展（本次核查公告范围）：" + "／".join(counts)
                        + f"｜[核查来源]({current.url})；未审核家庭不计为不合格。")
            break
        if launch:
            from app.housing.qualification import public_review
            card.qualification=public_review(session,batch_key(launch),card.lottery_breakdown,at=now)
            if card.qualification:
                current_review=session.get(HousingNotice,card.qualification['notice_id'])
                card.qualification['display_scope_note']=review_scope_note(current_review,card.qualification)
                from app.housing.review_statistics import report_statistics
                card.qualification['statistics']=report_statistics(session,batch_key(launch),card.qualification,card.inventory)
    selectors = {
        "city_baozu": lambda c: c.profile.housing_type == "baozu" and c.profile.level == "city",
        "district_baozu": lambda c: c.profile.housing_type == "baozu" and c.profile.level == "district",
        "personal_baozu": lambda c: c.profile.housing_type == "baozu" and c.profile.audience == "personal",
        "enterprise_baozu": lambda c: c.profile.housing_type == "baozu" and c.profile.audience == "enterprise",
        "gongzu": lambda c: c.profile.housing_type == "gongzu",
        "unlevel_baozu": lambda c:c.profile.housing_type=='baozu' and c.profile.level=='unknown',
        "mixed": lambda c:c.profile.housing_type=='mixed',
        "other": lambda c:c.profile.housing_type=='other',
        "unknown": lambda c:c.profile.housing_type=='unknown',
    }
    sources = list(session.execute(select(HousingSource.name, HousingSourceState.last_verify_status,
        HousingSourceState.last_checked_at).outerjoin(HousingSourceState, HousingSourceState.source_id == HousingSource.id)
        .where(HousingSource.enabled.is_(True))))
    failed = [name for name, status, _ in sources if status not in {None, "ok"}]
    unchecked = sum(status is None for _, status, _ in sources)
    source_status = f"已配置 {len(sources)} 个来源，异常 {len(failed)} 个，未检查 {unchecked} 个"
    if failed: source_status += "；异常来源：" + "、".join(failed)
    checked = [local_time(t) for _, _, t in sources if t and local_time(t)<=now]
    snapshot = ReportSnapshot(kind, now, since, cards, summarize(cards),
        {key: summarize([c for c in cards if predicate(c)]) for key, predicate in selectors.items()},
        source_status, max(checked) if checked else None)
    snapshot.archived_cards=archived
    snapshot.history_events=history_events
    snapshot.baseline_key=(baseline or {}).get('key','')
    snapshot.baseline_at=since if baseline else None
    if baseline and baseline.get('schema',0)<4:
        snapshot.transition_note='统计范围已调整：已过办理期和长期无后续的历史批次退出日报；历史资料仍保留。'
    if kind!='instant':
        from app.housing.selection_progress import public_progress,progress_net
        old_cards={c['code']:c for c in (baseline or {}).get('audit',{}).get('cards',[])}
        for card in cards:
            launch=representatives.get(card.batch_id)
            if launch and card.profile.housing_type=='baozu' and card.stage in {'selection','signing'}:
                card.selection_progress=public_progress(session,batch_key(launch),at=now,windows=card.action_windows,notice_ids=card.notice_ids)
                progress_net(card.selection_progress,old_cards.get(card.code,{}).get('selection_progress'))
        from app.housing.daily_delta import apply_delta
        apply_delta(snapshot,baseline,revisions)
        snapshot.cards=[c for c in snapshot.cards if c.batch_id is not None or c.changed]
        snapshot.totals=summarize(snapshot.cards)
        snapshot.categories={key:summarize([c for c in snapshot.cards if predicate(c)]) for key,predicate in selectors.items()}
    snapshot.housing_totals={key:summarize([c for c in snapshot.cards if c.profile.housing_type==key]) for key in ('baozu','gongzu','mixed','other','unknown')}
    from app.housing.lookup import published_ready
    from app.housing.render import lookup_url_for
    if published_ready(session=session):
        snapshot.lookup_url = lookup_url_for(now)
        from app.housing.config import get_housing_settings
        if get_housing_settings().lookup_access_mode=='signed':
            from app.housing.lookup import read_manifest,source_version
            published=session.get(HousingMeta,'lookup-published').value
            built={k:v for p in read_manifest().get('pages',{}).values() for k,v in p.get('notices',{}).items()}
            snapshot.lookup_pending=any(published.get('notices',{}).get(k)!=v for k,v in built.items())
            if not snapshot.lookup_pending:
                snapshot.lookup_pending=any((n:=session.get(HousingNotice,int(k))) is not None and source_version(session,n)!=v for k,v in published.get('notices',{}).items())
        for card in cards:
            if card.stage in {"lottery", "selection"} and card.profile.housing_type == "baozu":
                if any(published_ready(representatives[r.id], session=session) for r in card.references):
                    snapshot.queryable_codes.append(card.code)
    return snapshot
