from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import DeclarativeBase, Session

from app.db import SessionLocal, engine


class HousingBase(DeclarativeBase):
    """独立的 metadata，避免调用股票的 Base.metadata.create_all 时连带建表。"""


def init_housing_db() -> None:
    from app.housing import models  # noqa: F401

    from app.housing.migrations import migrate_schema
    migrate_schema(engine)


@contextmanager
def housing_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
