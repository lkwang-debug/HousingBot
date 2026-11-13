from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from app.config import get_settings
from app.db import sqlite_path
from app.housing.models import utcnow


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"time": utcnow().isoformat(), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    root = logging.getLogger()
    if any(getattr(h, "housing_handler", False) for h in root.handlers):
        return
    directory = get_settings().project_root / "logs"
    directory.mkdir(exist_ok=True)
    handler = logging.FileHandler(directory / f"housing-{utcnow():%Y-%m-%d}.jsonl", encoding="utf-8")
    handler.housing_handler = True
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(get_settings().log_level)
    for path in list(directory.glob("housing-*.jsonl")) + list(directory.glob("task-*.log")):
        if path.stat().st_mtime < (utcnow() - timedelta(days=90)).timestamp():
            path.unlink()


def backup_database(*, force=False):
    source = sqlite_path()
    if not source or not source.exists():
        return None
    directory = get_settings().data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"housing-{utcnow():%Y-%m-%d}.db"
    if force:
        target = directory / f"housing-{utcnow():%Y-%m-%d-%H%M%S-%f}.db"
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("数据库备份校验失败")
        temporary.replace(target)
    for path in directory.glob("housing-*.db"):
        if path.stat().st_mtime < (utcnow() - timedelta(days=14)).timestamp():
            path.unlink()
    return str(target)
