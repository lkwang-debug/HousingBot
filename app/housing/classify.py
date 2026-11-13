from __future__ import annotations

import re
from dataclasses import dataclass, field

PROCUREMENT_TERMS = (
    "采购公告",
    "采购结果",
    "自行采购",
    "采购服务",
    "招标",
    "中标",
    "成交结果",
    "询价",
    "比选",
    "技术服务",
    "采购项目",
    "造价咨询",
)
# 与房源无关的行政类公告：正文提到住房只是背景，不应进入早报。
NOISE_TERMS = (
    "协助调查",
    "送达《",
    "催缴",
    "行政处罚",
    "失信",
)
EXCLUDED_SALE_TERMS = (
    "安居型商品房",
    "配售型保障性住房",
    "人才配售",
    "配售住房",
    "认购",
    "商品住房销售",
    "安居房",
    "可售型",
)
LEASE_TERMS = ("租赁", "配租", "出租", "保租房", "公租房", "轮候")
PROCESS_TERMS = (
    "认租资格",
    "资格核查",
    "核查结果",
    "核查合格",
    "轮候申请核查",
    "审核结果",
    "终审结果",
    "摇号",
    "选房",
    "结果公示",
    "选房结果",
    "名单公示",
    "信息公示",
    "合格家庭",
    "结果",
    "续租",
    "签约",
)
# 标题命中即可判定为腾退/存量。
VACANCY_TERMS = (
    "腾退",
    "退出",
    "退租",
    "存量房源",
    "存量住房",
    "剩余房源",
    "递补",
    "空置",
    "再次配租",
    "重新配租",
)
# 摇号/选房结果：认租家庭最关心的一条消息——多少人抢多少套、自己排第几。
# 只认"摇号结果/摇号名单/摇号排序/排位"这几种能拿来预测的形态。
# "选房名单"不算摇号结果：它是选房阶段的名单，标题写"公证摇号结果已公布"会误导。
LOTTERY_RESULT_PATTERNS = (
    re.compile(r"(?:公证)?摇号[^。]{0,30}(?:结果|名单|排序|排位)"),
    re.compile(r"摇号(?:结果|名单|排序|排位)"),
    re.compile(r"(?:入围|候补|递补)[^。]{0,12}名单"),
)
# 选房结束后的成交公示：房号 + 签约人，是"谁选走了房"，不是"谁排第几"。
# 这类名单不能当摇号结果推，否则标题会误导，也没有可预测的排位信息。
SELECTION_RESULT_PATTERNS = (
    re.compile(r"选房(?:结果|情况|签约结果)"),
    re.compile(r"分配结果"),
)
# 批次流程：认租 → 资格核查 → 摇号 → 选房 → 签约。
# 认租由 rent_launch 覆盖，摇号结果由 lottery_result 覆盖，这里补齐另外三步。
# 顺序不能反：摇号/选房的标题里也常出现"结果公示"，先判具体动作再落到 review。
STAGE_PATTERNS = (
    ("lottery", re.compile(r"(?:公证)?摇号(?:结果|名单|排序|排位|公告|通告|通知)")),
    # "选房名单及选房签约"这类标题，当下动作是选房，签约是后续安排，不能标成签约。
    ("selection", re.compile(r"选房|(?:入围|候补|递补)名单")),
    ("signing", re.compile(r"签约|签订[^。]{0,12}合同|办理入住|入住(?:通告|通知|公告|手续)")),
    ("review", re.compile(r"认租资格|资格核查|核查结果|核查合格|审核结果|复核结果|终审|资格审查|合格家庭")),
)
STAGE_PRIORITY = {"review": 72, "lottery": 92, "selection": 90, "signing": 88}
STAGE_LABELS = {
    "review": "资格核查",
    "lottery": "摇号",
    "selection": "选房",
    "signing": "签约",
}
# 正文命中必须是明确语义，避免把"退出轮候"这类模板话术算成腾退。
VACANCY_BODY_PHRASES = (
    "腾退后再次配租",
    "腾退房源",
    "存量腾退房源",
    "存量房源",
    "存量住房",
    "剩余房源",
    "递补房源",
    "空置房源",
)

RENT_LAUNCH_PATTERNS = (
    re.compile(r"配租(?:有关事项|有关事宜|相关事项)?(?:的)?(?:通告|通知|公告)"),
    re.compile(r"认租(?:有关事项)?(?:的)?(?:通告|通知|公告)"),
    re.compile(r"(?:日常|常态化|批次|定向|面向[^。]{0,30})配租"),
    re.compile(r"配租(?:申请|受理)"),
    re.compile(r"(?:受理|开展)[^。]{0,20}(?:配租|认租)"),
)
BAOZU_RE = re.compile(r"保障性租赁住房|保租房")
GONGZU_RE = re.compile(r"公共租赁住房|公租房")
REGION_RE = re.compile(r"(福田区|罗湖区|南山区|盐田区|宝安区|龙华区|龙岗区|光明区|坪山区|大鹏新区)")

CATEGORY_PRIORITY = {
    "rent_launch": 100,
    "lottery_result": 95,
    "vacancy_exit": 80,
    "process": 50,
    "policy": 30,
    "other_housing": 20,
    "procurement": 0,
    "excluded": 0,
}
PUSH_CATEGORIES = {"rent_launch", "lottery_result", "vacancy_exit"}


@dataclass
class Classification:
    category: str
    priority: int
    is_target: bool
    is_push_candidate: bool
    exclude_reason: str | None = None
    housing_types: list[str] = field(default_factory=list)
    region_code: str = ""
    vacancy_evidence: str = ""
    # 批次走到哪一步：review / lottery / selection / signing；普通通告为 None。
    stage: str | None = None


def classify(title: str, body: str = "", source_region: str = "") -> Classification:
    title = title or ""
    text = f"{title}\n{body or ''}"
    full = f"{title}{body or ''}"

    # 标题里写明的房源类型优先：正文常同时提到保租房和公租房。
    housing_types = detect_housing_types(title) or detect_housing_types(full)
    region = detect_region(body or title) or source_region

    if any(term in title for term in NOISE_TERMS):
        return _build("policy", False, "noise", housing_types, region)

    if any(term in title for term in PROCUREMENT_TERMS):
        return _build("procurement", False, "procurement", housing_types, region)

    if any(term in title for term in EXCLUDED_SALE_TERMS) and not any(
        term in title for term in LEASE_TERMS
    ):
        return _build("excluded", False, "sale_not_rental", housing_types, region)

    # 摇号结果必须先于 process，否则会被"摇号"这个通用过程词吃掉，只进早报。
    # 但选房成交公示要先排除：它也有"摇号/选房"，却不是排位名单。
    if housing_types and not _is_selection_result(title):
        if any(pattern.search(title) for pattern in LOTTERY_RESULT_PATTERNS):
            return _build("lottery_result", True, None, housing_types, region)

    # 顺序关键：过程类必须先于启动/腾退，否则"剩余房源认租资格核查结果"会误判。
    if any(term in title for term in PROCESS_TERMS):
        # 不提保障房类型的过程公告（如采购结果）不算目标通告。
        stage = stage_for_title(title)
        return _build(
            "process", bool(housing_types), None, housing_types, region, stage=stage
        )

    if any(pattern.search(title) for pattern in RENT_LAUNCH_PATTERNS) or (
        "网上认租时间" in text and "认租" in title
    ):
        return _build("rent_launch", True, None, housing_types, region)

    vacancy_hit = next((term for term in VACANCY_TERMS if term in title), None)
    if vacancy_hit is None:
        vacancy_hit = next((term for term in VACANCY_BODY_PHRASES if term in (body or "")), None)
    if vacancy_hit:
        evidence = _evidence_sentence(body, vacancy_hit)
        result = _build("vacancy_exit", True, None, housing_types, region)
        result.vacancy_evidence = evidence
        return result

    if housing_types:
        return _build("other_housing", True, None, housing_types, region)

    return _build("policy", False, "not_housing", housing_types, region)


def _build(
    category: str,
    is_target: bool,
    exclude_reason: str | None,
    housing_types: list[str],
    region: str,
    stage: str | None = None,
) -> Classification:
    priority = CATEGORY_PRIORITY.get(category, 0)
    # 流程节点通告（核查/摇号/选房/签约）价值高，即时推；其余过程公示仍只进早报。
    pushable = is_target and (category in PUSH_CATEGORIES or bool(stage))
    if stage:
        priority = max(priority, STAGE_PRIORITY.get(stage, priority))
    return Classification(
        category=category,
        priority=priority,
        is_target=is_target,
        is_push_candidate=pushable,
        exclude_reason=exclude_reason,
        housing_types=housing_types,
        region_code=region,
        stage=stage,
    )


def stage_for_title(title: str) -> str | None:
    """从标题判断批次走到哪一步；认不出就返回 None，不硬猜。"""
    for stage, pattern in STAGE_PATTERNS:
        if pattern.search(title or ""):
            return stage
    return None


def stage_label(stage: str | None) -> str:
    return STAGE_LABELS.get(stage or "", "")


def _is_selection_result(title: str) -> bool:
    return any(pattern.search(title) for pattern in SELECTION_RESULT_PATTERNS)


def detect_housing_types(text: str) -> list[str]:
    types: list[str] = []
    if BAOZU_RE.search(text):
        types.append("baozu")
    if GONGZU_RE.search(text):
        types.append("gongzu")
    return types


def detect_region(text: str) -> str:
    head = (text or "")[:500]
    match = REGION_RE.search(head)
    return match.group(1) if match else ""


def _evidence_sentence(body: str, keyword: str) -> str:
    if not body:
        return ""
    index = body.find(keyword)
    if index < 0:
        return ""
    start = max(0, index - 80)
    end = min(len(body), index + len(keyword) + 80)
    return body[start:end].replace("\n", " ").strip()
