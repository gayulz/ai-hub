"""PM 호출의 요금제 순서·부서 허용 범위·AGY 작업 폴더·실패 넘김을 검사한다."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency import ai
from agency.departments import Department
from orchestrator import providers

PLANS = '''[[plans]]
id = "first"
tool = "codex"
owner = "personal"
allow = ["personal"]
[[plans]]
id = "second"
tool = "agy"
owner = "personal"
allow = ["personal"]
[[plans]]
id = "third"
tool = "codex"
owner = "personal"
allow = ["personal"]
[[plans]]
id = "work"
tool = "codex"
owner = "personal"
allow = ["company"]
'''


def department(allow=("first", "second", "third", "work"), external=True):
	return Department("xyz", "합성 부서", "합성", ("ai-hub",), tuple(allow), external, "xyz", (), "", (), (),
		{"calls": 10, "minutes": 30})


class AgencyAiTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.repo = self.hub / "repo"
		self.repo.mkdir()

	def write(self, orchestration):
		(self.hub / "plans.toml").write_text(PLANS + "[orchestration]\n" + orchestration, encoding="utf-8")

	def test_order_prefers_pm_then_planner_then_implementer_and_filters(self):
		self.write('implementer = ["third"]\nplanner = ["second", "first"]\n')
		self.assertEqual([plan.id for plan in ai.pm_plans(department())], ["second", "first"])
		self.write('implementer = ["third"]\nplanner = ["second"]\npm = ["work", "third", "first"]\n')
		self.assertEqual([plan.id for plan in ai.pm_plans(department())], ["third", "first"])
		self.assertEqual([plan.id for plan in ai.pm_plans(department(allow=("first",)))], ["first"])
		self.write('implementer = ["third", "first"]\n')
		self.assertEqual([plan.id for plan in ai.pm_plans(department())], ["third", "first"])

	def test_external_ai_forbidden_department_is_refused(self):
		self.write('implementer = ["first"]\n')
		with self.assertRaises(ValueError):
			ai.pm_plans(department(allow=("first",), external=False))

	def test_ask_skips_failures_and_keeps_agy_out_of_repo(self):
		self.write('pm = ["second", "first", "third"]\n')
		seen = []

		def fake(plan, writer, prompt, cwd, schema, timeout):
			seen.append((plan.id, writer, Path(cwd)))
			if plan.id == "second":
				raise providers.ProviderError("exit", "second 종료 코드 3 (한도 소진)")
			return providers.Reply({"ok": True}, 1.5, "")

		with mock.patch.object(providers, "call", side_effect=fake):
			answer = ai.ask(department(), "질문", {"type": "object"}, repo=self.repo)
		self.assertEqual((answer.data, answer.plan_id, answer.seconds), ({"ok": True}, "first", 1.5))
		self.assertEqual([item[0] for item in seen], ["second", "first"])
		self.assertTrue(all(writer is False for _, writer, _ in seen))
		self.assertNotEqual(seen[0][2], self.repo)
		self.assertEqual(seen[1][2], self.repo)

	def test_ask_reports_only_cause_labels_when_all_fail(self):
		self.write('pm = ["first", "third"]\n')
		error = providers.ProviderError("exit", "first 종료 코드 1 (원인 미분류)")
		with mock.patch.object(providers, "call", side_effect=error):
			with self.assertRaises(RuntimeError) as caught:
				ai.ask(department(), "질문", {"type": "object"})
		self.assertIn("원인 미분류", str(caught.exception))

	def test_no_allowed_plan(self):
		self.write('pm = ["work"]\n')
		with self.assertRaises(RuntimeError):
			ai.ask(department(), "질문", {"type": "object"})


if __name__ == "__main__":
	unittest.main()
