"""Validated local builds and explicit manual-publication receipts."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.housing.models import HousingAttachmentState, HousingBatchUnit, HousingLotteryStat, HousingMeta, HousingNotice, utcnow
from app.housing.batches import batch_key
from app.housing.lock import process_lock


def source_version(session, notice, *, include_related=True, include_review_status=True):
    key = batch_key(notice)
    units = [(u.category_key, u.units, u.verified) for u in session.scalars(
        select(HousingBatchUnit).where(HousingBatchUnit.batch_key == key).order_by(HousingBatchUnit.category_key))] if key else []
    stats = [(s.category_key, s.total_rows, s.complete, s.error) for s in session.scalars(
        select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id).order_by(HousingLotteryStat.category_key))]
    states = [(s.purpose, s.parser_version, s.content_hashes, s.status) for s in session.scalars(
        select(HousingAttachmentState).where(HousingAttachmentState.notice_id == notice.id).order_by(HousingAttachmentState.purpose))]
    parts = [notice.business_version, key, units, stats, states]
    # Qualification and final-selection attachments are dependencies of this
    # lottery lookup even when the original lottery document has not changed.
    if key and include_related:
        from app.housing.models import HousingMeta
        related=[]
        for process in session.scalars(select(HousingNotice).where(
                HousingNotice.batch_key_v2==key,HousingNotice.category=='process').order_by(HousingNotice.id)):
            review=session.get(HousingMeta,f'qualification:{process.id}')
            review_state=session.scalar(select(HousingAttachmentState).where(
                HousingAttachmentState.notice_id==process.id,HousingAttachmentState.purpose=='qualification'))
            entry=(process.id,process.business_version,process.published_unverified,
                   review.value.get('version') if review else None)
            related.append(entry+(review_state.status if review_state else None,) if include_review_status else entry)
            selection=session.get(HousingMeta,f'selection:{process.id}')
            if selection:related.append(('selection',process.id,selection.value.get('version')))
        if related: parts.append(related)
    from app.housing.inventory import batch_inventory
    inventory = batch_inventory(session, key)
    if inventory: parts.append(inventory["version"])
    return hashlib.sha256(json.dumps(parts, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def validate_payload(payload):
    if not payload.get("batches") or not payload.get("rows"):
        raise ValueError("没有可查询的数据，保留上一份页面")
    for matches in payload["rows"].values():
        for row in matches:
            if len(row) != 7 or row[0] >= len(payload["batches"]):
                raise ValueError("查询数据结构异常")
            batch = payload["batches"][row[0]]
            if row[1] >= len(batch["c"]) or row[2] >= len(batch["Q"]) or (not batch.get('review_only') and (row[4] is None or row[4] < 1)):
                raise ValueError("排位索引异常")
            if batch.get('review_only') and row[4] is not None:raise ValueError('核查专用记录不能填造队内排位')
    return True


def build_directory(directory=None):
    """Signed deployments keep full build sources outside the public directory."""
    from app.housing.config import get_housing_settings
    root = get_settings().project_root
    path = Path(directory or root / 'lookup')
    if get_housing_settings().lookup_access_mode == 'signed' and path.resolve() == (root / 'lookup').resolve():
        return root / 'data' / 'lookup-source'
    return path


def read_manifest(directory=None):
    directory = build_directory(directory)
    path = Path(directory) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _lookup_lock_path(directory):
    root = get_settings().project_root
    if Path(directory).resolve() == (root / 'data' / 'lookup-source').resolve():
        return root / 'data' / 'housing.lookup.lock'
    return Path(directory).parent / 'data' / 'housing.lookup.lock'


async def build_regions(regions, *, directory=None, days=900, max_batches=6, filenames=None):
    from tools.build_lottery_lookup import _build, _render_html, _district_slug, _build_index
    directory = build_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with process_lock(_lookup_lock_path(directory)):
        old = read_manifest(directory)
        pages = dict(old.get("pages", {}))
        with tempfile.TemporaryDirectory(prefix="housing-lookup-") as temporary:
            staging = Path(temporary)
            for path in directory.glob("*.html"):
                if path.name in pages:
                    shutil.copy2(path, staging / path.name)
            for region in sorted(set(regions)):
                payload = await _build(region, days, max_batches)
                validate_payload(payload)
                filename = (filenames or {}).get(region, _district_slug(region) + ".html")
                if Path(filename).name != filename or not filename.endswith(".html") or filename == "index.html":
                    raise ValueError("区域输出必须为独立 HTML 文件名，不能覆盖入口页")
                page = staging / filename
                page.write_text(_render_html(payload), encoding="utf-8")
                pages[filename] = {"district": region, "sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
                    "built_at": utcnow().isoformat(), "records": sum(map(len, payload["rows"].values())),
                    "notices": {str(b["id"]): b["version"] for b in payload["batches"]},
                    "issues": [c["issue"] for b in payload["batches"] for c in b["c"] if c.get("issue")]}
            index = _build_index(staging)
            if not index.get("pages"):
                raise ValueError("入口页没有可用区域")
            pages["index.html"] = {"sha256": hashlib.sha256((staging / "index.html").read_bytes()).hexdigest()}
            manifest = {"built_at": utcnow().isoformat(), "pages": pages}
            manifest["version"] = hashlib.sha256(json.dumps(pages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            # Validate every candidate before replacing any live page.
            for name, info in pages.items():
                if not (staging / name).exists() or hashlib.sha256((staging / name).read_bytes()).hexdigest() != info["sha256"]:
                    raise ValueError("页面与清单不一致")
            for path in staging.glob("*.html"):
                destination = directory / path.name
                temporary_page = destination.with_suffix(".tmp")
                shutil.copyfile(path, temporary_page)
                temporary_page.replace(destination)
            temp_manifest = directory / "manifest.tmp"
            temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_manifest.replace(directory / "manifest.json")
        return {"status": "built", "version": manifest["version"], "regions": list(regions),
                "publication": "待人工上传；上传后执行 lookup-publish --version 此版本号"}


def mark_published(session, version, directory=None):
    from app.housing.config import get_housing_settings
    if get_housing_settings().lookup_access_mode == 'signed':
        raise ValueError('签名查询版本必须使用 secure_lookup activate，先校验云端接口及所有页面')
    manifest = read_manifest(directory)
    directory = Path(directory or get_settings().project_root / "lookup")
    if not manifest or version != manifest.get("version"):
        raise ValueError("必须提供当前 manifest.json 的完整版本号")
    notices = {}
    for name, info in manifest["pages"].items():
        path = directory / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("本地文件已变化，不能登记发布")
        for notice_id, expected in info.get("notices", {}).items():
            notice = session.get(HousingNotice, int(notice_id))
            if not notice or source_version(session, notice) != expected:
                raise ValueError("源数据已更新，请重新构建并上传")
            notices[notice_id] = expected
    record = session.get(HousingMeta, "lookup-published")
    if record is None:
        record = HousingMeta(key="lookup-published"); session.add(record)
    record.value = {"version": version, "published_at": utcnow().isoformat(), "notices": notices}
    session.flush()
    return {"status": "published_recorded", "version": version, "notices": len(notices)}


def published_ready(notice=None, session=None):
    """渲染时调用方手里已经有 session；再开一个连接既慢，也让测试读到另一个库。"""
    if session is not None:
        return _published_ready(session, notice)
    with SessionLocal() as own:
        return _published_ready(own, notice)


def _published_ready(session, notice=None):
    record = session.get(HousingMeta, "lookup-published")
    if not record:
        return False
    from app.housing.config import get_housing_settings
    if get_housing_settings().lookup_access_mode == 'signed' and record.value.get('schema') != 'signed-lookup-v1':
        return False
    if notice is None:
        return bool(record.value.get("notices"))
    candidates = [notice]
    if record.value.get('notices',{}).get(str(notice.id))==source_version(session,notice):return True
    if notice.category != "lottery_result":
        key = batch_key(notice)
        if not key:
            return False
        candidates = list(session.scalars(select(HousingNotice).where(
            HousingNotice.batch_key_v2 == key, HousingNotice.category == "lottery_result")))
    for candidate in candidates:
        if record.value.get("notices", {}).get(str(candidate.id)) == source_version(session, candidate):
            return True
    return False


async def build_changed():
    old = read_manifest()
    from app.housing.lookup_data import selected_events
    built_by_region = {info.get("district"): {k:v for k,v in info.get("notices", {}).items() if k in info.get('lottery_notice_ids',info.get('notices',{}))}
                       for info in old.get("pages", {}).values() if info.get("district")}
    regions = set();selected_ids_by_region={}
    with SessionLocal() as session:
        from app.housing.reporting import build_snapshot
        snapshot=build_snapshot(session,'evening',now=utcnow())
        published_dependencies={k:v for info in old.get('pages',{}).values() for k,v in info.get('notices',{}).items()}
        for card in snapshot.cards:
            if not card.qualification or card.qualification.get('pending'):continue
            key=session.get(HousingNotice,card.batch_id).batch_key_v2 if card.batch_id else None
            has_lottery=session.scalar(select(HousingNotice.id).where(HousingNotice.batch_key_v2==key,HousingNotice.category=='lottery_result')) if key else None
            if not has_lottery:
                node=session.get(HousingNotice,card.qualification['notice_id'])
                if published_dependencies.get(str(node.id))!=source_version(session,node):return refresh_review_metadata()
        candidates = set(session.scalars(select(HousingNotice.region_code).where(
            HousingNotice.category == "lottery_result", HousingNotice.is_target.is_(True))))
        for region in candidates:
            selected = selected_events(session, region)
            current = {str(group[0].id): source_version(session, group[0]) for group in selected}
            ready = any(session.scalar(select(HousingAttachmentState.id).where(
                HousingAttachmentState.notice_id == group[0].id,
                HousingAttachmentState.purpose == "lottery",
                HousingAttachmentState.status.in_({"ok", "partial"}))) for group in selected)
            if ready and current != built_by_region.get(region, {}):
                regions.add(region)
                selected_ids_by_region[region]=set(current)
    if not regions:return {"status":"unchanged"}
    if all(selected_ids_by_region[r]==set(built_by_region.get(r,{})) for r in regions):
        try:
            return refresh_review_metadata()
        except ValueError:
            pass  # New/changed raw rosters still require the full build below.
    results,failures=[],[]
    for region in sorted(regions):
        try:results.append(await build_regions([region]))
        except Exception:
            __import__('logging').getLogger(__name__).exception('lookup region build failed; previous page preserved',extra={'region':region})
            failures.append(region)
    if failures:return {'status':'degraded','updated_regions':[r for item in results for r in item.get('regions',[])],'failed_regions':failures,'reason':'部分历史名单未完整核实，保留原记录及官方入口'}
    return results[0] if len(results)==1 else {'status':'built','regions':sorted(regions),'versions':[item.get('version') for item in results]}


def refresh_competition(directory=None):
    """Re-render intact, current local rosters without changing delivery history."""
    from app.housing.competition import attach_competition
    from app.housing.lookup_data import attach_inventory, merge_mirror_payload
    from app.housing.inventory import batch_inventory
    from app.housing.opportunity import load_payload
    from tools.build_lottery_lookup import _render_html
    directory = build_directory(directory)
    with process_lock(_lookup_lock_path(directory)), SessionLocal() as session:
        manifest = read_manifest(directory)
        if not manifest:
            raise ValueError("没有已校验的查询页清单")
        with tempfile.TemporaryDirectory(prefix="housing-competition-") as temporary:
            staging = Path(temporary)
            for name, info in manifest["pages"].items():
                path = directory / name
                if Path(name).name != name or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                    raise ValueError("本地文件与清单不一致，保留原版本")
                if name == "index.html":
                    shutil.copy2(path, staging / name)
                    continue
                payload = load_payload(path)
                validate_payload(payload)
                for batch in payload["batches"]:
                    notice = session.get(HousingNotice, batch["id"])
                    if not notice or batch.get("stale") or batch.get("version") != source_version(session, notice):
                        raise ValueError(f"公告{batch['id']}的本地名单版本已变化，请先完整重建")
                    attach_inventory(batch, batch_inventory(session, batch_key(notice)))
                merge_mirror_payload(session, payload)
                attach_competition(session, payload)
                from app.housing.qualification import attach_reviews
                attach_reviews(session,payload)
                page = staging / name
                page.write_text(_render_html(payload), encoding="utf-8")
                info.update(sha256=hashlib.sha256(page.read_bytes()).hexdigest(), built_at=utcnow().isoformat(),
                            competition_schema="lottery-competition-v1", records=sum(map(len, payload['rows'].values())),
                            notices={str(b['id']): b['version'] for b in payload['batches']})
            manifest["built_at"] = utcnow().isoformat()
            manifest["version"] = hashlib.sha256(json.dumps(manifest["pages"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for path in staging.glob("*.html"):
                candidate = (directory / path.name).with_suffix(".tmp")
                shutil.copyfile(path, candidate)
                candidate.replace(directory / path.name)
            candidate = directory / "manifest.tmp"
            candidate.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            candidate.replace(directory / "manifest.json")
        return {"status": "built", "version": manifest["version"], "publication": "待人工上传并核对"}


def refresh_review_metadata(directory=None,*,final_updates=None):
    """Reuse a proven unchanged lottery roster and attach new private reviews."""
    from app.housing.competition import categories_from_payload,saved_categories,attach_competition
    from app.housing.qualification import attach_reviews
    from app.housing.opportunity import load_payload
    from tools.build_lottery_lookup import _render_html
    directory=build_directory(directory)
    with process_lock(_lookup_lock_path(directory)),SessionLocal() as session:
        manifest=read_manifest(directory)
        if not manifest:raise ValueError('缺少完整本地名单构建')
        staged={};payloads={}
        for name,info in manifest['pages'].items():
            path=directory/name
            if Path(name).name!=name or hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:
                raise ValueError('原名单文件与清单不一致')
            if name=='index.html':continue
            payload=load_payload(path);validate_payload(payload)
            for i,batch in enumerate(payload['batches']):
                notice=session.get(HousingNotice,batch['id'])
                if batch.get('review_only'):continue
                saved={v['category']:v.get('roster_hash') for v in saved_categories(session,notice) or []}
                observed={v['category']:v.get('roster_hash') for v in categories_from_payload(payload,i)}
                base_version=source_version(session,notice,include_related=False)
                unchanged_source=(batch.get('lottery_base_version')==base_version or batch.get('version') in {
                    source_version(session,notice),source_version(session,notice,include_review_status=False),base_version})
                if not unchanged_source and (saved!=observed or not all(saved.values())):
                    raise ValueError(f'公告{notice.id}原名单成员或顺序已变化，必须完整重建')
                batch['version']=source_version(session,notice)
                batch['lottery_base_version']=base_version
                if not final_updates or batch.get('batch_key') not in final_updates:
                    from app.housing.models import HousingMeta
                    nodes=list(session.scalars(select(HousingNotice).where(HousingNotice.batch_key_v2==batch.get('batch_key'),HousingNotice.category=='process').order_by(HousingNotice.published_at.desc())))
                    for node in nodes:
                        cache=session.get(HousingMeta,f'selection:{node.id}')
                        if cache and cache.value.get('verified'):
                            from app.housing.qualification import cache_root
                            file=cache.value['file']
                            if Path(file).name!=file:raise ValueError('非法正式排序缓存路径')
                            saved=json.loads((cache_root()/file).read_text(encoding='utf-8'))
                            batch['queue_updates']=saved.get('queue_updates',{})
                            selected_state=session.scalar(select(HousingAttachmentState).where(HousingAttachmentState.notice_id==node.id,HousingAttachmentState.purpose=='selection'))
                            if selected_state and selected_state.status!='ok':
                                saved['notice'].update(pending=True,verified=False,issue='最新正式排序附件尚未完整核实，上一份资料保留，请查看官方原文。')
                                for row in saved['rows'].values():row['scope_verified']=False
                            final_updates={**(final_updates or {}),batch['batch_key']:(saved['rows'],saved['notice'])}
                            break
                if final_updates and batch.get('batch_key') in final_updates:
                    batch['final_rows'],batch['final_notice']=final_updates[batch['batch_key']]
                    from app.housing.daily_lifecycle import action_windows
                    final_notice=session.get(HousingNotice,next(iter(batch['final_rows'].values()))['notice_id']) if batch['final_rows'] else None
                    if final_notice:
                        batch['selection_handling']=action_windows(final_notice)
                        batch['selection_info']={'url':final_notice.canonical_url,'published_at':(final_notice.published_at or final_notice.first_seen_at).isoformat()}
            attach_competition(session,payload);attach_reviews(session,payload)
            payloads[name]=payload
        from app.housing.review_only import payloads as add_reviews
        add_reviews(session,payloads)
        for name,payload in payloads.items():
            validate_payload(payload)
            info=manifest['pages'].setdefault(name,{'district':payload['district']})
            staged[name]=_render_html(payload).encode('utf-8')
            info.update(sha256=hashlib.sha256(staged[name]).hexdigest(),built_at=utcnow().isoformat(),
                        notices={str(b['id']):b['version'] for b in payload['batches']},records=sum(map(len,payload['rows'].values())),
                        lottery_notice_ids=[str(b['id']) for b in payload['batches'] if not b.get('review_only')])
        # Rebuild the private entry directory when a verified review adds a region.
        from tools.build_lottery_lookup import _build_index
        with tempfile.TemporaryDirectory(prefix='housing-review-index-') as tmp:
            staging=Path(tmp)
            for name,content in staged.items():(staging/name).write_bytes(content)
            _build_index(staging)
            staged['index.html']=(staging/'index.html').read_bytes()
            manifest['pages']['index.html']={'sha256':hashlib.sha256(staged['index.html']).hexdigest()}
        manifest['built_at']=utcnow().isoformat()
        manifest['version']=hashlib.sha256(json.dumps(manifest['pages'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        for name,content in staged.items():
            temp=(directory/name).with_suffix('.tmp');temp.write_bytes(content);temp.replace(directory/name)
        temp=directory/'manifest.tmp';temp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(directory/'manifest.json')
        return {'status':'built','regions':len(payloads),'version':manifest['version'],'publication':'待云端发布及核对'}


def refresh_cached_rendering(directory=None):
    """Upgrade only rendering/verified unit metadata of an intact local build.

    Does not claim to refresh downloaded lottery rows. Rows must still agree with
    the persisted aggregates; any non-unique match loses its estimate.
    """
    from tools.build_lottery_lookup import _render_html, _category_label
    from app.housing.lottery import queue_sort_key
    directory = build_directory(directory)
    with process_lock(_lookup_lock_path(directory)), SessionLocal() as session:
        manifest = read_manifest(directory)
        if not manifest:
            raise ValueError("没有可升级的已校验构建")
        with tempfile.TemporaryDirectory(prefix="housing-render-") as temporary:
            staging = Path(temporary)
            for name, info in manifest["pages"].items():
                path = directory / name
                if hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                    raise ValueError("本地页面已变化，不能直接升级元数据")
                if name == "index.html":
                    continue
                payload = json.loads(re.search(r"const DATA = (.*?);\s*const \$", path.read_text(encoding="utf-8"), re.S)[1])
                validate_payload(payload)
                for batch_index, batch in enumerate(payload["batches"]):
                    notice = session.get(HousingNotice, batch["id"])
                    stats = list(session.scalars(select(HousingLotteryStat).where(HousingLotteryStat.notice_id == notice.id)))
                    for category in batch["c"]:
                        matches = [s for s in stats if s.total_rows == category["n"] and s.queue_counts == category["q"]]
                        category["u"] = 0
                        if len(matches) != 1:
                            category["issue"] = "聚合档位尚未唯一核实，暂停房源估算"
                            continue
                        stat = matches[0]
                        category["l"] = _category_label(stat.label, stat.category_key)
                        category["issue"] = stat.error or ""
                        if notice.batch_key_v2 and stat.complete:
                            unit = session.scalar(select(HousingBatchUnit).where(
                                HousingBatchUnit.batch_key == notice.batch_key_v2,
                                HousingBatchUnit.category_key == stat.category_key,
                                HousingBatchUnit.verified.is_(True)))
                            category["u"] = unit.units if unit else 0
                    old_queues = batch["Q"]
                    batch["Q"] = sorted(old_queues, key=queue_sort_key)
                    remap = {i: batch["Q"].index(q) for i, q in enumerate(old_queues)}
                    batch["R"] = {f"{key.split('|')[0]}|{remap[int(key.split('|')[1])]}": values
                                  for key, values in batch["R"].items()}
                    for matches in payload["rows"].values():
                        for match in matches:
                            if match[0] == batch_index:
                                match[2], match[5] = remap[match[2]], remap[match[5]]
                    batch["version"] = source_version(session, notice)
                validate_payload(payload)
                page = staging / name
                page.write_text(_render_html(payload), encoding="utf-8")
                info.update(sha256=hashlib.sha256(page.read_bytes()).hexdigest(), built_at=utcnow().isoformat(),
                    notices={str(b["id"]): b["version"] for b in payload["batches"]},
                    issues=[c["issue"] for b in payload["batches"] for c in b["c"] if c.get("issue")])
            manifest["built_at"] = utcnow().isoformat()
            manifest["version"] = hashlib.sha256(json.dumps(manifest["pages"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for path in staging.glob("*.html"):
                target = directory / path.name
                candidate = target.with_suffix(".tmp")
                shutil.copyfile(path, candidate); candidate.replace(target)
            candidate = directory / "manifest.tmp"
            candidate.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            candidate.replace(directory / "manifest.json")
        return {"version": manifest["version"], "status": "rendered_metadata_only"}
