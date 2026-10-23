"""비교의 제외 사유·장애·중단 복구·보안값 제거를 합성 자료로 검사한다."""

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest import mock

from collector import backfill, backfill_sessions, config, ollama, summarize, vault
from collector.backfill_common import packed, sha
from collector.backfill_compare_report import checks
from collector.backfill_store import C4_SCHEMA, Store
from collector.ledger import SCHEMA
from test_backfill_compare_support import CompareCase


class CompareSafetyTest(CompareCase):
	def test_successful_personal_study_and_career_routes(self):
		for cls in ("personal", "study", "career"):
			self.sources(cls=cls)
			self.codex(sid=cls)
			with self.subTest(cls=cls):
				self.assertEqual(self.compare(1), 0)
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (3, 3))

	def test_cli_default_model_is_identified_without_an_extra_call(self):
		self.codex()
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace("model='xyz-sub'\n", ""))
		self.assertEqual(self.compare(1), 0)
		self.assertEqual(self.batch()[1][0]["data"]["comparison"]["subscription"]["model"], "xyz-default")
		self.assertEqual(len(self.subscription_calls()), 1)

	def test_subscription_json_metadata(self):
		self.codex()
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace("model='xyz-sub'\n", "").replace("tool='codex'", "tool='agy'"))
		self.assertEqual(self.compare(1), 0)
		self.assertEqual(self.batch()[1][0]["data"]["comparison"]["subscription"]["model"], "xyz-default")
		self.assertIn("구독:", self.batch()[1][0]["data"]["comparison"]["subscription"]["summary"])
		self.assertEqual(self.subscription_calls()[0]["prompt"], self.server.posts[0][1]["prompt"])

	def test_agy_comparison_passes_the_configured_model(self):
		self.codex()
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace("tool='codex'", "tool='agy'"))
		self.assertEqual(self.compare(1), 0)
		self.assertEqual(self.subscription_calls()[0]["model"], "xyz-sub")
		self.assertEqual(self.subscription_calls()[0]["prompt"], self.server.posts[0][1]["prompt"])

	def test_report_symlink_is_rejected_without_touching_its_target(self):
		self.configure(limit=1)
		self.codex(sid="s01")
		self.codex(sid="s02")
		self.assertEqual(self.compare(2), 1)
		batch, _ = self.batch()
		path = Path(batch["report"])
		target = self.root / "human.md"
		target.write_text("보존할 합성 메모")
		path.unlink()
		path.symlink_to(target)
		before = (len(self.server.posts), len(self.subscription_calls()))
		self.clock += timedelta(days=1)
		self.assertEqual(self.command("--resume", batch["id"]), 2)
		self.assertEqual(target.read_text(), "보존할 합성 메모")
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), before)

	def test_dry_run_with_c1_only_ledger_does_not_migrate_or_write(self):
		conn = sqlite3.connect(self.hub / "ledger.db")
		conn.executescript(SCHEMA)
		conn.close()
		self.codex()
		before = self.snapshot()
		self.assertEqual(self.command("--compare", "p01", "--count", "1", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(self.server.gets, [])

	def test_existing_cli_policy_fingerprint_is_unchanged(self):
		self.plans()
		sources, plans = config.load_sources(), config.load_plans()
		repo = backfill.project(sources, "p01")
		settings = asdict(plans)
		for plan in settings["plans"].values():
			plan.pop("url")
			plan.pop("timeout")
		expected = sha(packed({"project": asdict(repo), "rules": [asdict(r) for r in sources.rules],
			"vault": str(sources.vault), "plans": settings}))
		self.assertEqual(backfill.policy(sources, plans, repo), expected)

	def test_invalid_count_and_unrelated_count_option_are_rejected(self):
		for args in (("--compare", "p01", "--count", "0"), ("--compare", "p01", "--count", "21"),
				("--project", "p01", "--kind", "sessions", "--count", "1")):
			with self.subTest(args=args), self.assertRaises(SystemExit):
				self.command(*args)

	def test_insufficient_candidates_exclude_recorded_reusable_company_and_long_inputs(self):
		self.sources(extra=f"\n[[classify]]\npath={json.dumps(str(self.repo / 'xyz'))}\nclass='company'\n")
		self.codex(sid="fresh")
		self.codex(sid="recorded")
		self.codex(sid="reusable")
		self.codex(sid="cached")
		self.codex(sid="company", cwd=str(self.repo / "xyz"))
		self.codex(sid="unfinished", answer=None)
		self.codex(sid="long", answer="아주 긴 합성 대화 " * 1700)
		store = Store(self.hub / "ledger.db")
		self.addCleanup(store.close)
		store.put("codex:recorded", "codex", "recorded", "personal", "합성 해시", "recorded")
		folder = self.root / "summaries"
		folder.mkdir()
		(folder / "reusable.json").write_text(json.dumps({"session_id": "reusable", "cwd": str(self.repo),
			"updated_at": "2026-08-02T01:00:00Z", "summary": "재사용 가능한 합성 요약"}), encoding="utf-8")
		data = backfill_sessions.inventory(backfill.project(config.load_sources(), "p01"), config.load_sources(),
			backfill.DEFAULT_SINCE, self.clock)
		task = next(t for t in data["tasks"] if t["sid"] == "cached")
		scope = store.create_scope("p01", "sessions", "2026-07-22", self.clock.isoformat(), "합성 정책", {"tasks": [task]})
		bid = store.create_batch(scope, [task], self.hub / "cache.md")
		store.save(bid, dict(task, cls="personal", result={"method": "ai"}), "prepared")
		self.assertEqual(self.compare(), 1)
		batch, items = self.batch()
		self.assertEqual([i["key"] for i in items], ["codex:fresh"])
		report = Path(batch["report"]).read_text()
		for reason in ("적격 부족 19건", "이미 기록된 대화", "기존 요약 재사용 대상", "기존 준비 결과 재사용 대상",
				"분할 입력", "완료 답이 없음", "분류가 미확인 또는 충돌", "비교 미완료"):
			self.assertIn(reason, report)
		self.assertNotEqual(batch["state"], "compared")
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (1, 1))

	def test_no_candidates_means_no_generation_and_an_incomplete_report(self):
		self.assertEqual(self.compare(), 1)
		batch, items = self.batch()
		self.assertEqual(items, [])
		self.assertIn("적격 부족 20건", Path(batch["report"]).read_text())
		self.assertEqual(self.server.posts, [])
		self.assertEqual(self.subscription_calls(), [])

	def test_wrong_class_stale_input_and_reference_only_cache_need_fresh_refinement(self):
		for i in range(3):
			self.codex(sid=f"s{i}")
		sources = config.load_sources()
		data = backfill_sessions.inventory(backfill.project(sources, "p01"), sources, backfill.DEFAULT_SINCE, self.clock)
		store = Store(self.hub / "ledger.db")
		self.addCleanup(store.close)
		scope = store.create_scope("p01", "sessions", "2026-07-22", self.clock.isoformat(), "합성 정책", data)
		bid = store.create_batch(scope, data["tasks"], self.hub / "cache.md")
		for task, change in zip(data["tasks"], ({"cls": "company"}, {"material": "이전 입력"}, {"result": {"method": "existing"}})):
			store.save(bid, dict(task, cls="personal", result={"method": "ai"}) | change, "prepared")
		self.assertEqual(self.compare(3), 0)
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (3, 3))

	def test_ollama_down_or_missing_model_never_calls_subscription(self):
		self.codex()
		self.server.models = []
		self.assertEqual(self.compare(1), 1)
		self.assertIn("ollama pull", self.stdout.getvalue())
		batch, _ = self.batch()
		self.server.models = ["xyz"]
		self.stop_server()
		self.assertEqual(self.command("--resume", batch["id"]), 1)
		self.assertIn("ollama serve", self.stdout.getvalue())
		self.assertIn("설치", self.stdout.getvalue())
		self.assertEqual(self.server.posts, [])
		self.assertEqual(self.subscription_calls(), [])

	def test_missing_model_can_be_prepared_then_resumed(self):
		self.codex()
		self.server.models = []
		self.assertEqual(self.compare(1), 1)
		batch, _ = self.batch()
		self.server.models = ["xyz:latest"]
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (1, 1))

	def test_empty_body_or_generation_error_does_not_call_subscription(self):
		self.codex()
		self.server.answer = ""
		self.assertEqual(self.compare(1), 1)
		self.assertEqual(self.subscription_calls(), [])
		self.assertEqual(self.batch()[1][0]["state"], "held")

	def test_configured_timeout_is_passed_to_urllib_and_failure_stops_subscription(self):
		self.codex()
		self.configure(timeout=0.001)
		self.server.delay = 0.02
		self.assertEqual(self.compare(1), 1)
		self.assertEqual(self.subscription_calls(), [])
		self.assertIn("Ollama", self.stdout.getvalue())

	def test_success_saved_before_interruption_is_not_called_again(self):
		self.codex()
		original = Store.save
		interrupted = False
		def save(store, bid, task, state):
			nonlocal interrupted
			if state == "processing" and not interrupted:
				interrupted = True
				raise KeyboardInterrupt()
			return original(store, bid, task, state)
		with mock.patch.object(Store, "save", new=save), self.assertRaises(KeyboardInterrupt):
			self.compare(1)
		batch, _ = self.batch()
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (1, 1))

	def test_unknown_local_result_stays_reserved_without_automatic_retry(self):
		self.codex()
		with mock.patch.object(Store, "finish_call", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
			self.compare(1)
		batch, _ = self.batch()
		self.assertEqual(self.command("--resume", batch["id"]), 1)
		self.assertEqual(len(self.server.posts), 1)
		self.assertEqual(self.subscription_calls(), [])
		self.assertIn("호출 결과 불명", self.batch()[1][0]["data"]["hold_reason"])

	def test_source_change_before_resume_is_held_without_new_calls(self):
		self.configure(limit=1)
		self.codex(sid="s01")
		self.codex(sid="s02")
		self.assertEqual(self.compare(2), 1)
		batch, _ = self.batch()
		self.codex(sid="s02", answer="변경된 합성 답")
		before = (len(self.server.posts), len(self.subscription_calls()))
		self.clock += timedelta(days=1)
		self.assertEqual(self.command("--resume", batch["id"]), 1)
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), before)
		self.assertIn("내용이 변경됨", self.batch()[1][-1]["data"]["hold_reason"])

	def test_detects_and_removes_generated_secrets_and_preserves_human_judgments(self):
		self.configure(limit=1)
		self.codex(sid="s01", question="2026-08-01까지 18건 완료할까요?")
		self.codex(sid="s02")
		self.server.answer = "- 2026-08-01까지 18건 검토. password=sample-local"
		with mock.patch.dict(os.environ, {"ACME_ANSWER": "- 2026-08-01까지 18건 검토. token=sample-sub"}):
			self.assertEqual(self.compare(2), 1)
		batch, items = self.batch()
		checks = items[0]["data"]["comparison"]["local"]["checks"]
		self.assertTrue(checks["secret_patterns_detected"])
		self.assertFalse(checks["secret_patterns_remaining"])
		self.assertEqual(checks["dates"]["ratio"], 1)
		self.assertEqual(checks["numbers"]["ratio"], 1)
		path = Path(batch["report"])
		path.write_text(path.read_text().replace("| 사실 왜곡 |  |  |", "| 사실 왜곡 | 확인 필요 | 없음 |", 1))
		self.clock += timedelta(days=1)
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertIn("| 사실 왜곡 | 확인 필요 | 없음 |", path.read_text())
		for result in path.parent.iterdir():
			self.assertNotIn("sample-local", result.read_text())
			self.assertNotIn("sample-sub", result.read_text())
		store = Store(self.hub / "ledger.db", readonly=True)
		self.addCleanup(store.close)
		self.assertNotIn("sample-local", packed(store.rows("c4_calls")))
		self.assertNotIn("sample-sub", packed(store.rows("c4_calls")))

	def test_c1_plan_budget_is_also_respected_by_compare(self):
		self.configure(per_plan=1, routing=["p01", "p02"])
		store = Store(self.hub / "ledger.db")
		store.bump_call("p01", self.clock.date().isoformat())
		store.close()
		self.codex()
		self.assertEqual(self.compare(1), 1)
		self.assertEqual(self.subscription_calls(), [])

	def test_existing_ledger_migration_keeps_subscription_usage(self):
		path = self.hub / "ledger.db"
		conn = sqlite3.connect(path)
		conn.executescript(SCHEMA + C4_SCHEMA.replace(", local integer not null default 0", ""))
		conn.execute("insert into c4_calls(batch,key,part,plan,day,state) values (?,?,?,?,?,?)",
			("old", "codex:old", 0, "p01", self.clock.date().isoformat(), "failed"))
		conn.commit()
		conn.close()
		self.configure(limit=1)
		self.codex()
		self.assertEqual(self.compare(1), 1)
		self.assertEqual(len(self.server.posts), 1)
		self.assertEqual(self.subscription_calls(), [])

	def test_tcp_http_server_transport(self):
		if not self.tcp:
			self.skipTest("샌드박스가 임시 TCP 포트 바인딩을 금지함. 다른 비교 테스트는 HTTP 핸들러를 메모리로 연결해 검사")
		self.codex()
		self.assertEqual(self.compare(1), 0)
		self.assertIn("/api/tags", self.server.gets)
		self.assertEqual(self.server.posts[0][0], "/api/generate")


class OllamaConfigTest(unittest.TestCase):
	def test_retention_requires_whole_numbers_and_dates(self):
		prompt = summarize.PROMPT_HEAD + "부분 1/1\n18건을 2026-08-01까지 확인" + summarize.PROMPT_TAIL
		with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AI_HUB_HOME": tmp}):
			self.assertEqual(checks(prompt, "180건을 2026-08-02까지 확인")["numbers"]["ratio"], 0)
			self.assertEqual(checks(prompt, "18건을 2026-08-01까지 확인")["dates"]["ratio"], 1)
			self.assertEqual(checks(prompt, "18건을 2026-08-010까지 확인")["dates"]["ratio"], 0)

	def test_type_defaults_and_timeout_validation(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "plans.toml"
			base = "[[plans]]\nid='p01'\ntype='ollama'\nowner='personal'\nallow=['study']\nmodel='xyz'\n"
			path.write_text(base)
			plan = config.load_plans(path).plans["p01"]
			self.assertEqual((plan.tool, plan.url, plan.timeout), ("ollama", "http://localhost:11434", 300))
			for bad in ("0", "-1", "true", "'xyz'", "nan", "inf"):
				path.write_text(base + f"timeout={bad}\n")
				with self.subTest(timeout=bad), self.assertRaises(config.ConfigError):
					config.load_plans(path)
			for line in ("url='file:///acme'", "url='http://[invalid'", "url=1", "url='http://localhost:0'", "tool='codex'"):
				path.write_text(base + line + "\n")
				with self.subTest(line=line), self.assertRaises(config.ConfigError):
					config.load_plans(path)
			path.write_text(base.replace("model='xyz'", "model=''"))
			with self.assertRaises(config.ConfigError):
				config.load_plans(path)

	def test_ollama_timeout_and_actual_model_metadata(self):
		plan = config.Plan("p01", "ollama", "", "personal", ("personal",), "xyz", None, timeout=12)
		response = mock.MagicMock()
		response.__enter__.return_value.read.return_value = json.dumps({
			"response": "- 합성 본문", "thinking": "생각 과정 제외", "model": "xyz:latest"}).encode()
		metadata = {}
		with mock.patch.object(ollama, "urlopen", return_value=response) as request:
			self.assertEqual(summarize.call(plan, "합성 질문", metadata=metadata), "- 합성 본문")
		self.assertEqual(request.call_args.kwargs["timeout"], 12)
		self.assertEqual(metadata, {"model": "xyz:latest"})

	def test_unidentified_subscription_model_is_not_reported_as_known(self):
		plan = config.Plan("p01", "codex", "", "personal", ("personal",), None, None)
		with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AI_HUB_HOME": tmp}):
			result = mock.Mock(returncode=0, stdout="- 합성 결과", stderr="")
			with mock.patch.object(summarize.subprocess, "run", return_value=result), self.assertRaises(summarize.SummarizeError):
				summarize.call(plan, "합성 질문", metadata={})
