from __future__ import annotations

from datetime import time

from app.housing.scheduled_reports import send_report


async def send_digest(*, dry_run=False, if_due=False, force=False, now=None):
    return await send_report("digest", dry_run=dry_run, if_due=if_due, force=force, now=now)


async def send_evening(*, dry_run=False, if_due=False, force=False, now=None):
    return await send_report("evening", dry_run=dry_run, if_due=if_due, force=force, now=now)


def digest_key(now, scope=None):
    day = f"{now:%Y-%m-%d}"
    return f"digest:{scope}:{day}" if scope else f"digest:{day}"


def _is_due(now, hour, minute):
    return time(hour, minute) <= now.time() <= time(12, 0)
