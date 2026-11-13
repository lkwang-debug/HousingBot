from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime

import typer
from sqlalchemy import select, or_

from app.db import SessionLocal
from app.housing.config import get_housing_settings
from app.housing.delivery import push_pending
from app.housing.digest import send_digest, send_evening
from app.housing.lock import scan_lock
from app.housing.maintenance import doctor as doctor_report
from app.housing.maintenance import dedupe, initialize, purge, reclassify, verify_sources
from app.housing.models import HousingNotice, utcnow
from app.housing.scan import run_scan
from app.housing.store import notice_to_dict

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False, help="深圳保租房/公租房通告监控")


@app.callback()
def cli_start(ctx: typer.Context):
    from app.housing.operations import configure_logging
    configure_logging()
    if ctx.invoked_subcommand in {"scan", "digest", "evening", "remind", "push", "backfill", "report-test"} and (
        "--deliver" in sys.argv or ctx.invoked_subcommand == "push"
    ):
        from app.housing.operations import backup_database
        backup_database()


def _delivery_exit(payload):
    if payload.get("status") in {"failed", "blocked", "degraded", "needs_review"}:
        raise typer.Exit(code=2)


def _echo(payload) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _in_quiet_window(now: datetime) -> bool:
    current = now.time()
    for start, end in get_housing_settings().quiet_ranges:
        if start <= end and start <= current <= end:
            return True
        if start > end and (current >= start or current <= end):
            return True
    return False


@app.command("init")
def init_command() -> None:
    """建表、同步源注册表、写入默认关注规则。"""
    _echo(initialize())


@app.command("sources")
def sources_command(
    verify: bool = typer.Option(False, help="逐源实测，输出可用性"),
    source: list[str] = typer.Option(None, help="只检查指定源 key"),
) -> None:
    if not verify:
        from app.housing.sources import load_source_defs

        _echo(
            [
                {"key": item.key, "name": item.name, "adapter": item.adapter, "enabled": item.enabled}
                for item in load_source_defs()
            ]
        )
        return
    results = asyncio.run(verify_sources(only=source or None))
    _echo(results)
    if any(item["status"] != "ok" for item in results):
        raise typer.Exit(code=2)


def with_detail_available(no_detail):
    return not no_detail


@app.command("scan")
def scan_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="不写库、不发送"),
    source: list[str] = typer.Option(None, "--source", help="只扫描指定源 key"),
    pages: int = typer.Option(None, help="每个源最多翻页数"),
    no_detail: bool = typer.Option(False, "--no-detail", help="只抓列表，不进详情页"),
    deliver: bool = typer.Option(False, help="扫描后立即推送待发通告"),
    force: bool = typer.Option(False, help="忽略配置的推送静默窗口"),
) -> None:
    settings = get_housing_settings()
    with scan_lock():
        if not dry_run:
            from app.housing.operations import backup_database
            backup_database()
        result = asyncio.run(
            run_scan(
                dry_run=dry_run,
                only_sources=source or None,
                max_pages=pages,
                with_detail=not no_detail,
            )
        )
    payload = {
        "run_id": result.run_id,
        "status": result.status,
        "sources_total": result.sources_total,
        "sources_ok": result.sources_ok,
        "sources_failed": result.sources_failed,
        "items_seen": result.items_seen,
        "items_new": result.items_new,
        "items_updated": result.items_updated,
        "pending_push": len(result.new_pushable_ids),
        "degraded": result.degraded,
    }
    if deliver and not dry_run:
        if result.new_pushable_ids and not force and _in_quiet_window(utcnow()):
            payload["delivery"] = {"status": "deferred", "reason": "处于配置的推送静默窗口"}
        else:
            with SessionLocal() as session:
                payload["delivery"] = asyncio.run(
                    push_pending(session, settings.instant_max_items)
                )
                session.commit()
    if not dry_run and with_detail_available(no_detail):
        try:
            from app.housing.lookup import build_changed
            payload["lookup"] = asyncio.run(build_changed())
        except Exception:
            logging.getLogger(__name__).exception("lookup build failed; previous files preserved")
            payload["lookup"] = {"status": "degraded", "reason": "查询页构建失败，详见日志"}
    if deliver and not dry_run:
        payload["digest_compensation"] = asyncio.run(send_digest(if_due=True))
        payload["evening_compensation"] = asyncio.run(send_evening(if_due=True))
    _echo(payload)
    if payload.get("delivery", {}).get("status") == "degraded" or payload.get("lookup", {}).get("status") == "degraded" or payload.get("digest_compensation", {}).get("status") == "degraded" or payload.get("evening_compensation", {}).get("status") == "degraded":
        raise typer.Exit(code=2)
    if result.status == "degraded":
        raise typer.Exit(code=2)


@app.command("digest")
def digest_command(
    dry_run: bool = typer.Option(False, "--dry-run"),
    deliver: bool = typer.Option(False, help="实际发送到微信"),
    if_due: bool = typer.Option(False, "--if-due", help="仅早报时间窗内发送"),
    force: bool = typer.Option(False, "--force", help="独立测试发送，不占正式早报日键"),
) -> None:
    result = asyncio.run(send_digest(dry_run=dry_run or not deliver, if_due=if_due, force=force))
    if dry_run or deliver:
        _echo(result)
    else:
        typer.echo(result["content"])
    _delivery_exit(result)


@app.command("evening")
def evening_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览分类晚报及统计核对清单"),
    deliver: bool = typer.Option(False, help="实际发送固定晚报，20:30后生效"),
    if_due: bool = typer.Option(False, "--if-due"),
    force: bool = typer.Option(False, "--force", help="独立测试发送，不占正式晚报日键"),
):
    result = asyncio.run(send_evening(dry_run=dry_run or not deliver, if_due=if_due, force=force))
    if dry_run or deliver:
        _echo(result)
    else:
        typer.echo(result["content"])
    _delivery_exit(result)


@app.command("group-digest")
def group_digest_command(
    evening: bool = typer.Option(False, "--evening", help="生成晚报短版"),
    limit: int = typer.Option(1000, "--limit", help="字符上限，默认1000"),
    full: bool = typer.Option(False, "--full", help="全量版：完整报表拆成多条，逐条发送"),
    keep_links: bool = typer.Option(False, "--keep-links", help="全量版保留原文链接（很占字数）"),
) -> None:
    """输出可直接粘贴到群聊的纯文本摘要。

    默认只给短版（认租中批次＋本期节点）；--full 输出完整报表，按 limit 拆成多条。
    """
    from app.housing.render import render_group_digest, render_group_full

    with SessionLocal() as session:
        if not full:
            typer.echo(render_group_digest(session, evening=evening, max_chars=limit))
            return
        chunks = render_group_full(session, evening=evening, max_chars=limit, keep_links=keep_links)
        for i, chunk in enumerate(chunks, 1):
            typer.echo(f"======== 第 {i}/{len(chunks)} 条 ========")
            typer.echo(chunk)
            typer.echo("")


@app.command("groups")
def groups_command() -> None:
    """查看群组订阅配置（topic 留空的群不会推送）。"""
    from app.housing.groups import load_groups

    _echo(
        [
            {
                "id": group.identity,
                "enabled": group.enabled,
                "name": group.name,
                "tier": group.tier,
                "topic": group.topic or None,
                "regions": list(group.regions) or ["全部"],
                "active": group.active,
            }
            for group in load_groups()
        ]
    )


@app.command("share-pack")
def share_pack_command(
    kind: str = typer.Option("digest", help="digest早报、evening晚报；默认早报"),
    date: str = typer.Option(None, "--date", help="指定已发报表日期，默认最近已完整受理的一期"),
    output: str = typer.Option(None, "--output", help="分享包输出目录"),
):
    """只读导出已发报表为分页图片和≤900字文字，供手动发送。"""
    from app.housing.share_pack import export_share_pack, read_frozen_report
    _echo(export_share_pack(read_frozen_report(kind, date), output))


@app.command("report-test")
def report_test_command(
    kind: str = typer.Option(..., help="instant、digest 或 evening"),
    deliver: bool = typer.Option(False, help="向已启用总群发送真实数据验收样例；不占正式日键"),
):
    from app.housing.scheduled_reports import send_test_report
    if kind not in {"instant", "digest", "evening"}:
        raise typer.BadParameter("kind 必须是 instant、digest 或 evening")
    result = asyncio.run(send_test_report(kind, deliver=deliver))
    _echo(result)
    _delivery_exit(result)


@app.command("remind")
def remind_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="只列出待发提醒，不发送"),
    deliver: bool = typer.Option(False, help="实际发送到群"),
) -> None:
    """管家版：认租截止前 3 天/1 天提醒 + 批次进度推进。"""
    from app.housing.remind import send_reminders

    result = asyncio.run(send_reminders(dry_run=dry_run, deliver=deliver))
    _echo(result)
    _delivery_exit(result)


@app.command("batches")
def batches_command(
    days: int = typer.Option(120, help="回看天数"),
    region: str = typer.Option(None, help="只看某区，如 光明"),
) -> None:
    """批次全流程：认租→核查→摇号→选房→签约，看每批走到哪一步、缺哪一步。"""
    from app.housing.batches import build_batches, detect_stage

    with SessionLocal() as session:
        payload = []
        for batch in build_batches(session, since_days=days):
            if region and region not in (batch.region or ""):
                continue
            steps = []
            for notice in sorted(
                batch.notices,
                key=lambda item: item.published_at or item.first_seen_at or utcnow(),
            ):
                stage = detect_stage(notice)
                if not stage:
                    continue
                steps.append(
                    {
                        "date": (notice.published_at or notice.first_seen_at).strftime("%Y-%m-%d")
                        if (notice.published_at or notice.first_seen_at)
                        else "",
                        "stage": stage,
                        "title": notice.title[:60],
                        "url": notice.canonical_url,
                    }
                )
            payload.append(
                {
                    "batch": batch.key,
                    "region": batch.region,
                    "stage": batch.stage,
                    "stage_label": batch.stage_label,
                    "title": batch.title[:70],
                    "steps": steps,
                }
            )
    _echo(payload)


@app.command("lottery")
def lottery_command(
    notice_id: int = typer.Option(None, "--notice-id", help="只处理某条通告"),
    backfill: bool = typer.Option(False, "--backfill", help="给已入库的通告补抓附件统计"),
    days: int = typer.Option(180, help="backfill 回看天数"),
    force: bool = typer.Option(False, help="重新解析已有统计"),
    show: bool = typer.Option(False, "--show", help="打印已入库的摇号统计"),
    push: bool = typer.Option(False, help="解析后立即推送待发通告"),
) -> None:
    """摇号结果：抓附件、算人数、入库（只存聚合，不存名单）。"""
    from app.housing.fetch import HousingHttpClient
    from app.housing.lottery import backfill_lottery, ingest_lottery, ingest_units
    from app.housing.models import HousingLotteryStat

    if show:
        with SessionLocal() as session:
            rows = session.scalars(
                select(HousingLotteryStat).order_by(HousingLotteryStat.parsed_at.desc()).limit(40)
            ).all()
        _echo(
            [
                {
                    "notice_id": row.notice_id,
                    "batch": row.batch_key,
                    "category": row.category_key,
                    "label": row.label,
                    "applicants": row.total_rows,
                    "queues": row.queue_counts,
                    "parsed": row.parsed,
                    "error": row.error,
                }
                for row in rows
            ]
        )
        return

    async def _run() -> dict:
        async with HousingHttpClient() as client:
            with SessionLocal() as session:
                if notice_id is not None:
                    notice = session.get(HousingNotice, notice_id)
                    if notice is None:
                        raise typer.BadParameter(f"通告不存在: {notice_id}")
                    stats = (
                        await ingest_lottery(session, client, notice, force=force)
                        if notice.category == "lottery_result"
                        else await ingest_units(session, client, notice, force=force)
                    )
                    session.commit()
                    return {"notice_id": notice_id, "parsed": len(stats)}
                if backfill:
                    result = await backfill_lottery(
                        session, client, days=days, force=force
                    )
                    session.commit()
                    return result
                return {"status": "nothing to do：加 --notice-id 或 --backfill"}

    payload = asyncio.run(_run())
    if push:
        with SessionLocal() as session:
            payload["delivery"] = asyncio.run(push_pending(session))
            session.commit()
    _echo(payload)


@app.command("predict")
def predict_command(
    apply_no: str = typer.Option(None, "--apply-no", help="认租编号，如 SQ2026BZ360007741"),
    notice_id: int = typer.Option(None, "--notice-id"),
    queue: str = typer.Option(None, help="没有认租编号时按队列演算，如 第三队列"),
    order: int = typer.Option(None, help="从真实名单确认摇号顺序号"),
    rank: int = typer.Option(None, help="明确假设的队内排位，与 --order 互斥"),
    category: str = typer.Option("40", help="户型档：40 / 50 / 90 / 100+"),
) -> None:
    """按认租编号查排位，并估算能不能选到房。"""
    from app.housing.fetch import HousingHttpClient
    from app.housing.forecast import predict_by_apply_no, predict_whatif
    from app.housing.lottery import summary_map

    if apply_no:
        async def _run() -> dict:
            async with HousingHttpClient() as client:
                with SessionLocal() as session:
                    return await predict_by_apply_no(
                        session, client, apply_no, notice_id=notice_id
                    )

        _echo(asyncio.run(_run()))
        return

    with SessionLocal() as session:
        statement = select(HousingNotice).where(HousingNotice.category == "lottery_result")
        if notice_id is not None:
            statement = statement.where(HousingNotice.id == notice_id)
        notice = session.scalars(
            statement.order_by(HousingNotice.published_at.desc()).limit(1)
        ).first()
        if notice is None:
            raise typer.BadParameter("没有可用的摇号结果通告，先跑 lottery --backfill")
        summary = summary_map(session, [notice]).get(notice.id)
        target = next((item for item in summary.categories if item.key == category), None)
        if target is None:
            raise typer.BadParameter(f"该通告没有 {category} 档，可选：" + "、".join(
                item.key for item in summary.categories
            ))
        if not queue or (order is None and rank is None) or (order is not None and rank is not None):
            raise typer.BadParameter("请给 --queue 及 --order 或 --rank 中的一项，或使用 --apply-no")
        if rank is not None:
            result = predict_whatif(target.units, target.queue_counts, queue, rank=rank)
        else:
            from app.housing.forecast import predict_order
            async def _order():
                async with HousingHttpClient() as client:
                    return await predict_order(session, client, notice, category, queue, order)
            result = asyncio.run(_order())
    _echo(
        {
            "notice_id": notice.id,
            "category": target.label,
            "message": "\n".join(result.pop("lines")),
            **result,
        }
    )


@app.command("inventory")
def inventory_command(notice_id: int = typer.Option(...), refresh: bool = False):
    """核对／刷新配租项目明细；不创建或投递消息。"""
    from app.housing.inventory import load_inventory, enrich_inventory
    from app.housing.fetch import HousingHttpClient
    async def run():
        with SessionLocal() as session:
            notice = session.get(HousingNotice, notice_id)
            if not notice or notice.category != "rent_launch": raise typer.BadParameter("请选择原配租启动公告ID")
            if refresh:
                async with HousingHttpClient() as client:
                    await enrich_inventory(session, client, notice, force=True)
                session.commit()
            return load_inventory(session, notice) or {"status": "unverified"}
    _echo(asyncio.run(run()))


@app.command("opportunity")
def opportunity_command(apply_no: str = typer.Option(...), page: str = typer.Option("lookup/guangming.html"),
                        batch: int = typer.Option(None), round_no: int = typer.Option(None, "--round"), projects: str = "", people: int = typer.Option(None),
                        children: int = typer.Option(None), basis: str = "", ahead: int = typer.Option(None),
                        not_consuming: int = typer.Option(None), queue: str = typer.Option(None, "--queue"),
                        simple: bool = typer.Option(False, "--simple", help="只填项目和家庭信息，自动给出通俗结论"),
                        original_lottery: bool = typer.Option(False, "--original-lottery", help="明确查看原摇号假设，不代表最新审核后位置"),
                        review_filter: bool = typer.Option(False,"--review-filter",help="按核查结果暂扣不合格及撤销，保留原队列假设")):
    """与网页使用相同规则版本的本地情景演算；不发送消息。"""
    from app.housing.opportunity import load_payload, analyze, analyze_simple
    from app.housing.lookup import build_directory
    from pathlib import Path
    payload = load_payload(build_directory(Path(page).parent) / Path(page).name)
    identifier = re.sub(r"\s+", "", apply_no).upper()
    matches = payload["rows"].get(identifier, [])
    if batch is not None: matches = [m for m in matches if m[0] == batch]
    if round_no is not None: matches = [m for m in matches if len(m)>6 and m[6] == round_no]
    if len(matches) != 1: raise typer.BadParameter("编号未匹配或命中多个批次／轮次，请明确 --batch 并核对记录")
    match = matches[0]
    available = payload["batches"][match[0]].get("opportunity", {}).get("projects", [])
    selected = []
    for token in filter(None, (p.strip() for p in projects.split(","))):
        candidates = [p["id"] for p in available if token in {p["id"], p["name"]}]
        if len(candidates) != 1: raise typer.BadParameter(f"项目须使用页面中的完整名称或ID：{token}")
        selected.append(candidates[0])
    _echo((analyze_simple if simple else analyze)(payload, match, {"apply_no": identifier, "projects": selected, "people": people,
          "children": children, "basis": basis, "ahead": ahead, "not_consuming": not_consuming, "queue":queue,
          "use_original_lottery":original_lottery,"use_review_filter":review_filter}))


@app.command("push")
def push_command(
    retry: int = typer.Option(None, help="兼容参数：重新冲刷待发队列"),
    limit: int = typer.Option(None),
) -> None:
    """把待推通告立即发出去（不重新扫描）。"""
    with SessionLocal() as session:
        result = asyncio.run(push_pending(session, limit))
        session.commit()
        _echo(result)
        _delivery_exit(result)


@app.command("list")
def list_command(
    open_only: bool = typer.Option(False, "--open", help="只看正在认租"),
    region: str = typer.Option(None),
    category: str = typer.Option(None),
    limit: int = typer.Option(20),
) -> None:
    now = utcnow()
    with SessionLocal() as session:
        statement = select(HousingNotice).order_by(
            HousingNotice.published_at.desc(), HousingNotice.priority.desc()
        )
        if open_only:
            statement = statement.where(
                HousingNotice.category == "rent_launch",
                HousingNotice.application_end.is_not(None),
                HousingNotice.application_end >= now,
                or_(HousingNotice.application_start.is_(None), HousingNotice.application_start <= now),
            )
        if region:
            statement = statement.where(HousingNotice.region_code.like(f"%{region}%"))
        if category:
            statement = statement.where(HousingNotice.category == category)
        rows = session.scalars(statement.limit(limit)).all()
    _echo([notice_to_dict(item) for item in rows])


@app.command("show")
def show_command(notice_id: int) -> None:
    with SessionLocal() as session:
        notice = session.get(HousingNotice, notice_id)
        if notice is None:
            raise typer.BadParameter(f"通告不存在: {notice_id}")
        payload = notice_to_dict(notice)
        payload["body_excerpt"] = (notice.body_text or "")[:800]
        payload["attachments"] = notice.attachments
        payload["instant_pushed_at"] = notice.instant_pushed_at
        payload["instant_push_status"] = notice.instant_push_status
        payload["batch"] = notice.batch_key_v2
        payload["batch_link_status"] = notice.batch_link_status
        payload["published_unverified"] = notice.published_unverified
        payload["business_version"] = notice.business_version
    _echo(payload)


@app.command("backfill")
def backfill_command(
    pages: int = typer.Option(5, help="每个源回填页数"),
    deliver: bool = typer.Option(False),
) -> None:
    result = asyncio.run(
        run_scan(max_pages=pages, with_detail=False, dry_run=False)
    )
    _echo({"status": result.status, "items_seen": result.items_seen, "items_new": result.items_new})
    if deliver:
        with SessionLocal() as session:
            _echo(asyncio.run(push_pending(session)))
            session.commit()


@app.command("doctor")
def doctor_command() -> None:
    _echo(doctor_report())


@app.command("reclassify")
def reclassify_command() -> None:
    """改过分类规则后，用已入库正文重跑分类与项目名清洗。"""
    _echo(reclassify())


@app.command("dedupe")
def dedupe_command() -> None:
    """合并早期采集留下的重复通告（同文号/同标题）。"""
    _echo(dedupe())


@app.command("purge")
def purge_command(days: int = typer.Option(None)) -> None:
    _echo(purge(days))


@app.command("migrate")
def migrate_command(apply: bool = typer.Option(False, "--apply")):
    """默认在临时数据库演练；--apply 才迁移正式库。"""
    from app.db import engine
    from app.housing.migrations import migrate, stabilize_group_config
    from app.housing.operations import backup_database
    if apply:
        backup_database(force=True)
    result = migrate(engine, apply=apply)
    if apply:
        stabilize_group_config(get_housing_settings().groups_path)
    _echo(result)


@app.command("batch-link")
def batch_link_command(notice_id: int, launch_id: int):
    from app.housing.batches import assign_batch
    with SessionLocal() as session:
        result = assign_batch(session, notice_id, launch_id)
        session.commit()
    _echo(result)


@app.command("outbox")
def outbox_command(status: str = typer.Option(None), limit: int = 100):
    from app.housing.models import HousingOutbox
    with SessionLocal() as session:
        query = select(HousingOutbox).order_by(HousingOutbox.id.desc()).limit(limit)
        if status:
            query = query.where(HousingOutbox.status == status)
        rows = session.scalars(query).all()
        _echo([{"id": r.id, "group_id": r.group_id, "kind": r.kind, "status": r.status,
                "attempts": r.attempts, "next_attempt_at": r.next_attempt_at,
                "error": r.error, "title": r.title} for r in rows])


@app.command("outbox-resolve")
def outbox_resolve_command(job_id: int, action: str, group_id: str = typer.Option(None), note: str = ""):
    from app.housing.outbox import resolve, scheduling_lock
    from app.housing.lock import process_lock
    from app.config import get_settings
    with process_lock(get_settings().data_dir / "housing.send.lock"), scheduling_lock(), SessionLocal() as session:
        result = resolve(session, job_id, action, group_id=group_id, note=note)
        session.commit()
    _echo(result)


@app.command("lookup-publish")
def lookup_publish_command(version: str = typer.Option(...)):
    """人工上传全部页面及 manifest 后，登记所上传的完整版本号。"""
    from app.housing.lookup import mark_published
    with SessionLocal() as session:
        result = mark_published(session, version)
        session.commit()
    _echo(result)


@app.command("repair-data")
def repair_data_command(days: int = 900, baseline: bool = typer.Option(False, "--baseline")):
    """重建关联、复核历史摇号/相关房源；baseline 不产生历史即时通知。"""
    from app.housing.repair import repair_data
    result = asyncio.run(repair_data(days=days, baseline=baseline))
    _echo(result)
    if result.get("lookup", {}).get("status") == "degraded":
        raise typer.Exit(code=2)


@app.command('selection-progress-import')
def selection_progress_import_command(
    notice_id:int=typer.Option(...,help='对应批次的官方公告ID'),
    file:str=typer.Option(...,help='官方Excel、CSV、HTML表格或PDF'),
    source:str=typer.Option(...,help='取得资料的HTTPS官方网址'),
    as_of:str=typer.Option(None,help='官方数据时间，ISO格式；未提供时明确标未知'),
    round_name:str=typer.Option('本批首轮',help='轮次；递补不能混入首轮'),
    scope:str=typer.Option('整批',help='资料覆盖范围'),
    complete:bool=typer.Option(False,help='确认资料完整覆盖所填范围'),
    apply:bool=typer.Option(False,'--apply',help='登记已核对的预览，不发送消息'),
    confirm_version:str=typer.Option(None,help='--apply必须填写刚预览的version'),
):
    from pathlib import Path
    from app.housing.selection_progress import prepare_import,register
    if apply:
        from app.housing.operations import backup_database
        backup_database(force=True)
    with SessionLocal() as session:
        notice=session.get(HousingNotice,notice_id)
        if not notice:raise typer.BadParameter('未找到官方公告')
        snapshot,data=prepare_import(session,notice,file,source,as_of=as_of,round_name=round_name,scope=scope,complete=complete)
        if not apply:
            _echo({'status':'preview','version':snapshot['version'],'as_of':snapshot['as_of'],'round':round_name,'scope':scope,
                   'complete':complete,'rows':snapshot['rows'],'families':snapshot['families'],'messages_sent':0})
        else:
            if confirm_version!=snapshot['version']:raise typer.BadParameter('预览版本不一致；请先预览、核对，再用--confirm-version登记')
            result=register(session,snapshot,data,Path(file).suffix.lower());session.commit();_echo(dict(result,messages_sent=0))


@app.command('selection-progress-status')
def selection_progress_status_command():
    from app.housing.reporting import build_snapshot
    from app.housing.selection_progress import source_candidates
    with SessionLocal() as session:
        snapshot=build_snapshot(session,'evening',now=utcnow())
        rows=[]
        for c in snapshot.cards:
            if c.profile.housing_type!='baozu' or c.stage not in {'selection','signing'}:continue
            rows.append({'batch':c.code,'region':c.profile.region,'audience':c.profile.audience,
                         'progress':c.selection_progress,'official_sources':list(dict.fromkeys(u for id in c.notice_ids
                             if (n:=session.get(HousingNotice,id)) for u in source_candidates(n)))})
        _echo({'batches':rows,'note':'仅代表已采集批次；不代表各区官方均有可读取数据'})


if __name__ == "__main__":
    try:
        app()
    except Exception:
        logging.getLogger(__name__).exception("housing command failed")
        raise
