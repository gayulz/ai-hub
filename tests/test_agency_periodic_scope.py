"""사용자가 정한 부분 보고 범위와 기존 실패 검수를 함께 보존한다."""
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_agency_periodic_recovery as fixtures
from agency import acceptance, ai, delivery, document_review, private_provider, project, runner
from agency.__main__ import main as agency_main
from orchestrator import providers
from office import agency as office_agency


class PartialScopeTest(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.RecoveryTest(methodName="runTest")
		self.fixture.setUp()
		self.addCleanup(self.fixture.doCleanups)
		f = self.fixture
		self.assertEqual(f.replan(), 0)
		passed = providers.Reply({"verdict": "pass", "summary": "합성 보고", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=passed):
			self.assertEqual(runner.run(f.identity)["status"], "검수")
		failed = ai.Answer({"criteria": [{"criterion": f.criterion, "verdict": "fail", "evidence": "전체 미실행"}],
			"summary": "전체 범위 실패"}, "reviewer", .1)
		with mock.patch.object(document_review.ai, "ask", return_value=failed):
			self.assertEqual(acceptance.review(f.identity)["status"], "막힘")
		self.before = copy.deepcopy(project.load(f.identity))
		self.old_report = (f.folder / "report.md").read_bytes()
		self.scope = {"goal": "합성 부분 시험과 미확인 사항을 보고한다",
			"criteria": ["부분 시험과 전체 미실행 사유를 보고한다", "직접 의존성과 작은 오류 미점검을 보고한다"]}

	def replan_scope(self):
		option = {"project": self.fixture.identity, "scope": self.scope}
		with mock.patch.dict(os.environ, {"AI_HUB_PERIODIC_REPLAN": json.dumps(option)}), \
				redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
			return agency_main(["plan", self.fixture.identity])

	def test_preserves_failure_and_review_uses_only_approved_partial_scope(self):
		f = self.fixture
		self.assertEqual(self.replan_scope(), 0)
		row = project.load(f.identity)
		self.assertEqual(row["status"], "검수")
		self.assertEqual(row["spec"], self.before["spec"])
		self.assertEqual(row["spec_hash"], self.before["spec_hash"])
		self.assertEqual(row["tasks"], self.before["tasks"])
		self.assertEqual(row["periodic_final_reviews"], self.before["periodic_final_reviews"])
		archive = Path(row["periodic_scope"]["path"]).parent
		self.assertEqual((archive / "previous-report.md").read_bytes(), self.old_report)
		answer = ai.Answer({"criteria": [{"criterion": text, "verdict": "pass", "evidence": "부분 보고 확인"}
			for text in self.scope["criteria"]], "summary": "부분 범위만 충족"}, "reviewer", .1)
		with mock.patch.object(document_review.ai, "ask", return_value=answer) as reviewer:
			self.assertEqual(acceptance.review(f.identity)["status"], "납품 대기")
		prompt = reviewer.call_args.args[1]
		self.assertIn(self.scope["goal"], prompt)
		self.assertIn("기존 확정", prompt)
		self.assertEqual((archive / "previous-report.md").read_bytes(), self.old_report)
		bundle = delivery.load_bundle(f.identity)
		self.assertEqual([item["criterion"] for item in bundle["criteria"]], self.scope["criteria"])
		self.assertEqual(bundle["scope_sha256"], row["periodic_scope"]["sha256"])
		view = office_agency.detail(f.identity, "delivery")
		self.assertEqual([item["criterion"] for item in view["criteria"]], self.scope["criteria"])
		self.assertEqual(view["actions"], ["document"])
		self.assertEqual(project.load(f.identity)["periodic_final_reviews"][0], self.before["periodic_final_reviews"][0])
		self.assertEqual(len(project.load(f.identity)["periodic_final_reviews"]), 2)
		(archive / "previous-report.md").chmod(0o600)
		(archive / "previous-report.md").write_bytes(b"changed")
		with self.assertRaises(ValueError):
			delivery.load_bundle(f.identity)

	def test_interrupted_report_reuses_new_review_without_losing_old_report(self):
		f = self.fixture
		self.assertEqual(self.replan_scope(), 0)
		answer = ai.Answer({"criteria": [{"criterion": text, "verdict": "pass", "evidence": "부분 보고 확인"}
			for text in self.scope["criteria"]], "summary": "부분 범위만 충족"}, "reviewer", .1)
		with mock.patch.object(document_review.ai, "ask", return_value=answer) as reviewer:
			with mock.patch.object(document_review, "private_write", side_effect=OSError("합성 중단")):
				with self.assertRaises(OSError):
					acceptance.review(f.identity)
			self.assertEqual((f.folder / "report.md").read_bytes(), self.old_report)
			self.assertEqual(acceptance.review(f.identity)["status"], "납품 대기")
			reviewer.assert_called_once()

	def test_rejects_scope_without_failed_review_and_repeated_choice(self):
		self.assertEqual(self.replan_scope(), 0)
		before = project.load(self.fixture.identity)
		self.assertEqual(self.replan_scope(), 1)
		self.assertEqual(project.load(self.fixture.identity), before)


if __name__ == "__main__":
	unittest.main()
