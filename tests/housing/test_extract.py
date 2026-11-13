from __future__ import annotations

from datetime import datetime

import pytest

from app.housing.extract import extract_doc_number, extract_units, extract_rent_window


@pytest.mark.parametrize(
    ("text", "published", "start", "end", "inferred"),
    [
        (
            "网上认租时间为2026年10月8日9:00至2026年10月13日18:00。",
            datetime(2026, 10, 8),
            datetime(2026, 10, 8, 9, 0),
            datetime(2026, 10, 13, 18, 0),
            False,
        ),
        (
            "网上认租时间为10月8日至10月13日。",
            datetime(2026, 10, 8),
            datetime(2026, 10, 8, 9, 0),
            datetime(2026, 10, 13, 18, 0),
            True,
        ),
        (
            "网上认租时间：2026年12月20日9：00—2027年1月5日18：00",
            datetime(2026, 12, 18),
            datetime(2026, 12, 20, 9, 0),
            datetime(2027, 1, 5, 18, 0),
            False,
        ),
        (
            "网上认租时间为10月8日9:00至13日18:00",
            datetime(2026, 10, 8),
            datetime(2026, 10, 8, 9, 0),
            datetime(2026, 10, 13, 18, 0),
            False,
        ),
        (
            "网上认租时间为12月28日9时起至1月5日18时",
            datetime(2026, 12, 25),
            datetime(2026, 12, 28, 9, 0),
            datetime(2027, 1, 5, 18, 0),
            False,
        ),
    ],
)
def test_rent_window_ranges(text, published, start, end, inferred):
    window = extract_rent_window(text, published)
    assert (window.start, window.end, window.inferred) == (start, end, inferred)


def test_deadline_only_sets_end():
    window = extract_rent_window("认租截止时间为2026年10月22日18:00", datetime(2026, 10, 8))
    assert window.start is None
    assert window.end == datetime(2026, 10, 22, 18, 0)


def test_deadline_is_not_paired_with_phone_hyphen_and_signature_date():
    text = "本次认租时间截至12月28日18:00。\n咨询电话：0755-29102063。\n2025年12月23日"
    window = extract_rent_window(text, datetime(2025,12,23))
    assert window.start is None and window.end == datetime(2025,12,28,18)


def test_missing_year_uses_published_year():
    # 发布在 12 月底、认租窗口跨到次年 1 月时不能生成“过去的”时间。
    window = extract_rent_window(
        "受理时间为12月28日至1月5日", datetime(2026, 12, 25)
    )
    assert window.start == datetime(2026, 12, 28, 9, 0)
    assert window.end == datetime(2027, 1, 5, 18, 0)


def test_units_and_doc_number():
    body = "本批次共配租房源1963套，文号住保租〔2026〕038号。"
    assert extract_units(body) == 1963
    assert extract_doc_number(body) == "住保租〔2026〕038号"


def test_units_ignores_absurd_value():
    assert extract_units("共 999999999 套") is None
