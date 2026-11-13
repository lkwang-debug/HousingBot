"""资格粗筛：只做提示，不做判定。

资格规则每年都调，系统替人下"你一定符合"的结论，一旦说错就是投诉和退款。
所以这里只做两件事：把明显不对口的批次挑出来（企业定向、人才配售），
把批次自己写明的门槛列出来（户籍、社保年限、收入财产限额、轮候库）。
"""

from __future__ import annotations

import re
from typing import Iterable

from app.housing.models import HousingNotice

ENTERPRISE_TERMS = (
    "企事业单位",
    "企（事）业单位",
    "面向企业",
    "企业定向",
    "单位定向",
    "重点企业",
    "企业人才",
)
PERSONAL_TERMS = ("面向个人", "个人配租", "个人申请", "个人轮候", "户籍在册轮候", "户籍轮候家庭")

HOUSEHOLD_TERMS = ("户籍", "深户", "轮候人")
QUOTA_TERMS = ("收入财产限额", "收入限额", "财产限额")
WAITLIST_TERMS = ("在册轮候", "轮候库", "轮候家庭", "轮候人")
SOCIAL_INSURANCE_RE = re.compile(r"(?:社保|社会保险)[^。；\n]{0,12}?(\d{1,2})\s*年")


def audience(notice: HousingNotice) -> str:
    """personal / enterprise / unknown。只看标题：正文里常同时出现两类词，噪声大。"""
    title = notice.title or ""
    if any(term in title for term in ENTERPRISE_TERMS) and not any(
        term in title for term in PERSONAL_TERMS
    ):
        return "enterprise"
    if any(term in title for term in PERSONAL_TERMS):
        return "personal"
    return "unknown"


def is_personal_friendly(notice: HousingNotice) -> bool:
    """企业定向的批次进早报就够了，不值得单独打扰个人订阅者。"""
    return audience(notice) != "enterprise"


def requirements(notice: HousingNotice) -> list[str]:
    # Quote complete clauses; never turn a keyword or a negation into a requirement.
    text = notice.body_text or ""
    terms = HOUSEHOLD_TERMS + QUOTA_TERMS + WAITLIST_TERMS + ("社保", "社会保险")
    found = []
    for sentence in re.split(r"[。；\n]+", text):
        sentence = " ".join(sentence.split()).strip()
        if not sentence or len(sentence) > 300:
            continue  # Truncating a long exception/alternative would change its meaning.
        if any(term in sentence for term in terms):
            found.append(sentence)
    return _dedupe(found)[:4]


def summary_lines(notice: HousingNotice) -> list[str]:
    """管家版推送里额外附的一行资格粗筛。"""
    lines: list[str] = []
    kind = audience(notice)
    if kind == "enterprise":
        lines.append("申请对象：企事业单位；个人申请方式以原文为准")
    elif kind == "personal":
        lines.append("申请对象：个人；是否符合资格需核对原文")
    needs = requirements(notice)
    if needs:
        lines.extend("资格原文：" + value for value in needs)
    else:
        lines.append("资格要求见原文")
    return lines


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
