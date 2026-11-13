from __future__ import annotations

from sqlalchemy import func, select

from app.housing.models import HousingExitRecord, HousingNotice, HousingNoticeOccurrence
from app.housing.store import store_notice

RENT_TITLE = "光明区住房和建设局关于2026年第一批次面向个人配租保障性租赁住房的通告（住保租〔2026〕038号）"

RENT_BODY = (
    "光明区2026年第一批次面向个人配租保障性租赁住房有关事项通告如下。"
    "网上认租时间为2026年10月8日9:00至2026年10月13日18:00。"
    "本批次共提供房源1963套。"
)

PROJECT_TABLE = """
<table>
  <tr><th>序号</th><th>小区名称</th><th>房型</th><th>套数</th></tr>
  <tr><td>1</td><td>明皓府</td><td>二房</td><td>120</td></tr>
  <tr><td>2</td><td>80-84㎡左右</td><td>三房</td><td>60</td></tr>
</table>
"""

EXIT_TABLE = """
<table>
  <tr><th>序号</th><th>承租人</th><th>住房类型</th><th>面积</th><th>小区名称</th></tr>
  <tr><td>1</td><td>张三</td><td>公租房</td><td>68.5</td><td>龙悦居二期</td></tr>
</table>
"""


def test_rent_notice_fields_and_projects(session, source, published):
    notice, created = store_notice(
        session,
        source=source,
        title=RENT_TITLE,
        url="https://www.szgm.gov.cn/x/post_10000001.html",
        published_at=published,
        body_text=RENT_BODY,
        content_html=PROJECT_TABLE,
        detail_status="ok",
    )
    assert created is True
    assert notice.category == "rent_launch"
    assert notice.units_count == 1963
    assert notice.application_start.isoformat() == "2026-10-08T09:00:00"
    assert notice.application_end.isoformat() == "2026-10-13T18:00:00"
    # 表头/面积混进项目名时要被过滤掉
    assert notice.projects == ["明皓府"]


def test_same_notice_from_two_sources_merges(session, source, city_source, published):
    url_city = "https://zjj.sz.gov.cn/a/post_20000002.html"
    url_district = "https://www.szgm.gov.cn/b/post_20000002.html"
    first, created_first = store_notice(
        session,
        source=city_source,
        title=RENT_TITLE,
        url=url_city,
        published_at=published,
    )
    second, created_second = store_notice(
        session,
        source=source,
        title=RENT_TITLE,
        url=url_district,
        published_at=published,
    )
    session.flush()
    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.occurrence_count == 2
    assert session.scalar(select(func.count(HousingNoticeOccurrence.id))) == 2
    assert session.scalar(select(func.count(HousingNotice.id))) == 1


def test_same_doc_number_different_titles_stay_separate(session, source, published):
    store_notice(
        session,
        source=source,
        title="光明区住房和建设局关于2026年第一批次配租公证摇号的通告",
        url="https://www.szgm.gov.cn/x/post_30000001.html",
        published_at=published,
    )
    store_notice(
        session,
        source=source,
        title="光明区住房和建设局关于2026年第一批次配租公证摇号结果的通告",
        url="https://www.szgm.gov.cn/x/post_30000002.html",
        published_at=published,
    )
    assert session.scalar(select(func.count(HousingNotice.id))) == 2


def test_rent_notice_tables_are_not_exit_records(session, source, published):
    store_notice(
        session,
        source=source,
        title=RENT_TITLE,
        url="https://www.szgm.gov.cn/x/post_40000001.html",
        published_at=published,
        body_text=RENT_BODY,
        content_html=PROJECT_TABLE,
        detail_status="ok",
    )
    assert session.scalar(select(func.count(HousingExitRecord.id))) == 0


def test_exit_table_stores_no_personal_name(session, source, published):
    notice, _ = store_notice(
        session,
        source=source,
        title="罗湖区2026年第三季度保障性住房退出信息一览表",
        url="https://www.szlh.gov.cn/x/post_50000001.html",
        published_at=published,
        body_text="现将本季度保障性住房退出信息公布如下",
        content_html=EXIT_TABLE,
        detail_status="ok",
    )
    session.flush()
    rows = session.scalars(select(HousingExitRecord)).all()
    assert notice.category == "vacancy_exit"
    assert len(rows) == 1
    row = rows[0]
    assert row.community_name == "龙悦居二期"
    assert row.housing_type == "公租房"
    assert row.area_sqm == 68.5
    assert "张三" not in str(row.__dict__)


def test_title_date_suffix_is_cleaned(session, source, published):
    notice, _ = store_notice(
        session,
        source=source,
        title="坪山区2026年第一批次配租通告 2026-07-20",
        url="https://www.szpsq.gov.cn/x/post_60000001.html",
        published_at=published,
    )
    assert notice.title == "坪山区2026年第一批次配租通告"
