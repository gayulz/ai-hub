"""소급 준비·재개·승인 반영과 무변경 미리보기."""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from collector import backfill, backfill_output, config, pipeline, summarize, vault
from collector.backfill_store import Store
from collector.ledger import Ledger
from test_backfill_support import BackfillCase


class BackfillFlowTest(BackfillCase):
	def test_prepare_resume_apply_uses_one_call_and_preserves_human_notes(self):
		self.codex()
		path = vault.daily_path(self.vault, "personal", date(2026, 8, 1))
		path.write_text("사람이 적은 메모\n", encoding="utf-8")
		before = self.snapshot(self.vault)
		self.assertEqual(self.prepare(), 0)
		self.assertEqual(self.snapshot(self.vault), before)
		batch, tasks = self.batch()
		self.assertEqual(tasks[0]["state"], "prepared")
		self.assertTrue(tasks[0]["data"]["result"]["original_missing"])
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.command("--apply", batch["id"]), 0)
		self.assertEqual(self.ai.call_count, 1)
		self.assertIn("사람이 적은 메모", path.read_text())
		self.assertIn("<!-- ai-hub:codex:s01 -->", path.read_text())
		written = self.snapshot(self.vault)
		self.assertEqual(self.command("--apply", batch["id"]), 0)
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual(self.snapshot(self.vault), written)
		self.assertIn("변경 전", Path(batch["report"]).read_text())
		self.assertIn("반영 후 블록 대조 불일치: 0건", Path(batch["report"]).read_text())

	def test_batch_size_and_fixed_inventory_dont_take_new_sessions(self):
		self.plans(size=2)
		for i in range(3):
			self.codex(sid=f"s{i:02d}")
		self.prepare()
		batch, tasks = self.batch()
		self.assertEqual(len(tasks), 2)
		self.codex(sid="new")
		self.command("--apply", batch["id"])
		self.prepare()
		next_batch, tasks = self.batch()
		self.assertNotEqual(next_batch["id"], batch["id"])
		self.assertEqual([t["key"] for t in tasks], ["codex:s02"])
		self.assertEqual(self.ai.call_count, 3)

	def test_daily_limit_holds_remaining_session_until_next_day(self):
		self.plans(limit=1, size=2)
		self.codex(sid="s01")
		self.codex(sid="s02")
		self.prepare()
		batch, tasks = self.batch()
		self.assertEqual([t["state"] for t in tasks], ["prepared", "held"])
		self.assertEqual(self.ai.call_count, 1)
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 1)
		self.clock += timedelta(days=1)
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 2)
		self.assertTrue(all(t["state"] == "prepared" for t in self.batch()[1]))

	def test_daily_budget_is_shared_by_two_approved_projects(self):
		self.plans(limit=1)
		other = self.root / "xyz"
		other.mkdir()
		path = self.hub / "sources.toml"
		path.write_text(path.read_text() + f"\n[[classify]]\npath={json.dumps(str(other))}\nclass='personal'\nname='xyz'\n"
			f"[[repo]]\nid='p02'\npath={json.dumps(str(other))}\nclass='personal'\nname='xyz'\ndoc_dirs=[]\n")
		self.codex(sid="s01")
		self.codex(sid="s02", cwd=str(other))
		self.prepare()
		first, _ = self.batch()
		self.command("--apply", first["id"])
		self.command("--project", "p02", "--kind", "sessions")
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_interruption_after_success_resumes_without_duplicate_call(self):
		self.codex(sid="s01")
		self.codex(sid="s02")
		original = Store.save
		interrupted = False

		def save(store, bid, task, state):
			nonlocal interrupted
			if state == "prepared" and not interrupted:
				interrupted = True
				raise KeyboardInterrupt()
			return original(store, bid, task, state)

		with mock.patch.object(Store, "save", new=save), self.assertRaises(KeyboardInterrupt):
			self.prepare()
		batch, _ = self.batch()
		self.assertEqual(self.ai.call_count, 1)
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 2)
		self.assertTrue(all(t["state"] == "prepared" for t in self.batch()[1]))

	def test_unknown_remote_result_is_held_and_not_recalled(self):
		self.codex()
		self.ai.side_effect = KeyboardInterrupt()
		with self.assertRaises(KeyboardInterrupt):
			self.prepare()
		batch, _ = self.batch()
		self.ai.side_effect = None
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 1)
		self.assertIn("호출 결과 불명", self.batch()[1][0]["data"]["hold_reason"])

	def test_company_without_authorized_route_stays_held(self):
		self.sources(cls="company")
		self.plans(company=False)
		self.codex()
		self.prepare()
		batch, items = self.batch()
		self.assertEqual(items[0]["state"], "held")
		self.ai.assert_not_called()
		self.command("--apply", batch["id"])
		self.assertEqual(self.snapshot(self.vault), {})
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertIsNone(store.get("codex:s01"))
		store.close()

	def test_company_failure_does_not_fall_back_outside_company_route(self):
		self.sources(cls="company")
		path = self.hub / "plans.toml"
		path.write_text("[[plans]]\nid='p01'\ntool='codex'\nowner='personal'\nallow=['company']\n"
			"[[plans]]\nid='p02'\ntool='claude'\nowner='personal'\nallow=['company','personal']\n"
			"[routing]\ncompany=['p01']\npersonal=['p02']\n", encoding="utf-8")
		self.ai.side_effect = summarize.SummarizeError("합성 실패")
		self.codex()
		self.prepare()
		self.assertEqual([call.args[0].id for call in self.ai.call_args_list], ["p01"])
		self.assertEqual(self.batch()[1][0]["state"], "held")
		self.command("--resume", self.batch()[0]["id"])
		self.assertEqual(self.ai.call_count, 1)

	def test_apply_detects_human_block_conflict_and_missing_block_is_restorable(self):
		self.codex()
		self.prepare()
		batch, _ = self.batch()
		path = vault.daily_path(self.vault, "personal", date(2026, 8, 1))
		vault.upsert_block(path, "codex:s01", "사람이 수정", "acme", date(2026, 8, 1), "personal")
		self.assertEqual(self.command("--apply", batch["id"]), 2)
		self.assertIn("사람이 수정", path.read_text())
		path.unlink()
		self.command("--apply", batch["id"])
		path.unlink()
		self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.batch()[1][0]["state"], "prepared")
		self.command("--apply", batch["id"])
		self.assertTrue(path.exists())

	def test_source_change_before_apply_is_not_approved_automatically(self):
		self.codex()
		self.prepare()
		batch, _ = self.batch()
		self.codex(answer="새 답변")
		self.assertEqual(self.command("--apply", batch["id"]), 2)
		self.command("--resume", batch["id"])
		self.assertEqual(self.batch()[1][0]["state"], "held")
		self.assertEqual(self.ai.call_count, 1)
		self.prepare()
		fresh, items = self.batch()
		self.assertNotEqual(batch["id"], fresh["id"])
		self.assertEqual(items[0]["state"], "prepared")
		self.assertEqual(self.ai.call_count, 2)
		self.assertEqual(self.command("--apply", batch["id"]), 2)

	def test_policy_change_creates_new_review_with_cached_result(self):
		self.codex()
		self.prepare()
		old, _ = self.batch()
		self.plans(limit=21)
		self.assertEqual(self.command("--resume", old["id"]), 2)
		self.assertEqual(self.prepare(), 0)
		fresh, items = self.batch()
		self.assertNotEqual(old["id"], fresh["id"])
		self.assertEqual(items[0]["data"]["result"]["method"], "cached-ai")
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.command("--apply", fresh["id"]), 0)

	def test_partial_chunks_are_reused_after_policy_change(self):
		self.plans(limit=1)
		self.codex(question="합성 질문 " * 1500)
		self.prepare()
		old, _ = self.batch()
		self.assertEqual(self.ai.call_count, 1)
		self.plans(limit=20)
		self.prepare()
		fresh, items = self.batch()
		self.assertNotEqual(old["id"], fresh["id"])
		self.assertEqual(items[0]["state"], "prepared")
		self.assertEqual(self.ai.call_count, items[0]["data"]["parts"])

	def test_unknown_call_stays_held_across_policy_change(self):
		self.codex()
		self.ai.side_effect = KeyboardInterrupt()
		with self.assertRaises(KeyboardInterrupt):
			self.prepare()
		self.ai.side_effect = None
		self.plans(limit=21)
		self.prepare()
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_resume_then_new_review_for_human_block_change_reuses_ai(self):
		self.codex()
		self.prepare()
		old, _ = self.batch()
		path = vault.daily_path(self.vault, "personal", date(2026, 8, 1))
		vault.upsert_block(path, "codex:s01", "사람이 바꾼 블록", "acme", date(2026, 8, 1), "personal")
		self.command("--resume", old["id"])
		self.assertEqual(self.batch()[1][0]["state"], "held")
		self.prepare()
		fresh, items = self.batch()
		self.assertNotEqual(old["id"], fresh["id"])
		self.assertEqual(items[0]["data"]["result"]["before"], "사람이 바꾼 블록")
		self.assertEqual(self.ai.call_count, 1)
		self.assertIn("사람이 바꾼 블록", path.read_text())

	def test_dry_run_never_creates_or_changes_ledger_reports_or_vault(self):
		self.codex()
		before = self.snapshot()
		with mock.patch.object(pipeline, "open_ledger", side_effect=AssertionError("쓰기 연결")):
			self.assertEqual(self.prepare("sessions", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.assertFalse((self.hub / "ledger.db").exists())
		self.ai.assert_not_called()
		ledger = Ledger(self.hub / "ledger.db")
		ledger.start_run()
		ledger.close()
		before = self.snapshot()
		self.assertEqual(self.prepare("sessions", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.prepare()
		batch, _ = self.batch()
		before = self.snapshot()
		self.command("--resume", batch["id"], "--dry-run")
		self.command("--apply", batch["id"], "--dry-run")
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(self.ai.call_count, 1)

	def test_repos_dry_run_does_not_create_ledger_or_reports(self):
		(self.repo / "README.md").write_text("# 합성 문서", encoding="utf-8")
		self.sources(doc_dirs=(".",))
		before = self.snapshot()
		self.assertEqual(self.prepare("repos", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.ai.assert_not_called()

	def test_c1_recorded_session_is_referenced_without_ai(self):
		self.codex()
		sources = config.load_sources()
		repo = sources.repos[0]
		sess = backfill.sessions.load(backfill.sessions.inventory(repo, sources, date(2026, 7, 22), self.clock)["tasks"][0])
		ledger = Ledger(self.hub / "ledger.db")
		ledger.put(sess.key, sess.tool, sess.sid, "personal", pipeline.digest(sess), "recorded")
		ledger.close()
		path = vault.daily_path(self.vault, "personal", sess.started.date())
		vault.upsert_block(path, sess.key, "기존 정제 기록", "acme", sess.started.date(), "personal")
		self.prepare()
		self.ai.assert_not_called()
		self.assertEqual(self.batch()[1][0]["state"], "existing")

	def test_c1_does_not_summarize_a_session_waiting_for_c4_approval(self):
		self.codex()
		self.prepare()
		_, items = self.batch()
		sess = backfill.sessions.load(items[0]["data"])
		ledger = Ledger(self.hub / "ledger.db")
		runner = pipeline.Pipeline(config.load_sources(), config.load_plans(), ledger, False)
		runner.handle_session(sess)
		self.assertEqual(runner.counts["claimed"], 1)
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.snapshot(self.vault), {})
		ledger.close()
