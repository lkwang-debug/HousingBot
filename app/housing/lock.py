from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.housing.config import get_housing_settings


class LockBusy(RuntimeError):
    pass


@contextmanager
def scan_lock(
    path: Path | None = None, stale_minutes: int | None = None
) -> Iterator[Path]:
    settings = get_housing_settings()
    path = path or settings.lock_path
    stale_minutes = stale_minutes or settings.scan_lock_timeout_minutes
    path.parent.mkdir(parents=True, exist_ok=True)

    if _is_stale(path, stale_minutes):
        try:
            path.unlink()
        except OSError:
            pass

    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockBusy(f"已有扫描在运行：{path}") from exc

    try:
        os.write(descriptor, f"{os.getpid()} {datetime.now().isoformat()}\n".encode("utf-8"))
        os.close(descriptor)
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _is_stale(path: Path, stale_minutes: int) -> bool:
    if not path.exists():
        return False
    age_seconds = datetime.now().timestamp() - path.stat().st_mtime
    return age_seconds > stale_minutes * 60


@contextmanager
def process_lock(path: Path, timeout: float = 30):
    """OS-owned lock; process death releases it without deleting a live lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if path.stat().st_size == 0:
        handle.write(b"0"); handle.flush()
    started = time.monotonic()
    try:
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    raise LockBusy(f"锁等待超时：{path.name}")
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
