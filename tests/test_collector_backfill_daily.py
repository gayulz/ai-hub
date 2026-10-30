"""일일 소급은 합성 원천과 가짜 호출만 사용한다."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from collector import __main__ as collector_cli, backfill, backfill_daily, backfill_output, config
from collector.backfill_store import Store
from test_backfill_support import BackfillCase


class DailyBackfillTest(BackfillCase):
	def setUp(self):
		super().setUp()
		self.clock = self.clock.replace(hour=3)
		self.clock_patch = mock.patch.object(backfill_daily, "now", side_effect=lambda: self.clock)
		self.clock_patch.start()
		self.addCleanup(self.clock_patch.stop)
		self.send_patch = mock.patch("office.telegram_client.Client.send_message")
		self.send = self.send_patch.start()
		self.addCleanup(self.send_patch.stop)

	def run_daily(self):
		backfill_daily.run(config.load_sources())

	def test_collector_run_triggers_daily_but_dry_run_does_not(self):
		sources = config.load_sources()
		ledger = mock.Mock()
		runner = mock.Mock(counts={}, excluded_titles=[])
		with mock.patch.object(collector_cli, "cmd_archive"), \
				mock.patch.object(collector_cli.pipeline, "load_all", return_value=(sources, None, [])), \
				mock.patch.object(collector_cli.pipeline, "open_ledger", return_value=ledger), \
				mock.patch.object(collector_cli.pipeline, "Pipeline", return_value=runner), \
				mock.patch.object(backfill_daily, "run") as daily:
			self.assertEqual(collector_cli.cmd_run(SimpleNamespace(dry_run=False, since=None)), 0)
			daily.assert_called_once_with(sources)
			daily.reset_mock()
			self.assertEqual(collector_cli.cmd_run(SimpleNamespace(dry_run=True, since=None)), 0)
			daily.assert_not_called()

	def test_personal_auto_applies_once_and_reports(self):
		self.codex()
		self.run_daily()
		batch, items = self.batch()
		self.assertEqual(items[0]["state"], "applied")
		before = self.snapshot(self.vault)
		self.run_daily()
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.send.call_count, 1)
		self.assertEqual(before, self.snapshot(self.vault))
		self.assertIn("개인 자동 반영 1건", self.send.call_args.args[0])

	def test_company_local_only_and_safe_summary(self):
		self.sources(cls="company")
		(self.hub / "plans.toml").write_text("[[plans]]\nid='p02'\ntool='ollama'\nowner='company'\n"
			"allow=['company']\nmodel='synthetic'\n[routing]\ncompany=['p02']\n")
		self.codex(question="회사 비밀 내용", answer="비밀 결과")
		self.run_daily()
		batch, items = self.batch()
		self.assertEqual(items[0]["state"], "prepared")
		self.assertEqual(self.snapshot(self.vault), {})
		self.assertEqual(self.ai.call_args.args[0].tool, "ollama")
		message = self.send.call_args.args[0]
		self.assertIn("회사 승인 대기 1묶음", message)
		evidence = Path(batch["report"]).read_text()
		for value in ("비밀", str(self.repo), "acme", "p01"):
			self.assertNotIn(value, message)
			self.assertNotIn(value, evidence)
		self.assertFalse((self.hub / "backfill/agy-report.md").exists())

	def test_daily_apply_rejects_company_at_the_write_boundary(self):
		self.sources(cls="company")
		(self.hub / "plans.toml").write_text("[[plans]]\nid='p02'\ntool='ollama'\nowner='company'\n"
			"allow=['company']\nmodel='synthetic'\n[routing]\ncompany=['p02']\n")
		self.codex()
		self.run_daily()
		batch, _ = self.batch()
		with self.assertRaises(config.ConfigError):
			backfill.execute(apply=batch["id"], daily=True)
		self.assertEqual(self.snapshot(self.vault), {})
		self.assertEqual(self.batch()[1][0]["state"], "prepared")

	def test_limit_and_held_batch_does_not_block_next_day(self):
		self.plans(limit=1, size=1)
		self.codex(sid="s01", answer=None)
		self.codex(sid="s02")
		self.codex(sid="s03")
		self.run_daily()
		self.assertEqual(self.batch()[1][0]["state"], "held")
		self.clock += timedelta(days=1)
		self.run_daily()
		self.assertEqual(self.batch()[1][0]["state"], "applied")
		self.assertEqual(self.ai.call_count, 1)
		self.clock += timedelta(days=1)
		self.run_daily()
		self.assertEqual(self.ai.call_count, 2)

	def test_budget_and_failure_report(self):
		self.plans(limit=1, size=2)
		self.codex(sid="s01")
		self.codex(sid="s02")
		self.send.side_effect = RuntimeError("secret")
		self.run_daily()
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual([i["state"] for i in self.batch()[1]], ["applied", "held"])
		message = self.send.call_args.args[0]
		self.assertIn("호출 한도", message)
		store = Store(self.hub / "ledger.db", readonly=True)
		try:
			self.assertEqual(store.rows("c4_daily_report")[0]["status"], "failed:RuntimeError")
		finally:
			store.close()

	def test_interrupted_after_call_recovers_without_duplicate(self):
		self.codex()
		original = Store.save
		interrupted = False
		def save(store, bid, task, state):
			nonlocal interrupted
			if state == "prepared" and not interrupted:
				interrupted = True
				raise KeyboardInterrupt()
			return original(store, bid, task, state)
		with mock.patch.object(Store, "save", new=save), self.assertRaises(KeyboardInterrupt):
			self.run_daily()
		self.run_daily()
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.batch()[1][0]["state"], "applied")

	def test_new_archive_after_exhaustion_is_discovered(self):
		self.codex(sid="s01")
		self.run_daily()
		self.codex(sid="s02")
		self.clock += timedelta(days=1)
		self.run_daily()
		self.assertEqual(self.ai.call_count, 2)
		self.assertEqual(self.batch()[1][0]["key"], "codex:s02")
		self.assertEqual(self.batch()[1][0]["state"], "applied")

	def test_company_external_route_is_not_called(self):
		self.sources(cls="company")
		self.codex()
		self.run_daily()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_manual_resume_of_daily_company_batch_never_calls_external_route(self):
		self.sources(cls="company")
		self.codex()
		self.run_daily()
		bid = self.batch()[0]["id"]
		backfill.execute(resume=bid)
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_manual_resume_before_daily_batch_link_stays_local(self):
		self.sources(cls="company")
		self.codex()
		store = Store(self.hub / "ledger.db")
		try:
			store.conn.executescript(backfill_daily.SCHEMA)
			with store.conn:
				store.conn.execute("insert into c4_daily(day,project,baseline) values (?,?,0)",
					(self.clock.date().isoformat(), "p01"))
		finally:
			store.close()
		backfill.execute(project_id="p01", kind="sessions", daily=True)
		backfill.execute(resume=self.batch()[0]["id"])
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_company_remote_ollama_is_not_called(self):
		self.sources(cls="company")
		(self.hub / "plans.toml").write_text("[[plans]]\nid='p02'\ntool='ollama'\nowner='company'\n"
			"allow=['company']\nmodel='synthetic'\nurl='http://example.invalid:11434'\n[routing]\ncompany=['p02']\n")
		self.codex()
		self.run_daily()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_company_local_url_through_proxy_is_not_called(self):
		self.sources(cls="company")
		(self.hub / "plans.toml").write_text("[[plans]]\nid='p02'\ntool='ollama'\nowner='company'\n"
			"allow=['company']\nmodel='synthetic'\n[routing]\ncompany=['p02']\n")
		self.codex()
		with mock.patch('collector.backfill.getproxies', return_value={'http': 'http://proxy.invalid'}), \
				mock.patch('collector.backfill.proxy_bypass', return_value=False):
			self.run_daily()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_company_counts_only_report_preserves_existing_report(self):
		self.sources(cls="company")
		self.codex()
		self.run_daily()
		batch, _ = self.batch()
		path = Path(batch["report"])
		path.write_text("기존 보고서 보존")
		store = Store(self.hub / "ledger.db")
		try:
			backfill_output.report(store, batch["id"], config.load_sources(), counts_only=True)
		finally:
			store.close()
		self.assertEqual(path.read_text(), "기존 보고서 보존")

	def test_unavailable_sources_do_not_report_a_false_remaining_zero(self):
		with mock.patch.object(backfill_daily, "process", side_effect=ValueError("비공개")):
			self.run_daily()
		message = self.send.call_args.args[0]
		self.assertIn("남은 대상 미확인", message)
		self.assertNotIn("비공개", message)

	def test_company_approval_count_includes_all_open_scopes(self):
		self.sources(cls="company")
		(self.hub / "plans.toml").write_text("[[plans]]\nid='p02'\ntool='ollama'\nowner='company'\n"
			"allow=['company']\nmodel='synthetic'\n[routing]\ncompany=['p02']\n")
		self.codex()
		self.run_daily()
		store = Store(self.hub / "ledger.db")
		try:
			scope = store.create_scope("p01", "sessions", "2026-07-22", self.clock.isoformat(), "synthetic",
				{"tasks": [], "agy_count": 0})
			task = {"key": "synthetic-other", "hash": "synthetic", "cls": "company"}
			bid = store.create_batch(scope, [task], self.hub / "second-report.md")
			store.save(bid, task, "prepared")
			store.state(bid, "prepared")
			message = backfill_daily.summary(store, config.load_sources(), self.clock.date().isoformat())
		finally:
			store.close()
		self.assertIn("회사 승인 대기 2묶음", message)

	def test_outside_window_does_not_run(self):
		self.clock = self.clock.replace(hour=12)
		self.codex()
		self.run_daily()
		self.ai.assert_not_called()
		self.send.assert_not_called()
