from __future__ import annotations

import pytest

from app.housing.forecast import (
    format_applicant,
    predict_whatif,
    queue_odds,
    queue_rank,
    queue_rules,
    units_left_before,
    verdict,
)

COUNTS = {"第一队列": 330, "第二队列": 741, "第三队列": 2673}


def test_queue_odds_consumes_units_in_queue_order():
    odds = queue_odds(111, COUNTS)
    assert [item.queue for item in odds] == ["第一队列", "第二队列", "第三队列"]
    assert odds[0].units_left == 111
    assert odds[0].chance == round(111 / 330, 3)
    # 第一队列没抢完的 0 套，后面队列自然为 0。
    assert odds[1].units_left == 0
    assert odds[1].chance == 0.0
    assert odds[2].chance == 0.0


def test_queue_odds_handles_overflow_into_next_queue():
    odds = queue_odds(500, {"第一队列": 100, "第二队列": 1000})
    assert odds[0].chance == 1.0
    assert odds[0].units_left == 500
    assert odds[1].units_left == 400
    assert odds[1].chance == round(400 / 1000, 3)


def test_queue_odds_without_units():
    assert queue_odds(None, COUNTS) == []
    assert queue_odds(0, COUNTS) == []


def test_units_left_before():
    assert units_left_before(COUNTS, 111, "第一队列") == 111
    assert units_left_before(COUNTS, 111, "第二队列") == 0
    assert units_left_before(COUNTS, 2000, "第三队列") == 2000 - 330 - 741
    assert units_left_before(COUNTS, None, "第一队列") is None


def test_queue_rank_counts_same_queue_up_to_order():
    orders = list(range(1, 11))
    assert queue_rank(orders, 1) == 1
    assert queue_rank(orders, 5) == 5
    assert queue_rank(orders, 99) == 10


def test_verdict_is_a_disclaimer_not_a_prediction():
    # 曾经按供需比例给出「基本稳」，被当成个人成功概率，已改成固定免责口径。
    for chance in (1.0, 0.6, 0.3, 0.1):
        assert verdict(chance) == "仅为名额供需比例，不是个人成功概率"


def test_queue_rules_from_launch_text():
    body = "第一队列：户籍在册轮候家庭。第二队列：其他符合条件家庭。"
    rules = queue_rules(body)
    assert rules["第一队列"] == "户籍在册轮候家庭"
    assert rules["第二队列"] == "其他符合条件家庭"


def test_format_applicant_inside_range():
    lines = format_applicant("40㎡左右", 111, COUNTS, "第一队列", 5, 5, 3744)
    text = "\n".join(lines)
    assert "本档房源：111 套" in text
    assert "你在本队列排第 5 位" in text
    assert "在无淘汰、无放弃的假设下位于名额范围内" in text


def test_format_applicant_waiting_on_giveups():
    lines = format_applicant("40㎡左右", 111, COUNTS, "第一队列", 118, 118, 3744)
    text = "\n".join(lines)
    assert "前面至少约 7 户退出才可能轮到" in text


def test_format_applicant_far_behind():
    lines = format_applicant("40㎡左右", 111, COUNTS, "第一队列", 2000, 2000, 3744)
    text = "\n".join(lines)
    assert "前面至少约 1889 户退出才可能轮到" in text
    assert "实际以选房通知为准" in text


def test_format_applicant_later_queue_has_no_units():
    lines = format_applicant("40㎡左右", 111, COUNTS, "第三队列", 10, 10, 3744)
    text = "\n".join(lines)
    assert "前序队列已占用全部名额" in text


def test_format_applicant_without_units_is_explicit():
    lines = format_applicant("两房", None, COUNTS, "第一队列", 10, 10, 3744)
    text = "\n".join(lines)
    assert "本档房源数或批次/名单尚未核实，无法估算" in text


def test_format_applicant_never_claims_certainty():
    # 对外不能出现"基本稳/基本无望"这类确定性标签。
    for rank in (1, 100, 2000):
        text = "\n".join(format_applicant("40㎡左右", 111, COUNTS, "第一队列", rank, rank, 3744))
        for word in ("基本稳", "基本无望", "保证", "一定"):
            assert word not in text


def test_predict_whatif_uses_hypothetical_rank():
    result = predict_whatif(111, COUNTS, "第一队列", rank=5)
    assert result["status"] == "ok"
    assert result["rank"] == 5
    assert result["units_left"] == 111
    assert "你在本队列排第 5 位" in "\n".join(result["lines"])


def test_predict_whatif_refuses_unverified_order():
    # 顺序号必须在真实名单里核实过；核实不了就不生成虚拟排位。
    result = predict_whatif(111, COUNTS, "第一队列", order=9999)
    assert result["status"] == "unverified"
    assert result["rank"] is None
    assert "不生成虚拟排位" in "\n".join(result["lines"])


def test_predict_whatif_rejects_two_rank_sources():
    with pytest.raises(ValueError):
        predict_whatif(111, COUNTS, "第一队列", 5, rank=5)


def test_predict_whatif_rejects_rank_outside_queue():
    with pytest.raises(ValueError):
        predict_whatif(111, COUNTS, "第一队列", rank=9999)


def test_predict_whatif_unknown_units():
    result = predict_whatif(None, COUNTS, "第一队列", rank=5)
    assert result["status"] == "ok"
    assert result["units_left"] is None
    assert "无法估算" in "\n".join(result["lines"])
