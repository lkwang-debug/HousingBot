from __future__ import annotations

import io
import pytest

from app.housing.lottery import (
    LotteryRow,
    _has_queue_column,
    _parse_pages,
    aggregate,
    category_display,
    category_key,
    parse_block_rows,
    parse_excel_rows,
    parse_text_rows,
    parse_units_overview,
    queue_prefix,
)


class _FakePage:
    """只实现 pypdf 页面里我们用到的 extract_text。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self, **_kwargs) -> str:
        return self._text


def test_names_with_spaces_do_not_disappear_from_rank_counts():
    rows=parse_text_rows('1 544 TEST000001 测试 姓名 4403011990******** 第三队列\n1 547 TEST000002 测试者 4403011991******** 第二队列')
    assert [(r.order,r.apply_no,r.queue) for r in rows]==[(544,'TEST000001','第三队列'),(547,'TEST000002','第二队列')]


def test_pdf_name_and_masked_identity_can_be_joined_without_a_space():
    text='1 544 TEST000001 测试 者4403011990******** 第三队列\n2 1885 TEST000003 测试者H1234567******** 第二队列'
    rows=parse_text_rows(text)
    assert [(r.order,r.apply_no,r.queue) for r in rows]==[(544,'TEST000001','第三队列'),(1885,'TEST000003','第二队列')]


def test_layout_column_padding_does_not_drop_roster_rows_or_use_serial_as_order():
    text='1 TEST000001 测试者 4403011990******** 主申请人 建筑面积70㎡左右的房源'+(' '*100)+'第一队列 14'
    rows=_parse_pages([_FakePage(text)],layout=True)
    assert len(rows)==1 and rows[0].order==14 and rows[0].queue=='第一队列'


def test_machine_round_six_and_twelve_are_not_dropped_or_invented_as_queues():
    text='轮数 顺序号 认租编号\n6 5001 TEST000001\n12 11001 TEST000002'
    rows=_parse_pages([_FakePage(text)],layout=False)
    assert [(r.round,r.order,r.queue) for r in rows]==[(6,5001,''),(12,11001,'')]


def test_queue_annotation_is_detected_without_storing_qualification_text():
    rows=parse_text_rows('1 138 TEST000001 测试者 4403012001********第三队列（累计缴纳社会保险未满1年）')
    assert len(rows)==1 and rows[0].queue=='第三队列' and rows[0].annotated
    assert not hasattr(rows[0],'qualification_text')


def test_recognized_but_unparsed_applicant_makes_roster_incomplete():
    from app.housing.lottery import IncompleteLotteryRows
    page=_FakePage('轮数 顺序号 认租编号 姓名 身份证号码 队列分类\n1 544 TEST000001 特殊破损格式，无法确认顺序字段')
    with pytest.raises(IncompleteLotteryRows,match='不能发布完整统计'):
        _parse_pages([page],layout=False)


def test_rows_with_name_and_queue():
    text = "1 1 SQ2026BZ360007741 何光宇 4311281990******** 第三队列"
    rows = parse_text_rows(text)
    assert len(rows) == 1
    assert rows[0].order == 1
    assert rows[0].apply_no == "SQ2026BZ360007741"
    assert rows[0].queue == "第三队列"


def test_compact_rows_use_first_column_as_queue():
    # 坪山/福田公证摇号结果只有 队列 顺序号 认租编号 三列。
    text = "   1    1    SQ2026BZ370001675\n   2    1001 SQ2026BZ370000595"
    rows = parse_text_rows(text)
    assert [row.queue for row in rows] == ["第一队列", "第二队列"]
    assert [row.order for row in rows] == [1, 1001]


def test_pair_rows_when_queue_column_is_blank():
    # 坪山有的批次表头有"队列"，但整列留空，只剩顺序号和认租编号。
    text = "1 SQ2025BZ150001980\n2 SQ2025BZ150002721"
    rows = parse_text_rows(text)
    assert [(row.order, row.apply_no, row.queue) for row in rows] == [
        (1, "SQ2025BZ150001980", ""),
        (2, "SQ2025BZ150002721", ""),
    ]


def test_pair_rows_are_disabled_on_masked_pages():
    # 多行版式里"1 BHR00085841"的 1 是序号，不是顺序号。
    text = "1 BHR00085841\n张三 本市户籍 4403011987********\n5 20777"
    assert parse_text_rows(text, allow_pairs=False) == []
    assert parse_text_rows(text)  # 允许时会被误读，所以调用方要看掩码


def test_team_rows_use_in_queue_order():
    # 宝安选房名单：选房排位是全表号，队内摇号结果才是同队列里的位次。
    text = (
        "1 SQ2025BZ190003650 张*芸 140411********4821 主申请人 第一队列 1\n"
        "2 SQ2025BZ190001136 田*艳 522222********0027 主申请人 第一队列 2\n"
    )
    rows = parse_text_rows(text, allow_pairs=False)
    assert [(row.order, row.apply_no, row.queue) for row in rows] == [
        (1, "SQ2025BZ190003650", "第一队列"),
        (2, "SQ2025BZ190001136", "第一队列"),
    ]


def test_team_row_falls_back_to_position_when_inner_order_missing():
    text = "7 SQ2025BZ190004112 陈*烨 440902********322X 主申请人 第二队列\n"
    rows = parse_text_rows(text, allow_pairs=False)
    assert [(row.order, row.queue) for row in rows] == [(7, "第二队列")]


def test_attachment_kind_falls_back_to_file_name():
    from app.housing.lottery import _attachment_kind

    assert _attachment_kind({"kind": "file", "name": "附件1：入围名单.et"}) == "et"
    assert _attachment_kind({"kind": "file", "name": "名单", "url": "https://x/a.pdf"}) == "pdf"
    assert _attachment_kind({"kind": "pdf", "name": "x.et"}) == "pdf"
    assert _attachment_kind({"kind": "file", "name": "名单", "url": "https://x/download"}) == "file"


def test_block_order_column_without_people_count():
    # 南山/罗湖的排位列自己一列，前面没有家庭人口数。
    text = (
        "1       BHR00585522   张三  本市户籍  4307211994********   304936  1\n"
        "2       GLH01053397   李四  本市户籍  4323011970********   394546  2\n"
    )
    rows = parse_block_rows(text)
    assert [(row.apply_no, row.order) for row in rows] == [
        ("BHR00585522", 304936),
        ("GLH01053397", 394546),
    ]


def test_block_rows_from_selection_list():
    text = """附件1
1       BHR00085841
                    张三       本市户籍       4403011987********
5       20777
第二队列
2       BHR00133224
                    李四       本市户籍       4415221987********
6       36268
第二队列
"""
    rows = parse_block_rows(text)
    assert [(row.apply_no, row.order, row.queue) for row in rows] == [
        ("BHR00085841", 20777, "第二队列"),
        ("BHR00133224", 36268, "第二队列"),
    ]


def test_block_rows_skip_blocks_without_order():
    text = "1       BHR00085841\n姓名 本市户籍 4403011987********\n"
    assert parse_block_rows(text) == []


def test_round_header_is_not_a_queue_column():
    # 华章新筑表头是「轮数 顺序号 认租编号」：首列是第几轮，有的家庭摇了两轮。
    # 当成队列读，同一队会被拆成第一/第二队列两组，排位全错。
    text = (
        "华章新筑一房户型第一队列摇号结果\n"
        "轮数 顺序号 认租编号\n"
        "1 1 SQ2025BZ160006326\n"
        "1 2 SQ2025BZ160000841\n"
        "2 3 SQ2025BZ160007254"
    )
    assert _has_queue_column([_FakePage(text)], layout=False) is False
    rows = _parse_pages([_FakePage(text)], layout=False)
    assert [row.queue for row in rows] == ["", "", ""]
    assert [row.order for row in rows] == [1, 2, 3]


def test_round_header_split_across_lines_is_still_recognized():
    # 坪山有的 PDF 一个单元格一行：表头三行分开，同样是「轮数」不是「队列」。
    text = (
        "安居凤凰苑等四个保障性租赁住房项目两房配租顺序摇号\n"
        "轮数\n顺序号\n认租编号\n"
        "1 684 SQ2025BZ150000071"
    )
    assert _has_queue_column([_FakePage(text)], layout=False) is False


def test_queue_header_still_reads_compact_rows():
    text = "队列 顺序号 认租编号\n   1    1    SQ2026BZ370001675"
    assert _has_queue_column([_FakePage(text)], layout=False) is True
    rows = _parse_pages([_FakePage(text)], layout=False)
    assert [row.queue for row in rows] == ["第一队列"]


def test_attachment_label_wins_over_wrong_queue_column():
    # 附件名写明「第二队列」，表里读出来的列不可信时以附件名为准。
    rows = [
        LotteryRow(order=1, apply_no="SQ2025BZ160006326", queue="第一队列"),
        LotteryRow(order=2, apply_no="SQ2025BZ160000841", queue="第四队列"),
    ]
    agg = aggregate(rows, "附件2：华章新筑一房户型第二队列摇号结果.pdf")
    assert agg.queue_counts == {"第二队列": 2}
    assert agg.queue_ranges == {"第二队列": [1, 2]}


def test_priority_prefix_survives_missing_queue_column():
    # 福田按优先/其他拆文件但名单里没有队列列，两组人不能都叫「未标注」混在一起。
    rows = [LotteryRow(order=1, apply_no="SQ2026BZ310002959")]
    assert aggregate(rows, "2.其他配租队列摇号结果.pdf").queue_counts == {"其他-未标注": 1}


def test_aggregate_counts_and_ranges():
    rows = parse_text_rows(
        "   1    1    SQ2026BZ370001675\n"
        "   1    2    SQ2026BZ370000595\n"
        "   2    1001 SQ2026BZ370001691\n"
    )
    agg = aggregate(rows, "附件1：两房户型认租申请家庭公证摇号结果.pdf")
    assert agg is not None
    assert agg.category_key == "两房"
    assert agg.rows == 3
    assert agg.max_order == 1001
    assert agg.queue_counts == {"第一队列": 2, "第二队列": 1}
    assert agg.queue_ranges == {"第一队列": [1, 2], "第二队列": [1001, 1001]}


def test_queue_prefix_keeps_priority_groups_apart():
    # 福田按优先/其他配租队列拆文件，两边队列号都从 1 开始，不能直接相加。
    assert queue_prefix("1.优先配租队列摇号对象结果.pdf") == "优先-"
    assert queue_prefix("2.其他配租队列摇号结果.pdf") == "其他-"
    assert queue_prefix("附件1：两房户型公证摇号结果.pdf") == ""
    rows = parse_text_rows("   1    1    SQ2026BZ310002959\n   1    2    SQ2026BZ310005110\n")
    assert aggregate(rows, "2.其他配租队列摇号结果.pdf").queue_counts == {"其他-第一队列": 2}


def test_category_mapping():
    assert category_key("附件1：两房户型认租申请家庭公证摇号结果.pdf") == "两房"
    assert category_key("附件2：三房户型认租申请家庭公证摇号结果.pdf") == "三房"
    assert category_key("住房小三房户型队列公证摇号结果.pdf") == "小三房"
    assert category_key("40㎡左右户型摇号结果") == "40"
    assert category_key("100平方米及以上摇号结果") == "100+"
    # 认不出户型的名单不再并成 "all"，给一个按附件名固定的 key，避免不同档被混成一组。
    unknown = category_key("入围选房家庭名单及排位.pdf")
    assert unknown.startswith("unknown:")
    assert unknown == category_key("入围选房家庭名单及排位.pdf")
    assert unknown != category_key("选房家庭名单及排位.pdf")


def test_category_display_strips_attachment_noise():
    label = "附件1：40㎡左右户型认租申请家庭公证摇号结果.pdf"
    assert category_display(label, "40") == "40㎡左右户型"


def test_units_overview_all_brackets():
    text = (
        "房源概况：40平方米左右房源111套，50平方米左右房源76套，"
        "90平方米左右房源549套，100平方米及以上房源1227套。"
    )
    assert parse_units_overview(text) == {
        "40": ("40左右", 111),
        "50": ("50左右", 76),
        "90": ("90左右", 549),
        "100+": ("100及以上", 1227),
    }


def test_excel_rows_with_header_aliases():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["序号", "认租编号", "姓名", "选房排序号", "队列"])
    sheet.append([1, "BHR00085841", "张三", 20777, "第二队列"])
    sheet.append([2, "BHR00133224", "李四", 36268, "第二队列"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    rows = parse_excel_rows(buffer.getvalue(), "xlsx")
    assert [(row.order, row.apply_no, row.queue) for row in rows] == [
        (20777, "BHR00085841", "第二队列"),
        (36268, "BHR00133224", "第二队列"),
    ]


def test_excel_without_header_returns_nothing():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["房源明细表"])
    sheet.append(["项目", "房号", "面积"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    assert parse_excel_rows(buffer.getvalue(), "xlsx") == []
