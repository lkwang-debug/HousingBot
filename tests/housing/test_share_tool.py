import json
import struct
import zlib
from datetime import date
from pathlib import Path

import pytest

from app.housing.share_pack import HEIGHT, WIDTH, text_units
from app.housing.share_tool import (
    ShareReport, canonical_hash, detect_metadata, export_simple_report, make_report, prepare_document, read_report_file,
)


BODY = """## 🏠 深圳住房管家早报 10-23

统计：10-22 08:30—10-23 08:30（北京时间）

全市去重：认租中3批｜在途22批｜新增0条｜更新13条

### 📍罗湖区

**【公租房｜个人｜B34】** [第三批配租](https://example.gov.cn/b34)

不限户籍，不要求社保满5年，但特殊项目除外。合格403户／不合格35户。

办理：2026-10-22—2026-10-28，18:00截止。🏠👨‍👩‍👧‍👦

办理提示：核对名单｜[配租原文](https://example.gov.cn/start)｜[核查原文](https://example.gov.cn/review)
"""


class Renderer:
    def render(self, report, document, directory):
        (directory / "排版预览.html").write_text("internal only", encoding="utf-8")
        pictures = directory / "图片"
        pictures.mkdir()
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * WIDTH * 3) * HEIGHT)) + chunk(b"IEND", b"")
        (pictures / "01.png").write_bytes(raw)
        return {"pages": [{"fits": True, "fragments": [{"block_id": b.id, "text": b.text} for b in document.blocks]}]}


def report(text=BODY, kind="digest"):
    return make_report(text, kind, "2026-10-23", "08:30")


def test_clean_links_keep_all_substantive_anchor_text_conditions_numbers_and_order():
    doc = prepare_document(BODY)
    assert "第三批配租" in doc.text
    assert "不限户籍，不要求社保满5年，但特殊项目除外。合格403户／不合格35户。" in doc.text
    assert "2026-10-22—2026-10-28，18:00截止。🏠👨‍👩‍👧‍👦" in doc.text
    assert "办理提示：核对名单" in doc.text
    assert all(s not in doc.text for s in ["https", "〔S", "配租原文", "核查原文"])
    assert doc.links == []


def test_plain_text_headings_and_batches_are_grouped_without_losing_fields():
    doc = prepare_document("深圳住房管家早报 10-23\n📍罗湖区\n【公租房｜个人｜B34】第三批\n状态：公示中\n对象：不限户籍\n【公租房｜个人｜B35】第四批\n状态：待核查")
    assert [g["label"] for g in doc.groups if g["kind"] == "batch"] == ["B34", "B35"]
    assert "状态：公示中\n对象：不限户籍" in doc.text
    assert "状态：待核查" in doc.text


def test_links_naked_and_preexisting_markers_are_not_in_output():
    doc = prepare_document("【保租房｜个人｜B1】\n\n完整标题〔S01〕｜https://example.gov.cn/a。\n\n[https://example.gov.cn/b](https://example.gov.cn/b)\n\n[真实条件：不要求满5年](https://example.gov.cn/c)\n\n官方原文：https://example.gov.cn/d\n\n核查原文〔S02〕")
    assert "完整标题" in doc.text and "真实条件：不要求满5年" in doc.text
    assert "https" not in doc.text and "〔S" not in doc.text
    assert "官方原文" not in doc.text and "核查原文" not in doc.text


def test_metadata_never_uses_historical_batch_dates_or_assumes_the_year():
    assert detect_metadata(BODY) == {"kind": "digest", "day": "", "clock": "08:30"}
    meta = detect_metadata(BODY.replace("早报 10-23", "早报 2026-10-23"))
    assert meta == {"kind": "digest", "day": "2026-10-23", "clock": "08:30"}
    pasted = "🏠 深圳住房管家早报 10-23\n群发 2026-10-23 08:30:07\n" + BODY
    assert detect_metadata(pasted)["day"] == "2026-10-23"
    assert detect_metadata("# 早报和晚报\n\n历史窗口2025-01-02")["kind"] == ""


def test_copied_platform_wrapper_and_plain_link_labels_do_not_create_another_report():
    title = "🏠 深圳住房管家早报 10-23"
    markdown = BODY + "\n\n[公告原文](https://example.gov.cn/a)；待核实：配租对象待核实\n\n🔎 [已登记发布的查询入口](https://example.gov.cn/query)（本期暂无已核对可查批次）"
    plain = prepare_document(BODY).text + "\n\n公告原文；待核实：配租对象待核实\n\n🔎 已登记发布的查询入口（本期暂无已核对可查批次）"
    pasted = title + "\n\n群发\n深圳保租房公租房-管家推送\n2026-10-23 08:30:07\n\n" + plain
    assert canonical_hash(prepare_document(markdown)) == canonical_hash(prepare_document(pasted))
    assert "群发" not in prepare_document(pasted).text
    assert "待核实：配租对象待核实" in prepare_document(pasted).text


def test_normalized_input_and_second_precision_reuse_same_files(tmp_path):
    output, work = tmp_path / "成品", tmp_path / "内部"
    first = export_simple_report(report(), output, work=work, renderer=Renderer())
    plain = prepare_document(BODY).text
    same = make_report(plain, "digest", "2026-10-23", "08:30:07")
    class NotCalled:
        def render(self, *args):
            raise AssertionError("相同内容不应重新渲染")
    again = export_simple_report(same, output, work=work, renderer=NotCalled())
    assert again["status"] == "existing" and again["directory"] == first["directory"]


def test_progress_is_numeric_monotonic_and_finishes_at_100_after_publication(tmp_path):
    events = []
    result = export_simple_report(report(), tmp_path / "成品", work=tmp_path / "内部", renderer=Renderer(),
                                 progress=lambda percent, text: events.append((percent, text)))
    assert [p for p, _ in events] == sorted(p for p, _ in events)
    assert events[-1][0] == 100 and Path(result["directory"]).is_dir()
    assert any("保存" in text for _, text in events)


@pytest.mark.parametrize("kind,day,clock", [("", "2026-10-23", ""), ("digest", "", ""), ("digest", "2026-02-30", ""), ("digest", "2026-10-23", "25:00")])
def test_missing_or_invalid_metadata_requires_correction(kind, day, clock):
    with pytest.raises(ValueError):
        make_report(BODY, kind, day, clock)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_file_loading_encodings(tmp_path, encoding):
    value = "深圳早报：不限户籍，不要求社保满5年。"
    path = tmp_path / "中文报表.txt"
    path.write_text(value, encoding=encoding)
    assert read_report_file(path) == value


def test_only_guide_and_png_are_published_and_unchanged_report_reuses_result(tmp_path):
    output, work = tmp_path / "成品", tmp_path / "内部"
    result = export_simple_report(report(), output, work=work, renderer=Renderer())
    folder = Path(result["directory"])
    assert folder.name == "2026-10-23早报"
    assert sorted(p.name for p in folder.iterdir()) == ["01.png", "导读.txt"]
    assert "认租中3批｜在途22批" in result["intro"]
    assert 200 <= len(result["intro"]) <= 400 and text_units(result["intro"]) < 1000
    assert "http" not in result["intro"] and not result["intro"].startswith("\ufeff")
    assert not list(work.glob("render-*"))
    again = export_simple_report(report(), output, work=work, renderer=Renderer())
    assert again["status"] == "existing" and again["intro"] == result["intro"]
    assert list(output.iterdir()) == [folder]


def test_changed_content_and_manually_edited_results_get_new_versions(tmp_path):
    output, work = tmp_path / "成品", tmp_path / "内部"
    one = export_simple_report(report(), output, work=work, renderer=Renderer())
    guide = Path(one["directory"]) / "导读.txt"
    guide.write_text("人工修改", encoding="utf-8")
    two = export_simple_report(report(), output, work=work, renderer=Renderer())
    three = export_simple_report(report(BODY + "\n\n新增加的完整条款"), output, work=work, renderer=Renderer())
    assert Path(two["directory"]).name == "2026-10-23早报-第2版"
    assert Path(three["directory"]).name == "2026-10-23早报-第3版"
    assert guide.read_text(encoding="utf-8") == "人工修改"


def test_failed_generation_and_bad_coverage_publish_nothing_and_preserve_previous(tmp_path):
    output, work = tmp_path / "成品", tmp_path / "内部"
    first = export_simple_report(report(), output, work=work, renderer=Renderer())
    original = (Path(first["directory"]) / "01.png").read_bytes()
    class Bad(Renderer):
        def render(self, *args):
            result = super().render(*args)
            result["pages"][0]["fragments"].pop()
            return result
    with pytest.raises(ValueError):
        export_simple_report(report(BODY + "\n\n不能漏掉"), output, work=work, renderer=Bad())
    assert len(list(output.iterdir())) == 1
    assert (Path(first["directory"]) / "01.png").read_bytes() == original
    assert not list(work.glob("render-*"))


def test_evening_output_and_unknown_clock_do_not_invent_a_generation_time(tmp_path):
    value = make_report(BODY.replace("早报", "晚报"), "evening", "2026-10-22")
    assert not value.show_clock
    result = export_simple_report(value, tmp_path / "成品", work=tmp_path / "内部", renderer=Renderer())
    assert Path(result["directory"]).name == "2026-10-22晚报"
    assert "2026-10-22（北京时间）" in result["intro"] and "00:00" not in result["intro"]


def test_gui_paste_validation_and_buttons_have_no_send_side_effects(monkeypatch):
    import tkinter as tk
    from app.housing import share_gui
    root = tk.Tk()
    root.withdraw()
    clipboard = {"text": ""}
    monkeypatch.setattr(root, "clipboard_clear", lambda: clipboard.update(text=""))
    monkeypatch.setattr(root, "clipboard_append", lambda value: clipboard.update(text=value))
    monkeypatch.setattr(root, "clipboard_get", lambda: clipboard["text"])
    errors = []
    monkeypatch.setattr(share_gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(share_gui.messagebox, "showinfo", lambda *a, **k: None)
    try:
        ui = share_gui.ShareWindow(root)
        ui.set_input(BODY)
        assert ui.kind.get() == "早报" and ui.day.get() == ""
        ui.generate()
        assert errors and not ui.busy
        ui.day.set("2026-10-23")
        ui.result = {"intro": "测试导读", "directory": "unused"}
        ui.copy_intro()
        assert root.clipboard_get() == "测试导读"
        ui.busy = True
        ui.refresh_controls()
        assert str(ui.generate_button["state"]) == "disabled"
        ui.generate()
        assert ui.busy
    finally:
        ui.busy = False
        ui.close()


def test_gui_background_load_generate_and_failure_preserve_success(tmp_path, monkeypatch):
    import gc
    import time
    import tkinter as tk
    from app.housing import share_gui
    from app.housing.share_pack import FrozenReport
    # Release the previous test's destroyed Tcl objects on their owning thread.
    gc.collect()
    root = tk.Tk()
    root.withdraw()
    errors = []
    monkeypatch.setattr(share_gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    notices = []
    monkeypatch.setattr(share_gui.messagebox, "showinfo", lambda *a, **k: notices.append(a))
    monkeypatch.setattr(share_gui, "read_frozen_report", lambda kind: FrozenReport("test", kind, "2026-10-23T08:30:07", (BODY,), "frozen"))
    monkeypatch.setattr(share_gui, "export_simple_report", lambda report, output, progress, target_pages: export_simple_report(
        report, output, work=tmp_path / "工作", renderer=Renderer(), progress=progress, target_pages=target_pages))
    try:
        ui = share_gui.ShareWindow(root)
        def finish():
            deadline = time.monotonic() + 5
            while ui.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            assert not ui.busy
        ui.load_latest("digest")
        finish()
        assert ui.day.get() == "2026-10-23" and ui.kind.get() == "早报"
        ui.output.set(str(tmp_path / "成品"))
        ui.generate()
        finish()
        first = ui.result
        assert Path(first["directory"]).is_dir()
        assert str(ui.copy_button["state"]) == "normal"
        assert ui.percent.get() == "100%" and float(ui.progress["value"]) == 100
        assert notices and "完成" in ui.summary.get()
        monkeypatch.setattr(share_gui, "export_simple_report", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟失败")))
        ui.generate()
        finish()
        assert errors and ui.result == first and Path(first["directory"]).is_dir()
    finally:
        ui.busy = False
        ui.close()


@pytest.mark.skipif(not Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").is_file(), reason="Chrome is not installed")
def test_real_chrome_targets_fewer_long_images_without_small_font_or_missing_batches(tmp_path):
    from app.housing.share_pack import png_size
    text = "## 🏠 深圳住房管家早报 10-23\n\n"
    for i in range(1, 19):
        text += f"### 📍测试区{i}\n\n【公租房｜个人｜B{i}】测试批次\n\n" + (
            "完整条件：不限户籍，不要求社保满5年，但特殊项目除外。截止2026-10-28 18:00。🏠👨‍👩‍👧‍👦" * 2
        ) + "\n\n"
    events = []
    result = export_simple_report(report(text), tmp_path / "成品", work=tmp_path / "内部", target_pages=4,
                                 progress=lambda percent, message: events.append((percent, message)))
    assert result["pages"] <= 4 and result["image_height"] > HEIGHT
    assert all(png_size(p) == (WIDTH, result["image_height"]) for p in Path(result["directory"]).glob("*.png"))
    record = next((tmp_path / "内部/manifests").glob("*.json"))
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert saved["batch_codes"] == [f"B{i}" for i in range(1, 19)]
    assert [p for p, _ in events] == sorted(p for p, _ in events)
    assert any("/" in message and "正在生成第" in message for _, message in events)
    # An unrealistic target never causes truncation or unreadably small text.
    limited = export_simple_report(report(text * 3), tmp_path / "成品", work=tmp_path / "内部", target_pages=2)
    assert limited["pages"] > 2 and limited["image_height"] == 4320
    assert sorted(p.name for p in Path(limited["directory"]).glob("*.png")) == [f"{i:02d}.png" for i in range(1, limited["pages"] + 1)]
