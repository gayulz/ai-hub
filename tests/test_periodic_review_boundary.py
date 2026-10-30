"""합성 분석 실행으로 독립 판정과 기계 검증의 승인 경계를 검사한다."""
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from agency import acceptance, delivery, document_review, document_run, documents, planner, private_provider, project, runner
from orchestrator import draft, providers
from orchestrator.__main__ import read_request
from orchestrator.flow import Flow
from orchestrator.record import Run, new_run_id
import test_agency_planner as planner_fixtures
import test_agency_runner as runner_fixtures
class ReportReviewBoundaryTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.enterContext(mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.root)}))
		self.enterContext(mock.patch("orchestrator.targets.REPO", self.root))
	def review(self, verdict, *, machine_ok=True, findings=None, periodic=True):
		run = Run(new_run_id(), {"goal": "합성 보고", "done": ["근거 확인"], **({"report_mode": "periodic-v1"} if periodic else {})},
			str(self.root), kind="analysis", step="review",
			budget={"calls": 4, "seconds": 60, "fixes": 0})
		run.document = "# 합성 보고\n\n근거: 합성 자료\n미검증: 시험 미실행\n"
		run.checks = [{"name": "보고서 형식", "ok": machine_ok, "detail": "합성 검사"}]
		flow = Flow(run, None, {})
		answer = {"summary": "합성 독립 검토", "findings": findings or []}
		if verdict is not None:
			answer["verdict"] = verdict
		with mock.patch.object(flow, "ask", return_value=answer):
			flow.review()
		return Run.load(run.run_id)
	def test_other_flow_keeps_existing_verdict_rules(self):
		self.assertEqual(self.review("changes", periodic=False).status, "승인 필요")
	def test_changes_without_findings_never_approves(self):
		run = self.review("changes")
		self.assertEqual(run.status, "실패")
		self.assertFalse(run.approval_pending)
		self.assertIn("verdict", [item["id"] for item in run.open_findings])
	def test_missing_or_unknown_verdict_never_approves(self):
		for verdict in (None, "unverified", "PASS", "", True):
			with self.subTest(verdict=verdict):
				run = self.review(verdict)
				self.assertEqual(run.status, "실패")
				self.assertFalse(run.approval_pending)
	def test_explicit_pass_with_failed_machine_check_never_approves(self):
		run = self.review("pass", machine_ok=False)
		self.assertEqual(run.status, "실패")
		self.assertFalse(run.approval_pending)
		self.assertIn("checks", [item["id"] for item in run.open_findings])
	def test_explicit_pass_does_not_hide_blocking_findings(self):
		finding = {"id": "evidence", "severity": "major", "file": "", "line": 0,
			"problem": "근거 누락", "evidence": "합성 자료", "suggestion": "근거 보완"}
		run = self.review("pass", findings=[finding])
		self.assertEqual(run.status, "실패")
		self.assertEqual(run.open_findings, [finding])
	def test_explicit_pass_keeps_unverified_report_text_unchanged(self):
		run = self.review("pass")
		self.assertEqual(run.status, "승인 필요")
		self.assertTrue(run.approval_pending)
		self.assertIn("미검증: 시험 미실행", run.document)
	def test_old_blog_delivery_cannot_start_an_unisolated_merge_test(self):
		bundle = {"version": 1, "target": "other", "hash": "a" * 64}
		external = self.root / "external"
		external.mkdir()
		selected = SimpleNamespace(id="other", path=external)
		with mock.patch.object(delivery.project, "_folder", return_value=Path("/synthetic")), \
				mock.patch.object(delivery.targets, "target", return_value=selected), \
				mock.patch.object(delivery, "locked", return_value=nullcontext()), \
				mock.patch.object(delivery, "_waiting"), mock.patch.object(delivery, "load_bundle", return_value=bundle), \
				mock.patch.object(delivery, "_merge_test") as merge, \
				mock.patch.object(delivery.project, "transition") as transition, self.assertRaisesRegex(ValueError, "격리"):
			delivery.approve("p-20261028-111", "a" * 64)
		merge.assert_not_called(); transition.assert_not_called()
