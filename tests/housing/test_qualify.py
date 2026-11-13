from __future__ import annotations

from datetime import datetime, timedelta

from app.housing.models import HousingNotice
from app.housing.qualify import audience, is_personal_friendly, requirements, summary_lines
from app.housing.render import render_instant

NOW = datetime(2026, 10, 20, 9, 0)


def _notice(**overrides) -> HousingNotice:
    payload = dict(
        canonical_key=overrides.get("canonical_key", "k1"),
        canonical_url="https://e.gov.cn/1.html",
        url_hash="h1",
        title="光明区2026年第一批次面向个人配租保障性租赁住房的通告",
        title_norm="x",
        category="rent_launch",
        priority=100,
        is_target=True,
        is_push_candidate=True,
        housing_types=["baozu"],
        region_code="光明区",
        published_at=NOW - timedelta(days=1),
        first_seen_at=NOW - timedelta(days=1),
        body_text="",
    )
    payload.update(overrides)
    return HousingNotice(**payload)


def test_enterprise_batch_is_not_for_individuals():
    notice = _notice(title="坪山区关于面向企（事）业单位配租保障性租赁住房分配结果公示")
    assert audience(notice) == "enterprise"
    assert is_personal_friendly(notice) is False


def test_personal_batch_is_flagged():
    notice = _notice()
    assert audience(notice) == "personal"
    assert is_personal_friendly(notice) is True


def test_unknown_audience_is_not_guessed():
    notice = _notice(title="光明区2026年第一批次配租保障性租赁住房的通告")
    assert audience(notice) == "unknown"


def test_enterprise_summary_never_says_individuals_cannot_apply():
    # 只说明申请对象，不替具体家庭下"不能申请"的结论。
    notice = _notice(title="坪山区关于面向企（事）业单位配租保障性租赁住房分配结果公示")
    text = " ".join(summary_lines(notice))
    assert "申请对象：企事业单位" in text
    assert "个人不能申请" not in text


def test_personal_summary_asks_to_check_the_original():
    text = " ".join(summary_lines(_notice()))
    assert "申请对象：个人" in text
    assert "是否符合资格需核对原文" in text


def test_requirements_quote_the_source_sentence():
    body = "本次配租面向光明区户籍在册轮候家庭，需符合收入财产限额标准，申请人需在本市缴纳社会保险满 5 年。"
    text = "、".join(requirements(_notice(body_text=body)))
    assert "户籍在册轮候家庭" in text
    assert "收入财产限额" in text
    assert "社会保险满 5 年" in text


def test_negated_requirement_is_quoted_not_inverted():
    # 曾经把「不限户籍，不要求社保满5年」输出成「光明区户籍、社保满5年」，等于把原文说反。
    body = "不限户籍，不要求社保满5年。"
    text = " ".join(summary_lines(_notice(body_text=body)))
    assert "不限户籍" in text
    assert "不要求社保满5年" in text
    # 不能凭空造出肯定式门槛。
    assert "光明区户籍、" not in text
    assert "社保满5年、" not in text


def test_long_clause_is_skipped_rather_than_truncated():
    body = "户籍" + "说明" * 200 + "。"
    assert requirements(_notice(body_text=body)) == []


def test_no_requirements_falls_back_to_the_original():
    text = " ".join(summary_lines(_notice(body_text="本次配租共 1963 套。")))
    assert "资格要求见原文" in text


def test_instant_detail_adds_qualification_line(session):
    notice = _notice(body_text="需符合收入财产限额标准。")
    session.add(notice)
    session.flush()

    _, basic = render_instant([notice], now=NOW)
    _, pro = render_instant([notice], now=NOW, detail=True)

    assert "资格原文" not in basic
    assert "资格原文：需符合收入财产限额标准" in pro
