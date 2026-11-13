"""City-wide deltas against the last accepted FORMAL report, never test sends."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select

from app.housing.models import HousingMeta, HousingOutbox, HousingOutboxEvent, HousingGroupState


def accepted_baseline(session, now):
    jobs=list(session.scalars(select(HousingOutbox).join(HousingGroupState,
        HousingGroupState.group_id==HousingOutbox.group_id).where(
        HousingOutbox.kind.in_({'digest','evening'}),HousingOutbox.status.in_({'submitted','confirmed'}),
        HousingGroupState.enabled.is_(True),HousingOutbox.created_at<now).order_by(HousingOutbox.created_at.desc())))
    candidates=[]
    for job in jobs:
        keys=session.scalars(select(HousingOutboxEvent.event_key).where(
            HousingOutboxEvent.outbox_id==job.id,HousingOutboxEvent.kind==job.kind))
        for key in keys:
            if not re.fullmatch(r'(?:digest|evening):\d{4}-\d{2}-\d{2}',key):continue
            record=session.get(HousingMeta,'report:'+key)
            if not record or not record.value.get('generated_at'):continue
            report=record.value
            stamp=datetime.fromisoformat(report['generated_at']).replace(tzinfo=None)
            if stamp<now:candidates.append(dict(report,key=key))
    return max(candidates,key=lambda r:r['generated_at']) if candidates else None


def _clean(value):
    if isinstance(value,str):return re.sub(r'\s+','',value)
    if isinstance(value,list):return [_clean(v) for v in value]
    if isinstance(value,dict):return {k:_clean(v) for k,v in value.items()}
    return value


def semantic(card):
    """Exclude rendering, parser timestamps, URL aliases and transport metadata."""
    if not isinstance(card,dict):card=json.loads(json.dumps(asdict(card),default=lambda v:v.isoformat()))
    inv=card.get('inventory') or {}
    projects=[{k:p.get(k) for k in ('name','address','rows','fees')} for p in inv.get('projects',[])]
    lottery=[{k:i.get(k) for k in ('category','households','queues','initial_units')}
             for i in card.get('lottery_breakdown',[]) if i.get('households_verified')]
    reliable=[i for i in card.get('lottery_breakdown',[]) if i.get('households_verified')]
    roster={i['category']:i['roster_hash'] for i in reliable} if reliable and all(i.get('roster_hash') for i in reliable) else None
    return _clean({'start':card.get('start'),'end':card.get('end'),'units':card.get('units'),
        'objects':card.get('profile',{}).get('object_text',[]),'qualifications':card.get('profile',{}).get('qualifications',[]),
        'projects':projects if inv.get('complete') else None,'rules':inv.get('evidence',[]),'lottery':lottery or None,'roster':roster,
        'qualification':(card.get('qualification') or {}).get('version') or None,
        'handling':[{k:w.get(k) for k in ('kind','start','end','scope','office_sessions','working_days_only')} for w in card.get('action_windows',[])]})


def apply_delta(snapshot,baseline,revisions):
    previous=(baseline or {}).get('audit',{}).get('cards',[])
    by_code={c['code']:c for c in previous}
    seen={i for c in previous for i in c.get('notice_ids',[])}
    for card in [*snapshot.cards,*snapshot.history_events]:
        old=by_code.get(card.code)
        if old is None:
            matches=[c for c in previous if set(c.get('notice_ids',[])) & set(card.notice_ids)]
            if len(matches)==1:old=matches[0]
        before=semantic(old) if old else None
        after=semantic(card)
        changes=[]
        if before:
            for key,label in (('end','认租截止调整'),('start','认租开始时间调整'),('units','配租套数调整'),
                              ('projects','房源或费用调整'),('objects','配租对象调整'),
                              ('qualifications','资格条件调整'),('rules','办理规则调整'),('lottery','名单统计调整'),('roster','名单成员或顺序调整'),('qualification','审核名单调整'),('handling','办理期限或范围调整')):
                if before.get(key) is not None and after.get(key) is not None and before.get(key)!=after.get(key):changes.append(label)
        for ref in card.references:
            in_window=bool(ref.published_at and snapshot.window_start<ref.published_at<=snapshot.generated_at)
            # Date-only publication metadata is represented as midnight. A new
            # same-day notice first collected after the last report must not be
            # lost merely because midnight precedes that report's 08:30 cutoff.
            newly_observed_today=bool(ref.published_at and ref.published_at.time()==datetime.min.time()
                and ref.published_at.date()==snapshot.generated_at.date() and ref.first_seen_at
                and snapshot.window_start<ref.first_seen_at<=snapshot.generated_at)
            fresh=(in_window or newly_observed_today) and ref.id not in seen
            original_update=ref.is_update
            ref.is_new=fresh
            ref.is_update=bool(not fresh and old and changes and original_update)
        if card.changed:
            card.delta_reasons=changes if any(r.is_update for r in card.references) else ['新增'+{
                'applying':'认租公告','lottery':'摇号结果','selection':'选房安排','review':'核查公告','signing':'签约安排'
                }.get(next((r.stage for r in card.references if r.is_new),None),'公告')]
    # Archived history is not reactivated by parsing old attachments. A newly
    # published result/termination may be mentioned once, outside current totals.
    snapshot.history_events=[c for c in snapshot.history_events if any(r.is_new for r in c.references)]


def validate_totals(snapshot):
    total=snapshot.totals
    assert sum(s.open_batches for s in snapshot.housing_totals.values())==total.open_batches
    assert sum(s.in_transit_batches for s in snapshot.housing_totals.values())==total.in_transit_batches
    for summary in [total,*snapshot.categories.values(),*snapshot.housing_totals.values()]:
        assert summary.personal_batches+summary.enterprise_batches+summary.unknown_batches==summary.in_transit_batches
        assert summary.open_batches<=summary.in_transit_batches
    return True
