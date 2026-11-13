"""推送群组：一个 PushPlus 群组就是一档订阅。

买家的订阅范围不存数据库，也不逐个配置——他扫哪个群的二维码，就只收哪个群的内容。
所以"只推一个区"和"11 个区全推"在代码里的区别只是：这个群覆盖哪些 region。
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.housing.config import get_housing_settings
from app.housing.models import HousingNotice


BASIC = "basic"
PRO = "pro"


@dataclass(frozen=True)
class SubscribeGroup:
    name: str
    topic: str
    regions: tuple[str, ...] = ()
    tier: str = BASIC
    label: str = ""
    id: str = ""
    enabled: bool = True

    @property
    def identity(self) -> str:
        return self.id or "group-" + hashlib.sha256(self.name.encode()).hexdigest()[:16]

    @property
    def display(self) -> str:
        """推送给买家看的名字；name 只用来做唯一标识和投递 key。"""
        return self.label or self.name

    @property
    def active(self) -> bool:
        """没填群组编码的先跳过，方便一个区一个区地开。"""
        return self.enabled and bool(self.topic.strip())

    @property
    def is_pro(self) -> bool:
        """管家版：新通告里带资格粗筛，另外还有截止提醒和批次进度。"""
        return self.tier == PRO

    def covers(self, notice: HousingNotice) -> bool:
        if not self.regions:
            return True
        # 市级通告（level=city）面向全市，所有区群都要发；否则只订一个区的人会漏掉全市批次。
        if (notice.level or "") == "city":
            return True
        return (notice.region_code or "") in self.regions


def load_groups(path: Path | None = None) -> list[SubscribeGroup]:
    path = path or get_housing_settings().groups_path
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("groups", []) if isinstance(payload, dict) else payload
    groups: list[SubscribeGroup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        regions = tuple(str(value).strip() for value in item.get("regions") or [] if str(value).strip())
        tier = str(item.get("tier") or PRO).strip().lower()
        if not isinstance(item.get("enabled", True), bool):
            raise ValueError("enabled 必须是 JSON 布尔值 true/false")
        groups.append(
            SubscribeGroup(
                name=name,
                topic=str(item.get("topic") or "").strip(),
                regions=regions,
                tier=PRO if tier == PRO else BASIC,
                label=str(item.get("label") or "").strip(),
                id=str(item.get("id") or "").strip(),
                enabled=item.get("enabled", True) is True,
            )
        )
    ids = [group.identity for group in groups]
    topics = [group.topic for group in groups if group.topic]
    if len(ids) != len(set(ids)):
        raise ValueError("群 id 必须唯一")
    if len(topics) != len(set(topics)):
        raise ValueError("同一个 topic 不能配置多次")
    if any(len(value) > 64 for value in ids):
        raise ValueError("群 id 最长 64 个字符")
    return groups


def sync_groups(session, now=None):
    """Persist activation boundaries. Removed/disabled groups do not receive new jobs."""
    from sqlalchemy import select
    from app.housing.models import HousingGroupState, utcnow

    now = now or utcnow()
    configured = load_groups()
    existing = {row.group_id: row for row in session.scalars(select(HousingGroupState))}
    for group in configured:
        row = existing.get(group.identity)
        enabled = group.enabled
        if row is None:
            row = HousingGroupState(group_id=group.identity, activated_at=now, eligible_since=now)
            session.add(row)
        elif not row.enabled and enabled:
            row.eligible_since = now
        row.name, row.topic, row.enabled = group.name, group.topic, enabled
        row.updated_at = now
    ids = {group.identity for group in configured}
    for key, row in existing.items():
        if key not in ids:
            row.enabled = False
    session.flush()
    return configured
