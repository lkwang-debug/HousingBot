import json
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from app.housing.share_pack import (
    ChromeRenderer, FrozenReport, HEIGHT, WIDTH, export_share_pack, parse_document,
    png_size, read_frozen_report, select_frozen_report, split_body, split_entries,
    text_units, validate_pagination,
)


@pytest.fixture
def frozen_db():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        create table housing_meta(key text primary key,value text);
        create table housing_outbox(id integer primary key,group_id text,kind text,status text,content text);
        create table housing_outbox_events(outbox_id integer,kind text,event_key text);
    """)
    yield connection
    connection.close()


def save_report(db, day="2026-10-23", *, kind="digest", parts=("原文",), statuses=None, groups=None):
    value = {"generated_at": day + "T08:30:02", "parts": [{"content": p} for p in parts]}
    db.execute("insert into housing_meta values (?,?)", (f"report:{kind}:{day}", json.dumps(value)))
    for i, content in enumerate(parts):
        status = (statuses or ["submitted"] * len(parts))[i]
        group = (groups or ["A"] * len(parts))[i]
        cursor = db.execute("insert into housing_outbox(group_id,kind,status,content) values (?,?,?,?)", (group, kind, status, content))
        key = f"{kind}:{day}" + (f":part:{i+1}" if i else "")
        db.execute("insert into housing_outbox_events values (?,?,?)", (cursor.lastrowid, kind, key))


def sample_report(text=None):
    return FrozenReport("report:digest:2026-10-23", "digest", "2026-10-23T08:30:02",
        (text or "## 深圳早报\n\n统计截至08:30。\n\n**【公租房｜个人｜B34】** 初审公示\n\n不限户籍，不要求社保满5年，但特殊项目除外。\n\n[配租原文](https://example.gov.cn/notice?a=1&b=2)",), "record-hash")


def test_loader_reads_exact_frozen_content_without_mutating_delivery_state(frozen_db):
    source = "原始全文 🏠\n\n不要求社保满5年，但特殊项目除外。"
    save_report(frozen_db, parts=(source,))
    before = frozen_db.total_changes
    report = select_frozen_report(frozen_db, "digest", "2026-10-23")
    assert report.markdown == source and report.generated_at == "2026-10-23T08:30:02"
    assert frozen_db.total_changes == before


@pytest.mark.parametrize("status", ["pending", "failed", "needs_review", "cancelled"])
def test_unaccepted_report_is_not_exported_as_an_already_sent_report(frozen_db, status):
    save_report(frozen_db, statuses=[status])
    with pytest.raises(ValueError, match="不会重新采集"):
        select_frozen_report(frozen_db, "digest", "2026-10-23")


def test_latest_morning_and_evening_are_selected_independently(frozen_db):
    save_report(frozen_db, "2026-10-22", kind="evening", parts=("昨晚已发",))
    save_report(frozen_db, "2026-10-23", kind="digest", parts=("今早已发",))
    save_report(frozen_db, "2026-10-23", kind="evening", parts=("尚未发送",), statuses=["pending"])
    assert select_frozen_report(frozen_db, "digest").markdown == "今早已发"
    assert select_frozen_report(frozen_db, "evening").markdown == "昨晚已发"
    with pytest.raises(ValueError): select_frozen_report(frozen_db, "evening", "2026-10-23")


def test_split_report_must_be_complete_for_at_least_one_same_group(frozen_db):
    save_report(frozen_db, parts=("第一部分", "第二部分"), groups=["A", "B"])
    with pytest.raises(ValueError): select_frozen_report(frozen_db)
    frozen_db.execute("update housing_outbox set group_id='A'")
    assert select_frozen_report(frozen_db).parts == ("第一部分", "第二部分")


def test_missing_database_does_not_create_an_empty_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(ValueError): read_frozen_report(database=path)
    assert not path.exists()


def test_markdown_keeps_complete_conditions_and_numbers_and_labels_all_sources():
    report = sample_report()
    document = parse_document(report.markdown + "\n\n[再次查看](https://example.gov.cn/notice?a=1&b=2)\n\n### 查询与说明\n\n[入口](https://example.gov.cn/query)")
    assert "不限户籍，不要求社保满5年，但特殊项目除外。" in document.text
    assert "配租原文〔S01〕" in document.text and "再次查看〔S01〕" in document.text
    assert len(document.links) == 2 and document.links[0].url == "https://example.gov.cn/notice?a=1&b=2"
    assert document.links[0].contexts == ["B34"]
    assert document.links[1].contexts == ["查询与说明"]
    assert document.links[0].labels == ["配租原文", "再次查看"]
    batch = next(g for g in document.groups if g["kind"] == "batch")
    assert len(batch["blocks"]) == 4


def test_bare_url_is_preserved_in_source_list_and_script_is_inert_text():
    doc = parse_document("正文 https://example.gov.cn/test\n\n<script>alert('原文');</script>")
    assert doc.links[0].url == "https://example.gov.cn/test"
    assert "来源〔S01〕" in doc.text and "<script>" in doc.text


def test_unsupported_inline_picture_fails_instead_of_dropping_content():
    with pytest.raises(ValueError, match="拒绝遗漏"):
        parse_document("![必须保留的图](https://example.gov.cn/image.png)")


def test_text_parts_count_emoji_and_headers_and_keep_urls_whole():
    entries = [f"〔S{i:02d}〕官方原文 🏠\nhttps://example.gov.cn/{i}?q=" + "x" * 140 for i in range(20)]
    parts = split_entries(entries, "2026-10-23早报原文链接")
    assert len(parts) > 1 and all(text_units(p) <= 900 for p in parts)
    for entry in entries: assert sum(entry in p for p in parts) == 1
    assert text_units("🏠") == 2


def test_unusually_long_source_url_is_not_cut():
    with pytest.raises(ValueError, match="未截断网址"):
        split_entries(["https://example.gov.cn/" + "x" * 1000], "原文")


def test_backup_text_splitting_is_lossless_even_with_chinese_and_emoji():
    text = ("完整条件：不限户籍，不要求社保满5年，但特殊项目除外。🏠\n\n" * 100)
    parts = split_body(text, "2026-10-23早报正文")
    assert all(text_units(p) <= 900 for p in parts)
    assert "".join(p.split("\n\n", 1)[1] for p in parts) == text


def layout_for(document):
    return {"pages": [{"fits": True, "fragments": [{"block_id": b.id, "text": b.text} for b in document.blocks]}]}


@pytest.mark.parametrize("fault", ["missing", "duplicate", "reordered", "overflow"])
def test_pagination_validation_rejects_omission_duplication_order_and_clipping(fault):
    doc = parse_document("## 早报\n\n第一段\n\n第二段")
    layout = layout_for(doc)
    fragments = layout["pages"][0]["fragments"]
    if fault == "missing": fragments.pop()
    elif fault == "duplicate": fragments.append(dict(fragments[-1]))
    elif fault == "reordered": fragments.reverse()
    else: layout["pages"][0]["fits"] = False
    with pytest.raises(ValueError): validate_pagination(doc, layout)


def write_test_png(path):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * WIDTH * 3) * HEIGHT)) + chunk(b"IEND", b"")
    path.write_bytes(data)


class TestRenderer:
    __test__ = False
    def render(self, report, document, directory):
        (directory / "图片").mkdir()
        write_test_png(directory / "图片/01.png")
        return layout_for(document)


def test_export_is_complete_idempotent_and_does_not_send_or_recollect(tmp_path):
    report = sample_report()
    result = export_share_pack(report, tmp_path, renderer=TestRenderer())
    folder = Path(result["directory"])
    assert (folder / "原始正文.md").read_text(encoding="utf-8") == report.markdown
    assert png_size(folder / "图片/01.png") == (WIDTH, HEIGHT)
    manifest = json.loads((folder / "核对清单.json").read_text(encoding="utf-8"))
    assert manifest["batch_codes"] == ["B34"] and manifest["source_key"] == report.key
    assert all(m["units"] <= 900 for m in manifest["text_messages"])
    assert Path(result["archive"]).is_file()
    assert export_share_pack(report, tmp_path, renderer=TestRenderer())["status"] == "existing"


def test_failed_render_leaves_no_partial_package(tmp_path):
    class Failure:
        def render(self, *args): raise RuntimeError("模拟渲染失败")
    with pytest.raises(RuntimeError): export_share_pack(sample_report(), tmp_path, renderer=Failure())
    assert list(tmp_path.iterdir()) == []


def test_existing_modified_package_is_not_overwritten(tmp_path):
    report = sample_report();result = export_share_pack(report, tmp_path, renderer=TestRenderer())
    path = Path(result["directory"]) / "原始正文.md";path.write_text("人工修改", encoding="utf-8")
    with pytest.raises(ValueError, match="未覆盖"):
        export_share_pack(report, tmp_path, renderer=TestRenderer())
    assert path.read_text(encoding="utf-8") == "人工修改"


@pytest.mark.skipif(not Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").is_file(), reason="Chrome is not installed")
def test_actual_chrome_long_clause_continues_without_losing_words_dates_or_emoji(tmp_path):
    text = "## 早报\n\n### 罗湖区\n\n**【公租房｜个人｜B34】** 完整公示\n\n" + (
        "完整条款，不限户籍，不要求社保满5年，但特殊项目除外。截止2026-10-28 18:00。🏠👨‍👩‍👧‍👦" * 35
    ) + "\n\n[官方原文](https://example.gov.cn/original)"
    report = sample_report(text);doc = parse_document(text)
    layout = ChromeRenderer().render(report, doc, tmp_path)
    validate_pagination(doc, layout)
    assert len(layout["pages"]) >= 3
    images = list((tmp_path / "图片").glob("*.png"))
    assert len(images) == len(layout["pages"]) and all(png_size(p) == (WIDTH, HEIGHT) for p in images)
    # Neither an absolute date/time nor an extended emoji sequence is cut across pages.
    pieces = [f["text"] for p in layout["pages"] for f in p["fragments"]]
    assert sum(p.count("2026-10-28 18:00") for p in pieces) == 35
    assert sum(p.count("👨‍👩‍👧‍👦") for p in pieces) == 35
