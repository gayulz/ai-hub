"""C4 비교의 후보·보고서·상한·재개를 가짜 HTTP와 CLI로 확인한다."""

import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from collector.backfill_store import Store
from test_backfill_compare_support import CompareCase


class CompareFlowTest(CompareCase):
	def test_twenty_pairs_use_identical_redacted_inputs_and_private_reports(self):
		for i in range(21):
			self.codex(sid=f"s{i:02d}", question="2026-08-01까지 18건 확인. password=sample-input",
				answer="18건 결정. 남은 일 검토.\n```python\nprint('코드 제외')\n```")
		before = self.snapshot(self.vault)
		self.assertEqual(self.command("--compare", "p01"), 0)
		batch, items = self.batch()
		self.assertEqual(batch["state"], "compared")
		self.assertEqual(len(items), 20)
		self.assertEqual(len(self.server.posts), 20)
		calls = self.subscription_calls()
		self.assertEqual(len(calls), 20)
		for (endpoint, data), sub in zip(self.server.posts, calls):
			self.assertEqual(endpoint, "/api/generate")
			self.assertIs(data["stream"], False)
			self.assertEqual(data["model"], "xyz")
			self.assertEqual(data["prompt"], sub["prompt"])
			self.assertNotIn("sample-input", data["prompt"])
			self.assertNotIn("코드 제외", data["prompt"])
			self.assertNotIn("중간 진행", data["prompt"])
		self.assertEqual(self.snapshot(self.vault), before)
		folder = Path(batch["report"]).parent
		self.assertEqual(folder.name, "compare-" + batch["id"])
		self.assertEqual(folder.parent, self.hub / "backfill")
		self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
		self.assertEqual(folder.parent.stat().st_mode & 0o777, 0o700)
		report = Path(batch["report"]).read_text()
		self.assertIn("| 항목 | 로컬 | 구독 |", report)
		for word in ("비교 완료", "양쪽 완료 20건", "사실 왜곡", "결정·남은 일 보존", "코드 미포함", "한국어 가독성",
				"중대 왜곡·보안값 0건이고 나머지 충족이 20건 중 18건 이상이면 로컬 채택 후보", "자동으로 채택하지 않습니다"):
			self.assertIn(word, report)
		for path in folder.iterdir():
			self.assertEqual(path.stat().st_mode & 0o777, 0o600)
			self.assertNotIn("합성 비공개 생각 과정", path.read_text())
			self.assertNotIn("sample-input", path.read_text())
		self.assertEqual(len(list(folder.glob("*.json"))), 20)
		result = json.loads(next(folder.glob("*.json")).read_text())
		self.assertEqual(result["results"]["subscription"]["model"], "xyz-sub")
		self.assertEqual(result["results"]["local"]["checks"]["numbers"]["ratio"], 1.0)
		self.assertGreater(result["results"]["local"]["seconds"], 0)
		store = Store(self.hub / "ledger.db", readonly=True)
		self.addCleanup(store.close)
		self.assertEqual(sum(c["local"] for c in store.rows("c4_calls")), 20)
		self.assertEqual(len(store.rows("c4_calls")), 40)

	def test_limit_resumes_next_day_without_repeating_either_side(self):
		self.configure(limit=1)
		for i in range(2):
			self.codex(sid=f"s{i}")
		self.assertEqual(self.compare(2), 1)
		batch, items = self.batch()
		self.assertEqual([i["state"] for i in items], ["compared", "held"])
		count = len(self.server.posts)
		self.assertEqual(self.command("--resume", batch["id"]), 1)
		self.assertEqual(len(self.server.posts), count)
		self.assertEqual(len(self.subscription_calls()), 1)
		self.clock += timedelta(days=1)
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual(len(self.server.posts), 2)
		self.assertEqual(len(self.subscription_calls()), 2)
		self.assertIn("당일 C4 구독 누계 1회", Path(batch["report"]).read_text())
		self.stop_server()
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual(self.compare(2), 0)
		self.assertEqual(len(self.subscription_calls()), 2)

	def test_c4_route_override_uses_subscription_fallback_and_records_actual_plan(self):
		self.configure(override=["p02", "p03", "p01"])
		self.codex()
		with mock.patch.dict("os.environ", {"ACME_FAILURE_MODELS": '["xyz-next"]'}):
			self.assertEqual(self.compare(1), 0)
		pair = self.batch()[1][0]["data"]["comparison"]
		self.assertEqual([c["model"] for c in self.subscription_calls()], ["xyz-next", "xyz-sub"])
		self.assertEqual((pair["subscription"]["plan"], pair["subscription"]["model"]), ("p01", "xyz-sub"))
		self.assertEqual(len(self.server.posts), 1)

	def test_failed_subscription_consumes_limit_and_resumes_at_next_allowed_plan(self):
		self.configure(limit=1)
		self.codex()
		with mock.patch.dict("os.environ", {"ACME_FAILURE_MODELS": '["xyz-sub"]'}):
			self.assertEqual(self.compare(1), 1)
		batch, _ = self.batch()
		self.assertEqual(self.command("--resume", batch["id"]), 1)
		self.clock += timedelta(days=1)
		self.assertEqual(self.command("--resume", batch["id"]), 0)
		self.assertEqual([c["model"] for c in self.subscription_calls()], ["xyz-sub", "xyz-next"])
		self.assertEqual(len(self.server.posts), 1)

	def test_comparison_subscription_is_reused_by_normal_backfill(self):
		self.codex()
		self.assertEqual(self.compare(1), 0)
		self.assertEqual(self.prepare(), 0)
		batch, items = self.batch()
		self.assertEqual(items[0]["state"], "prepared")
		self.assertIn("구독:", items[0]["data"]["result"]["body"])
		self.assertNotIn("로컬:", items[0]["data"]["result"]["body"])
		self.assertEqual((len(self.server.posts), len(self.subscription_calls())), (1, 1))
		self.assertNotEqual(Path(batch["report"]).parent.name, "compare-" + batch["id"])

	def test_local_only_backfill_does_not_consume_subscription_limit(self):
		self.configure(limit=1, routing=["p02"])
		for i in range(3):
			self.codex(sid=f"s{i}")
		self.assertEqual(self.prepare(), 0)
		self.assertTrue(all(i["state"] == "prepared" for i in self.batch()[1]))
		self.assertEqual(len(self.server.posts), 3)
		self.assertEqual(self.subscription_calls(), [])
		self.assertIn("로컬 호출 3회", Path(self.batch()[0]["report"]).read_text())

	def test_dry_run_and_compare_apply_do_not_write_to_vault(self):
		self.codex()
		before = self.snapshot()
		self.assertEqual(self.command("--compare", "p01", "--count", "1", "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(self.server.gets, [])
		self.assertEqual(self.compare(1), 0)
		batch, _ = self.batch()
		before = self.snapshot()
		self.assertEqual(self.command("--resume", batch["id"], "--dry-run"), 0)
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(self.command("--apply", batch["id"]), 2)
		self.assertIn("비교 묶음", self.stderr.getvalue())
		self.assertEqual(self.snapshot(self.vault), {})

	def test_routing_and_allow_are_required_for_every_class(self):
		for cls in ("personal", "study", "career"):
			self.sources(cls=cls)
			for kwargs in ({"routing": ["p01"]}, {"local_allow": []}, {"override": ["p02"]}):
				self.configure(**kwargs)
				with self.subTest(cls=cls, config=kwargs):
					self.assertEqual(self.compare(1), 2)
		self.assertEqual(self.server.gets, [])
		self.assertEqual(self.subscription_calls(), [])

	def test_company_source_is_rejected_before_reading_candidates(self):
		self.sources(cls="company")
		self.codex()
		self.assertEqual(self.compare(1), 2)
		self.assertIn("회사 대화는 제외", self.stderr.getvalue())
		self.assertEqual(self.server.gets, [])
		self.assertEqual(self.subscription_calls(), [])
