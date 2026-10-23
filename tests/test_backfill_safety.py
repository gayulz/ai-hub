"""C4 호출 예약·정책·AGY 메타데이터·쓰기 충돌의 합성 검증."""

import gzip
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest import mock

from collector import backfill, backfill_agy, backfill_output, config, summarize, vault
from collector.backfill_common import locked, sha
from collector.backfill_store import Store
from collector.ledger import Ledger
from test_backfill_support import BackfillCase


class BackfillSafetyTest(BackfillCase):
	def test_failed_fallback_and_timeouts_each_consume_one_call(self):
		(self.hub / "plans.toml").write_text(
			"[[plans]]\nid='p01'\ntool='codex'\nowner='personal'\nallow=['personal']\n"
			"[[plans]]\nid='p02'\ntool='claude'\nowner='personal'\nallow=['personal']\n"
			"[routing]\npersonal=['p01','p02']\n[backfill]\ndaily_limit_calls=2\n", encoding="utf-8")
		self.codex(sid="s01")
		self.codex(sid="s02")

		def fake(plan, prompt):
			if plan.id == "p01":
				raise subprocess.TimeoutExpired("합성 도구", 180)
			return "- 합성 결과"

		self.ai.side_effect = fake
		self.prepare()
		self.assertEqual([c.args[0].id for c in self.ai.call_args_list], ["p01", "p02"])
		batch, tasks = self.batch()
		self.assertEqual([t["state"] for t in tasks], ["prepared", "held"])
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual(len(store.rows("c4_calls")), 2)
		self.assertEqual(store.calls_today("p01", "2026-10-23"), 1)
		self.assertEqual(store.calls_today("p02", "2026-10-23"), 1)
		store.close()
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 2)

	def test_plan_limit_includes_c1_calls_and_c4_defaults_are_used(self):
		self.plans(per_plan=1)
		ledger = Ledger(self.hub / "ledger.db")
		ledger.bump_call("p01", "2026-10-23")
		ledger.close()
		self.codex()
		self.prepare()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")
		(self.hub / "plans.toml").write_text("[routing]\npersonal=[]\n", encoding="utf-8")
		plans = config.load_plans()
		self.assertEqual((plans.backfill_daily_limit, plans.backfill_batch_size), (20, 10))

	def test_atomic_reservations_from_separate_connections_never_exceed_limit(self):
		store = Store(self.hub / "ledger.db")
		store.close()
		plan = config.load_plans().plans["p01"]

		def reserve(number):
			store = Store(self.hub / "ledger.db")
			try:
				return store.reserve("b", {"key": f"codex:s{number}", "hash": str(number)}, 0, plan, 3)
			finally:
				store.close()

		with ThreadPoolExecutor(max_workers=8) as pool:
			self.assertEqual(sum(x is not None for x in pool.map(reserve, range(20))), 3)
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual(store.calls_today("p01", "2026-10-23"), 3)
		self.assertEqual(len(store.rows("c4_calls")), 3)
		store.close()

	def test_midnight_rechecks_seoul_day_before_each_call(self):
		self.plans(limit=1)
		self.codex(sid="s01")
		self.codex(sid="s02")
		def fake(plan, prompt):
			self.clock = self.clock.replace(day=24)
			return "- 합성 결과"
		self.ai.side_effect = fake
		self.prepare()
		self.assertEqual(self.ai.call_count, 2)
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual({r["day"] for r in store.rows("c4_calls")}, {"2026-10-23", "2026-10-24"})
		store.close()

	def test_company_c4_routing_cannot_expand_allow_list(self):
		self.plans(company=False, extra="[backfill.routing]\ncompany=['p01']\n")
		with self.assertRaises(config.ConfigError):
			config.load_plans()
		for bad in (0, -1, True, "many"):
			self.plans()
			path = self.hub / "plans.toml"
			path.write_text(path.read_text().replace("daily_limit_calls = 20", "daily_limit_calls = " + json.dumps(bad)))
			with self.subTest(value=bad), self.assertRaises(config.ConfigError):
				config.load_plans()

	def test_prepared_results_and_existing_block_previews_are_redacted(self):
		self.codex(question="질문 password=sample-input", answer="대답 token=sample-answer")
		self.ai.return_value = "- 결과 api_key=sample-output"
		path = vault.daily_path(self.vault, "personal", date(2026, 8, 1))
		vault.upsert_block(path, "codex:s01", "과거 메모 password=sample-before", "acme", date(2026, 8, 1), "personal")
		self.prepare()
		batch, _ = self.batch()
		report = Path(batch["report"])
		store = Store(self.hub / "ledger.db", readonly=True)
		data = "\n".join(r["data"] for r in store.rows("c4_items")) + report.read_text()
		store.close()
		for token in ("sample-input", "sample-answer", "sample-output", "sample-before"):
			self.assertNotIn(token, data)
			self.assertNotIn(token, self.ai.call_args.args[1])
		self.assertEqual(report.stat().st_mode & 0o777, 0o600)
		self.assertEqual(report.parent.stat().st_mode & 0o777, 0o700)

	def test_vaultsync_lock_defers_apply_without_new_ai_call(self):
		self.codex()
		self.prepare()
		batch, _ = self.batch()
		with locked("vaultsync", exact=self.hub / "run/vaultsync.lock"):
			self.assertEqual(self.command("--apply", batch["id"]), 2)
		self.assertEqual(self.snapshot(self.vault), {})
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.command("--apply", batch["id"]), 0)

	def test_apply_after_write_before_ledger_update_recovers_from_cached_block(self):
		self.codex()
		self.prepare()
		batch, _ = self.batch()
		with mock.patch.object(Store, "put", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
			self.command("--apply", batch["id"])
		before = self.snapshot(self.vault)
		self.command("--apply", batch["id"])
		self.assertEqual(self.snapshot(self.vault), before)
		self.assertEqual(self.batch()[1][0]["state"], "applied")
		self.assertEqual(self.ai.call_count, 1)

	def test_dry_run_reads_wal_ledger_without_changing_live_files(self):
		self.codex()
		ledger = Store(self.hub / "ledger.db")
		ledger.conn.execute("pragma journal_mode=wal")
		ledger.conn.execute("pragma wal_autocheckpoint=0")
		ledger.bump_call("p01", "2026-10-23")
		before = self.snapshot()
		reader = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual(reader.calls_today("p01", "2026-10-23"), 1)
		with self.assertRaises(sqlite3.OperationalError):
			reader.conn.execute("insert into runs(started) values ('acme')")
		reader.close()
		self.assertEqual(self.prepare("sessions", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		ledger.close()

	def test_c1_claim_defers_c4_without_creating_a_second_owner(self):
		self.codex()
		ledger = Ledger(self.hub / "ledger.db")
		self.assertTrue(ledger.claim("codex:s01", "run:acme"))
		self.prepare()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")
		ledger.release("codex:s01", "run:acme")
		ledger.close()
		self.command("--resume", self.batch()[0]["id"])
		self.assertEqual(self.ai.call_count, 1)

	def test_dead_c1_process_claim_is_released_but_c4_approval_claim_is_kept(self):
		ledger = Ledger(self.hub / "ledger.db")
		ledger.claim("codex:s01", "run:12345:acme")
		ledger.claim("codex:s02", "backfill:acme")
		with mock.patch("collector.ledger.os.kill", side_effect=ProcessLookupError()):
			self.assertTrue(ledger.claim("codex:s01", "backfill:xyz"))
			self.assertFalse(ledger.claim("codex:s02", "run:12345:xyz"))
		ledger.close()

	def test_company_route_must_also_allow_company(self):
		self.sources(cls="company")
		path = self.hub / "plans.toml"
		path.write_text("[[plans]]\nid='p01'\ntool='codex'\nowner='personal'\nallow=['personal']\n"
			"[routing]\ncompany=['p01']\n", encoding="utf-8")
		self.codex()
		self.prepare()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_missing_landing_is_held_without_creating_folders(self):
		(self.vault / "📥 Inbox/AI").rmdir()
		self.codex()
		self.prepare()
		self.assertEqual(self.batch()[1][0]["state"], "held")
		self.assertFalse((self.vault / "📥 Inbox/AI").exists())
		self.ai.assert_not_called()


class BackfillAgyTest(BackfillCase):
	def agy(self):
		path = self.root / "metadata.db"
		conn = sqlite3.connect(path)
		conn.execute("create table conversation_summaries (conversation_id text, agent_name text, "
			"workspace_uris text, step_count integer, last_modified_time text, body blob)")
		conn.executemany("insert into conversation_summaries values (?, ?, ?, ?, ?, ?)", [
			("a01", None, json.dumps([self.repo.as_uri()]), 2, "2026-08-01T01:00:00Z", b"DO-NOT-READ"),
			("a02", "xyz", "[]", 20, "2026-08-01T01:00:00Z", b"DO-NOT-READ"),
			("a03", "xyz", json.dumps(["file:///unclassified/acme"]), 20, "2026-08-01T01:00:00Z", b"DO-NOT-READ")])
		conn.commit()
		conn.close()
		self.gzip("agy/antigravity-cli/conversations/a04.db.gz", "DO-NOT-READ")
		target = self.hub / "archive/agy/antigravity-cli/conversation_summaries.db.gz"
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(gzip.compress(path.read_bytes()))
		return target

	def test_agy_c_policy_reports_metadata_and_pending_ids_without_vault_or_ai(self):
		self.agy()
		original_connect = sqlite3.connect
		queries = []

		def connect(*args, **kwargs):
			conn = original_connect(*args, **kwargs)
			conn.set_trace_callback(queries.append)
			return conn

		with mock.patch.object(backfill_agy.sqlite3, "connect", side_effect=connect):
			self.assertEqual(self.prepare(), 0)
		report = self.hub / "backfill/agy-report.md"
		text = report.read_text()
		for label in ("agent_name", "workspace_uris", "step_count", "a01", "a02", "a03", "a04"):
			self.assertIn(label, text)
		self.assertIn("경로 미확인 3건", text)
		self.assertFalse(any("select" in q.lower() and "body" in q.lower() for q in queries))
		self.assertNotIn("DO-NOT-READ", text)
		self.ai.assert_not_called()
		self.assertEqual(self.snapshot(self.vault), {})

	def test_agy_dry_run_does_not_write_report_or_ledger(self):
		self.agy()
		before = self.snapshot()
		self.assertEqual(self.prepare("sessions", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.ai.assert_not_called()
