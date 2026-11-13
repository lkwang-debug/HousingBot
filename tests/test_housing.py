from __future__ import annotations

import asyncio
import io
import json
import tempfile
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.housing.db import HousingBase
from app.housing.models import (
    HousingNotice, HousingBatchUnit, HousingLotteryStat, HousingGroupState,
    HousingOutbox, HousingOutboxEvent, HousingDelivery, HousingAttachmentState,
    HousingSource, utcnow,
)
from app.housing.groups import SubscribeGroup, sync_groups, load_groups
from app.housing.outbox import enqueue, flush_outbox, event_exists, resolve
from app.housing.batches import batch_key, rebuild_links
from app.housing.delivery import HousingPushClient, push_pending
from app.housing.testing import FixtureClient


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="housing-test-")
        self.root = Path(self.directory.name)
        self.engine = create_engine("sqlite:///:memory:")
        HousingBase.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.session = self.factory()
        self.groups = [SubscribeGroup(name=x, topic=x, tier="pro", id=x) for x in "ABC"]
        self.patches = [patch("app.housing.groups.load_groups", side_effect=lambda: self.groups),
            patch("app.housing.outbox.get_settings", return_value=SimpleNamespace(data_dir=self.root)),
            patch("app.housing.outbox._account_rate_limit", new=AsyncMock())]
        for p in self.patches:
            p.start()
        sync_groups(self.session, utcnow() - timedelta(days=1)); self.session.commit()

    def tearDown(self):
        self.session.close(); self.engine.dispose()
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    def notice(self, key, **kwargs):
        values = dict(canonical_key=key, canonical_url="https://example.invalid/" + key,
            url_hash=key, title=key, region_code="光明区", housing_types=["baozu"],
            is_target=True, category="rent_launch", is_push_candidate=True,
            business_version="v-" + key, event_at=utcnow(), published_at=utcnow(),
            attachments=[], projects=[])
        values.update(kwargs)
        notice = HousingNotice(**values)
        self.session.add(notice); self.session.flush()
        return notice

    def sender(self, responses=None, token="test"):
        calls = []
        responses = responses or {}
        async def submit(delivery, title, content, topic):
            calls.append((topic, title, content))
            outcome = responses.get(topic, "submitted")
            delivery.status = outcome
            delivery.response_payload = {"retryable": outcome == "failed"}
            if outcome == "submitted":
                delivery.provider_message_id = "mock-id"
        return SimpleNamespace(settings=SimpleNamespace(pushplus_token=token), _submit=submit,
            close=AsyncMock(), calls=calls)

    def flush(self, sender):
        return asyncio.run(flush_outbox(self.session, client=sender, rate_limit=False,
                                      lock_path=self.root / "send.lock"))


class BatchTests(DatabaseCase):
    def test_personal_enterprise_anchor_separation(self):
        personal = self.notice("personal", title="光明区2025年第四批次面向个人配租保障性租赁住房", doc_number="住保租〔2025〕058号")
        enterprise = self.notice("enterprise", title="光明区2025年第四批次面向企事业单位配租保障性租赁住房", doc_number="住保租〔2025〕059号")
        lottery = self.notice("lottery", title="光明区2025年第四批次面向个人配租保障性租赁住房公证摇号结果", category="lottery_result")
        unknown = self.notice("unknown", title="光明区2025年第四批次保障性租赁住房选房通知", category="process")
        rebuild_links(self.session)
        self.assertNotEqual(batch_key(personal), batch_key(enterprise))
        self.assertEqual(batch_key(lottery), batch_key(personal))
        self.assertIsNone(batch_key(unknown))

    def test_reference_resolves_unknown_but_not_conflicting_audience(self):
        launch = self.notice("p", title="2026年面向个人配租保障性租赁住房", doc_number="住保租〔2026〕038号")
        linked = self.notice("l", title="保障性租赁住房选房通告", category="process", body_text="配租通告住保租〔2026〕038号")
        conflict = self.notice("e", title="面向企事业单位保障性租赁住房选房通告", category="process", body_text="住保租〔2026〕038号")
        rebuild_links(self.session)
        self.assertEqual(batch_key(linked), batch_key(launch))
        self.assertIsNone(batch_key(conflict))

    def test_duplicate_launch_candidates_remain_unresolved(self):
        for key in ("p1", "p2"):
            self.notice(key, title="2026年第一批次面向个人配租保障性租赁住房")
        notice = self.notice("l", title="2026年第一批次面向个人保障性租赁住房摇号结果", category="lottery_result")
        rebuild_links(self.session)
        self.assertIsNone(batch_key(notice))
        self.assertEqual(notice.batch_link_status, "ambiguous")


class ParsingTests(DatabaseCase):
    def test_scan_dry_run_rolls_back_source_and_notice_changes(self):
        from app.housing.scan import run_scan
        from app.housing.sources import SourceDef
        from app.housing.config import HousingSettings
        source = HousingSource(key="fixture", name="fixture", entry_url="https://example.invalid/list/", region_code="光明区")
        self.session.add(source); self.session.commit()
        definition = SourceDef(key="fixture", name="fixture", region_code="光明区", entry_url=source.entry_url)
        html = '<li><a href="/content/post_123456.html">2026年第一批次面向个人保障性租赁住房配租通告</a><span>2026-10-21</span></li>'
        def sources(session):
            return [session.scalar(select(HousingSource).where(HousingSource.key == "fixture"))]
        with patch("app.housing.scan.SessionLocal", self.factory), patch("app.housing.scan.sync_sources", side_effect=sources), patch(
                "app.housing.scan.get_source_def", return_value=definition):
            result = asyncio.run(run_scan(dry_run=True, with_detail=False, max_pages=1,
                settings=HousingSettings(_env_file=None, enabled=True), client=FixtureClient({source.entry_url: html})))
        self.assertEqual(result.items_new, 1)
        self.assertEqual(len(list(self.session.scalars(select(HousingNotice)))), 0)

    def test_negated_qualifications_are_quoted(self):
        from app.housing.qualify import requirements, summary_lines
        notice = self.notice("n", title="面向个人配租保障性租赁住房", body_text="不限户籍，不要求社保满5年。符合A或B条件，特殊人员除外。")
        self.assertEqual(requirements(notice), ["不限户籍，不要求社保满5年"])
        output = " ".join(summary_lines(notice))
        self.assertNotIn("光明区户籍", output)
        self.assertNotIn("个人可以申请", output)

    def test_date_does_not_come_from_title(self):
        from app.housing.parsers.list_nfcms import parse_nfcms_list
        html = '<ul class="list"><li><a href="content/post_123456.html">关于2026年12月31日前到期住房续租的通知</a><span>2026-10-10</span></li></ul>'
        self.assertEqual(parse_nfcms_list(html, "https://example.invalid")[0].published_at.date(), datetime(2026, 10, 10).date())

    def test_missing_date_is_not_guessed_from_title(self):
        from app.housing.parsers.list_nfcms import parse_nfcms_list
        html = '<li><a href="content/post_123456.html">关于2026年12月31日前到期住房续租的通知</a></li>'
        self.assertIsNone(parse_nfcms_list(html, "https://example.invalid")[0].published_at)

    def test_impossible_order_has_no_virtual_rank(self):
        from app.housing.forecast import predict_whatif
        result = predict_whatif(10, {"第一队列": 10}, "第一队列", 9999)
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["rank"])
        self.assertEqual(predict_whatif(10, {"第一队列": 10}, "第一队列", rank=5)["rank"], 5)

    def test_real_sparse_order_distribution(self):
        from app.housing.forecast import predict_whatif
        result = predict_whatif(2, {"第一队列": 3}, "第一队列", 500, orders=[20, 500, 900])
        self.assertEqual(result["rank"], 2)
        self.assertNotIn("chance", result)

    def test_group_priority_is_shared_with_generated_pages(self):
        from app.housing.forecast import _ordered_queues
        from tools.build_lottery_lookup import _queue_sort_key
        queues = ["其他-第一队列", "优先-第二队列", "优先-第一队列"]
        expected = ["优先-第一队列", "优先-第二队列", "其他-第一队列"]
        self.assertEqual(_ordered_queues(dict.fromkeys(queues, 1)), expected)
        self.assertEqual(sorted(queues, key=_queue_sort_key), expected)

    def test_future_date_is_quarantined(self):
        from app.housing.store import store_notice
        source = HousingSource(key="src", name="source", entry_url="https://example.invalid", region_code="光明区")
        self.session.add(source); self.session.flush()
        notice, _ = store_notice(self.session, source=source, title="面向个人保障性租赁住房配租通告",
            url="https://example.invalid/content/post_123456.html", published_at=utcnow() + timedelta(days=40))
        self.assertTrue(notice.published_unverified)
        self.assertIsNone(notice.published_at)

    def test_whitespace_is_not_a_business_update(self):
        from app.housing.events import business_version
        notice = self.notice("n", title="保障性租赁住房 配租通告", body_text="社保满 5 年。")
        original = business_version(notice)
        notice.title = "保障性租赁住房配租通告"; notice.body_text = "社保满5年。"
        self.assertEqual(original, business_version(notice))

    def test_open_window_and_reminder_boundaries(self):
        from app.housing.render import _open_notices
        from app.housing.remind import _deadline_items
        now = utcnow().replace(hour=12)
        self.notice("future", application_start=now + timedelta(days=1), application_end=now + timedelta(days=5))
        current = self.notice("current", application_start=now - timedelta(days=1), application_end=now + timedelta(days=3))
        self.notice("four", application_end=now + timedelta(days=4))
        self.assertNotIn("future", [n.canonical_key for n in _open_notices(self.session, now)])
        items = _deadline_items(self.session, now)
        self.assertEqual([i.notice.id for i in items], [current.id])
        first = items[0].base_key
        current.application_end += timedelta(hours=1)
        self.assertNotEqual(first, _deadline_items(self.session, now)[0].base_key)


class OutboxTests(DatabaseCase):
    def test_two_processes_claim_one_job_once(self):
        database = self.root / "concurrent.db"
        engine = create_engine(f"sqlite:///{database.as_posix()}")
        HousingBase.metadata.create_all(engine)
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            session.add(HousingGroupState(group_id="A", name="A", topic="A"))
            session.flush()
            enqueue(session, self.groups[0], "instant", "title", "content", [("once", None)])
            session.commit()
        engine.dispose()
        code = '''import asyncio,sys,time
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.housing.outbox import flush_outbox
async def submit(delivery,*args):
    with open(sys.argv[2],"a",encoding="utf-8") as f: f.write("sent\\n")
    await asyncio.sleep(.1)
    delivery.status="submitted"
    delivery.response_payload={}
sender=SimpleNamespace(settings=SimpleNamespace(pushplus_token="fake"),_submit=submit)
engine=create_engine("sqlite:///"+sys.argv[1],connect_args={"timeout":10})
with Session(engine,expire_on_commit=False,autoflush=False) as session:
    asyncio.run(flush_outbox(session,client=sender,rate_limit=False,lock_path=Path(sys.argv[3])))
engine.dispose()
'''
        args = [sys.executable, "-X", "utf8", "-c", code, str(database), str(self.root / "sent.txt"), str(self.root / "process.lock")]
        workers = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for worker in workers:
            _, errors = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, errors.decode(errors="replace"))
        self.assertEqual((self.root / "sent.txt").read_text().splitlines(), ["sent"])

    def test_expired_digest_is_not_sent_next_morning(self):
        job = enqueue(self.session, self.groups[0], "digest", "t", "c", [("digest:old", None)])
        job.created_at = utcnow() - timedelta(days=1); self.session.commit()
        sender = self.sender()
        asyncio.run(flush_outbox(self.session, client=sender, now=utcnow().replace(hour=9), rate_limit=False,
                                lock_path=self.root / "send.lock"))
        self.assertFalse(sender.calls)
        self.assertEqual(job.status, "cancelled")

    def test_three_groups_partial_failure_frozen_retry(self):
        sender = self.sender({"B": "failed"})
        self.notice("one"); self.notice("two"); self.session.commit()
        with patch("app.housing.delivery.HousingPushClient", return_value=sender), patch(
                "app.housing.render.render_instant_parts", side_effect=lambda notices, **_: [__import__("app.housing.report_render", fromlist=["ReportPart"]).ReportPart("same title", ",".join(n.canonical_key for n in notices), tuple(n.id for n in notices), ())]):
            asyncio.run(push_pending(self.session))
            self.assertEqual(len(sender.calls), 3)
            self.assertEqual(len({c[2] for c in sender.calls}), 1)
            failed = self.session.scalar(select(HousingOutbox).where(HousingOutbox.group_id == "B"))
            frozen = failed.content
            self.notice("three"); self.session.commit()
            failed.next_attempt_at = utcnow() - timedelta(seconds=1); self.session.commit()
            sender.calls.clear(); sender._submit = self.sender()._submit
            # A new event is separate from the immutable failed packet.
            result = asyncio.run(push_pending(self.session))
            self.assertEqual(self.session.get(HousingOutbox, failed.id).content, frozen)
            self.assertEqual(self.session.get(HousingOutbox, failed.id).status, "submitted")
            a_packets = list(self.session.scalars(select(HousingOutbox).where(HousingOutbox.group_id == "A")))
            self.assertEqual(len(a_packets), 2)
            self.assertEqual(a_packets[-1].content, "three")

    def test_missing_token_does_not_consume_attempt(self):
        job = enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)])
        self.session.commit()
        self.flush(self.sender(token=""))
        self.assertEqual(job.status, "blocked"); self.assertEqual(job.attempts, 0)
        self.flush(self.sender())
        self.assertEqual(job.status, "submitted"); self.assertEqual(job.attempts, 1)

    def test_timeout_requires_manual_resolution(self):
        job = enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)])
        self.session.commit()
        sender = self.sender({"A": "needs_review"})
        self.flush(sender); self.flush(sender)
        self.assertEqual(len(sender.calls), 1)
        resolve(self.session, job.id, "retry", note="人工确认群未收到"); self.session.commit()
        self.flush(self.sender())
        self.assertEqual(job.attempts, 2)
        self.assertEqual(len(list(self.session.scalars(select(HousingDelivery)))), 2)

    def test_interrupted_send_is_not_retried(self):
        job = enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)])
        job.status = "sending"; self.session.commit()
        sender = self.sender(); self.flush(sender)
        self.assertEqual(job.status, "needs_review"); self.assertFalse(sender.calls)

    def test_permanent_failure_not_retried(self):
        job = enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)])
        self.session.commit()
        sender = self.sender()
        async def refused(delivery, *_):
            sender.calls.append("call"); delivery.status = "failed"; delivery.response_payload = {"retryable": False}
        sender._submit = refused
        self.flush(sender); self.flush(sender)
        self.assertEqual(len(sender.calls), 1)

    def test_pause_resume_and_rename(self):
        activated = self.session.get(HousingGroupState, "A").activated_at
        self.groups[0] = SubscribeGroup(name="new display", topic="A", tier="pro", id="A", enabled=False)
        sync_groups(self.session); self.session.commit()
        self.groups[0] = SubscribeGroup(name="renamed", topic="A", tier="pro", id="A")
        sync_groups(self.session); self.session.commit()
        state = self.session.get(HousingGroupState, "A")
        self.assertEqual(state.activated_at, activated)
        self.assertGreater(state.eligible_since, activated)
        self.assertEqual(state.name, "renamed")

    def test_new_group_gets_no_old_instant(self):
        old = self.notice("old"); self.session.commit()
        self.groups.append(SubscribeGroup(name="D", topic="D", tier="pro", id="D"))
        sender = self.sender()
        with patch("app.housing.delivery.HousingPushClient", return_value=sender), patch(
                "app.housing.render.render_instant_parts", side_effect=lambda notices, **_: [__import__("app.housing.report_render", fromlist=["ReportPart"]).ReportPart("title", "content", tuple(n.id for n in notices), ())]):
            asyncio.run(push_pending(self.session))
        self.assertNotIn("D", [c[0] for c in sender.calls])
        self.assertEqual(len(sender.calls), 3)

    def test_topic_change_requires_review(self):
        job = enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)])
        self.session.get(HousingGroupState, "A").topic = "new-topic"; self.session.commit()
        sender = self.sender(); self.flush(sender)
        self.assertFalse(sender.calls); self.assertEqual(job.status, "needs_review")

    def test_unique_event_across_packets(self):
        enqueue(self.session, self.groups[0], "instant", "t", "c", [("event", None)]); self.session.commit()
        self.assertIsNone(enqueue(self.session, self.groups[0], "instant", "new", "new", [("event", None)]))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_ambiguous_and_invalid_json_is_ambiguous(self):
        async def timeout(request):
            raise httpx.ReadTimeout("timeout", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            client = HousingPushClient(http); client.settings = SimpleNamespace(pushplus_token="fake")
            delivery = SimpleNamespace(response_payload={})
            await client._submit(delivery, "title", "content", "topic")
            self.assertEqual(delivery.status, "needs_review")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not-json"))) as http:
            client = HousingPushClient(http); client.settings = SimpleNamespace(pushplus_token="fake")
            delivery = SimpleNamespace(response_payload={})
            await client._submit(delivery, "title", "content", "topic")
            self.assertEqual(delivery.status, "needs_review")


class AttachmentTests(DatabaseCase):
    def test_units_use_explicit_subtotals_and_generic_area_bands(self):
        from app.housing.lottery import parse_units_overview
        text = "小区A\n70左右\n12\n小区B\n70左右\n33\n小计\n40左右\n629\n70左右\n645\n（二）费用租金"
        result = parse_units_overview(text)
        self.assertEqual(result["70"][1], 645)
        self.assertEqual(result["40"][1], 629)
        self.assertNotIn("70", parse_units_overview("小区A 70左右 12 小区B 70左右 33"))
        interval = parse_units_overview("两房\n63-67㎡左右\n154\n注：以实际为准")
        self.assertEqual(interval["63-67"][1], 154)
        self.assertNotIn("67", interval)
        self.assertEqual(parse_units_overview("一、申请对象 注：需核查。二、房源概况\n63-67㎡左右\n154\n注：见原文")["63-67"][1], 154)

    def test_same_url_changed_bytes_create_new_event_once(self):
        from app.housing.ingestion import attachment_event
        notice = self.notice("n")
        record = HousingAttachmentState(notice_id=notice.id, purpose="lottery", status='ok', content_hashes={"same-url": "one"})
        self.session.add(record); self.session.flush()
        attachment_event(self.session, notice)
        first_version = notice.business_version
        record.content_hashes = {"same-url": "two"}
        self.session.flush()
        attachment_event(self.session, notice)
        self.assertNotEqual(first_version, notice.business_version)
        changed_at = notice.event_at
        attachment_event(self.session, notice)
        self.assertEqual(changed_at, notice.event_at)

    def test_compound_area_is_not_merged_with_one_size(self):
        from app.housing.lottery import category_key
        self.assertEqual(category_key("85、95㎡左右摇号名单"), "85,95")
        self.assertNotEqual(category_key("85、95㎡左右摇号名单"), category_key("95㎡左右摇号名单"))

    def xlsx(self, rows):
        from openpyxl import Workbook
        workbook = Workbook(); sheet = workbook.active
        for row in rows:
            sheet.append(row)
        stream = io.BytesIO(); workbook.save(stream); workbook.close()
        return stream.getvalue()

    def test_partial_failure_rebuild_does_not_double_counts(self):
        from app.housing.ingestion import ingest_lottery
        notice = self.notice("lottery", category="lottery_result", title="面向个人保障性租赁住房摇号结果", detail_status="ok", detail_checked_at=utcnow(),
            attachments=[{"name": "40平方米第一队列摇号名单.xlsx", "url": "one", "kind": "xlsx"},
                         {"name": "40平方米第二队列摇号名单.xlsx", "url": "two", "kind": "xlsx"}])
        notice.batch_key_v2 = "v2:verified"
        data = self.xlsx([["顺序号", "认租编号", "队列"], [1, "TEST000001", "第一队列"]])
        client = FixtureClient({"one": data})
        first = asyncio.run(ingest_lottery(self.session, client, notice, force=True))
        self.assertEqual(first[0].total_rows, 1); self.assertFalse(first[0].complete)
        client.mapping["two"] = self.xlsx([["顺序号", "认租编号", "队列"], [2, "TEST000002", "第二队列"]])
        self.session.commit(); self.session.expunge_all(); notice = self.session.get(HousingNotice, notice.id)
        second = asyncio.run(ingest_lottery(self.session, client, notice, force=True))
        self.assertEqual(sum(r.total_rows for r in second), 2)
        self.assertTrue(all(r.complete for r in second))

    def test_units_xlsx_distinguishes_layout_from_area(self):
        from app.housing.ingestion import parse_units_table
        result = parse_units_table([["户型", "套数"], ["40平方米", "11"], ["一房", "7"]])
        self.assertEqual(result["40"][1], 11)
        self.assertEqual(result["一房"][1], 7)
        self.assertNotIn("50", result)

    def test_rounds_are_preserved_and_duplicates_disable_estimates(self):
        from app.housing.lottery import LotteryRow
        from app.housing.ingestion import row_issues
        issues = row_issues([LotteryRow(1, "TEST000001", "第一队列", round=1),
                            LotteryRow(2, "TEST000001", "第一队列", round=2)])
        self.assertTrue(any("多轮" in issue for issue in issues))


class LookupTests(DatabaseCase):
    def test_publication_receipt_rejects_changed_sources(self):
        from app.housing.lookup import build_regions, mark_published, source_version
        notice = self.notice("lottery", category="lottery_result")
        self.session.commit()
        payload = self.payload()
        payload["batches"][0]["id"] = notice.id
        payload["batches"][0]["version"] = source_version(self.session, notice)
        directory = self.root / "lookup"
        with patch("tools.build_lottery_lookup._build", new=AsyncMock(return_value=payload)):
            result = asyncio.run(build_regions(["测试区"], directory=directory))
        self.assertEqual(mark_published(self.session, result["version"], directory)["notices"], 1)
        notice.business_version = "changed"; self.session.flush()
        with self.assertRaisesRegex(ValueError, "源数据已更新"):
            mark_published(self.session, result["version"], directory)

    @unittest.skipUnless(shutil.which("node"), "Node is required for actual browser-script execution")
    def test_editing_identifier_clears_official_result_and_scenario(self):
        from tools.build_lottery_lookup import _render_html
        payload = self.payload()
        payload["batches"][0]["c"][0].update({"n": 3, "u": 3, "q": {"第一队列": 1, "第二队列": 2}})
        payload["batches"][0]["R"]["0|1"] = [3, 7]
        script = re.search(r"<script>\s*(const DATA = .*?)</script>", _render_html(payload), re.S)[1]
        harness = '''const vm=require('vm'); const nodes={};
const document={getElementById:(id)=>(nodes[id]||=( {innerHTML:'',style:{},value:'TEST000001',addEventListener:()=>{}} ))};
const context=vm.createContext({document});
vm.runInContext(SCRIPT,context); nodes['q-queue'].value='第一队列';vm.runInContext('run()',context);
if(!nodes.out.innerHTML.includes('第 1 位')) throw new Error(nodes.out.innerHTML);
if(!nodes.out.innerHTML.includes('本次查询：TEST000001')) throw new Error('query ID missing');
vm.runInContext('resetQuery()',context);
if(nodes.out.innerHTML!==''||nodes.out.style.display!=='none') throw new Error('stale result remained');'''
        # Windows limits command-line length; send the actual page script over
        # stdin so this regression keeps exercising the complete browser code.
        run = subprocess.run([shutil.which("node"), "-e", harness.replace("SCRIPT", "require('fs').readFileSync(0,'utf8')")],
                             input=script.encode('utf-8'), capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors="replace"))

    def payload(self):
        return {"district": "测试区", "batches": [{"id": 1, "version": "v", "t": "测试", "u": "https://example.invalid",
            "d": "2026-10-21", "Q": ["第一队列", "第二队列"], "c": [{"l": "40", "n": 1, "u": 0, "q": {"第一队列": 1}, "issue": ""}],
            "R": {"0|0": [5]}}], "rows": {"TEST000001": [[0, 0, 0, 5, 1, 0, 0]]}}

    def test_failed_build_preserves_existing_page(self):
        from app.housing.lookup import build_regions
        directory = self.root / "lookup"; directory.mkdir()
        page = directory / "index.html"; page.write_text("old", encoding="utf-8")
        with patch("tools.build_lottery_lookup._build", new=AsyncMock(side_effect=ValueError("bad attachment"))):
            with self.assertRaises(ValueError):
                asyncio.run(build_regions(["测试"], directory=directory))
        self.assertEqual(page.read_text(encoding="utf-8"), "old")

    def test_payload_validation_and_precomputed_rank_code(self):
        from app.housing.lookup import validate_payload
        from tools.build_lottery_lookup import _render_html
        self.assertTrue(validate_payload(self.payload()))
        html = _render_html(self.payload())
        self.assertIn("综合摇号排名", html)
        self.assertIn("梯队排名", html)
        self.assertNotIn("for (const key in DATA.rows)", html)


class MigrationTests(DatabaseCase):
    def test_repeatable_housing_only_migration(self):
        from app.housing.migrations import migrate
        self.session.execute(text("CREATE TABLE securities (id INTEGER PRIMARY KEY, value TEXT)"))
        self.session.execute(text("INSERT INTO securities VALUES (1, 'keep')"))
        self.session.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        self.session.execute(text("INSERT INTO alembic_version VALUES ('keep-version')"))
        self.session.commit()
        self.assertEqual(migrate(self.engine, apply=True)["status"], "migrated")
        self.assertEqual(migrate(self.engine, apply=True)["status"], "already_migrated")
        self.assertEqual(self.session.execute(text("SELECT value FROM securities")).scalar(), "keep")
        self.assertEqual(self.session.execute(text("SELECT version_num FROM alembic_version")).scalar(), "keep-version")


if __name__ == "__main__":
    unittest.main()
