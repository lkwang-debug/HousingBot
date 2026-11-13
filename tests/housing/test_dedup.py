from __future__ import annotations

from datetime import datetime

from app.housing.dedup import (
    canonical_key,
    clean_title,
    merge_key,
    normalize_url,
    title_similarity,
)


def test_canonical_key_ignores_scheme_host_case_and_fragment():
    a = canonical_key("https://www.szgm.gov.cn/gmzfhjsjcsgx/gkmlpt/content/1/1234/post_12345678.html")
    b = canonical_key("http://WWW.SZGM.GOV.CN/gmzfhjsjcsgx/gkmlpt/content/1/1234/post_12345678.html#top")
    assert a == b == "www.szgm.gov.cn:post:12345678"


def test_canonical_key_supports_mpost_variant():
    assert canonical_key("https://zjj.sz.gov.cn/x/mpost_12589448.html") == canonical_key(
        "https://zjj.sz.gov.cn/y/post_12589448.html"
    )


def test_canonical_key_falls_back_to_url_hash():
    left = canonical_key("https://example.gov.cn/a/index.html")
    right = canonical_key("https://example.gov.cn/a/")
    assert left == right
    assert "post:" not in left


def test_normalize_url_collapses_duplicate_slashes():
    assert normalize_url("https://a.gov.cn//b//c") == "https://a.gov.cn/b/c"


def test_clean_title_strips_trailing_date():
    assert clean_title("坪山区2026年第一批次配租通告 2026-07-20") == "坪山区2026年第一批次配租通告"
    assert clean_title("普通标题") == "普通标题"


def test_merge_key_same_doc_number_same_day():
    published = datetime(2026, 10, 8)
    left = merge_key("住保租〔2026〕038号", "光明区配租通告", published.date())
    right = merge_key("住保租〔2026〕038号", "光明区配租通告（转载）", published.date())
    assert left == right


def test_title_similarity_rejects_batch_siblings():
    # 同批次文号下的“摇号的通告”和“摇号结果的通告”是两条不同公告。
    assert (
        title_similarity(
            "光明区住房和建设局关于2026年第一批次配租公证摇号的通告",
            "光明区住房和建设局关于2026年第一批次配租公证摇号结果的通告",
        )
        < 0.98
    )
