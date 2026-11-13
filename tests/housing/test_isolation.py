from __future__ import annotations

import importlib

from app.db import Base as StockBase


def test_housing_tables_not_in_stock_metadata():
    import app.housing.models  # noqa: F401

    housing_tables = {name for name in StockBase.metadata.tables if name.startswith("housing_")}
    assert housing_tables == set()


def test_housing_metadata_is_separate():
    from app.housing import models as housing_models
    from app.housing.db import HousingBase

    assert HousingBase.metadata is not StockBase.metadata
    assert set(HousingBase.metadata.tables) >= {"housing_notices", "housing_sources"}
    assert not set(HousingBase.metadata.tables) & set(StockBase.metadata.tables)


def test_reimport_is_idempotent():
    first = importlib.import_module("app.housing.models")
    second = importlib.import_module("app.housing.models")
    assert first.HousingNotice is second.HousingNotice
