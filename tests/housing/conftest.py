from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.housing import models  # noqa: F401  建表前必须导入模型
from app.housing.db import HousingBase
from app.housing.models import HousingSource


@pytest.fixture(scope="session", autouse=True)
def _shared_schema():
    """渲染时会自己开 SessionLocal()（不看用例传进来的 session），
    所以全局引擎指向的测试库也必须有表，否则查 housing_meta 直接报 no such table。"""
    from app.db import engine as shared_engine

    HousingBase.metadata.create_all(shared_engine)
    yield


@pytest.fixture()
def lookup_published(session):
    """登记一份"已上传并人工确认"的查询页版本。

    推送里的查询入口现在受 published_ready 门禁控制，没登记的批次不给链接。
    """

    def _mark(*notices):
        from app.housing.lookup import source_version
        from app.housing.models import HousingMeta

        record = session.get(HousingMeta, "lookup-published")
        if record is None:
            record = HousingMeta(key="lookup-published", value={"notices": {}})
            session.add(record)
        published = dict((record.value or {}).get("notices", {}))
        for notice in notices:
            published[str(notice.id)] = source_version(session, notice)
        if not published:
            # 只是"上传过一版页面"，没有任何当前批次可查。
            published["0"] = "placeholder"
        record.value = {"version": "test", "published_at": "2026-10-20T09:00:00", "notices": published}
        session.flush()
        return record

    return _mark


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    HousingBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def source(session):
    row = HousingSource(
        key="test_district",
        name="测试区住建局",
        level="district",
        region_code="光明区",
        entry_url="https://www.szgm.gov.cn/example/",
        priority=90,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def city_source(session):
    row = HousingSource(
        key="test_city",
        name="市级住建局",
        level="city",
        region_code="",
        entry_url="https://zjj.sz.gov.cn/example/",
        priority=95,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def published():
    return datetime(2026, 10, 8, 10, 0)
