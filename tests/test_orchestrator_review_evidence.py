"""합성 외부 검토가 고정된 diff 및 격리 시험에 연결되는지 확인한다."""
import hashlib
import json
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from orchestrator import __main__ as cli, checks, isolated_inputs, review_evidence
from orchestrator.flow import Flow
from orchestrator.record import Run
from tests import test_isolated_inputs as fixtures


class EvidenceTest(unittest.TestCase):
	def setUp(self):
		fixtures.InputsTest.setUp(self)
		(self.repo / "logic.py").write_text("VALUE = 2\n")
		checks.git(self.repo, "add", "--", "logic.py")
		checks.git(self.repo, "commit", "-qm", "합성 변경")
		self.tip = checks.git(self.repo, "rev-parse", "HEAD").strip()
		_, self.diff = isolated_inputs.review_material(isolated_inputs.selected(self.repo), self.base, self.tip)
		counts = {"ran": 1, "failures": 0, "errors": 0, "skipped": 0}
		self.run = Run("synthetic-run", {"goal": "합성", "owned_paths": ["logic.py"]}, str(self.repo),
			worktree=str(self.repo), base_commit=self.base, step="review", status="작업 중")
		self.run.handoff["reverify_commit"] = self.tip
		self.run.checks = [{"name": "허용 경로", "ok": True}, {"name": "테스트", "ok": True, "executed": True,
			"counts": counts, "isolation": {"status": "passed", "executed": True, "tests": counts}}]
		self.value = {"run_id": self.run.run_id, "base_commit": self.base, "commit": self.tip,
			"diff_sha256": hashlib.sha256(self.diff.encode()).hexdigest(), "source": "agy-text", "reviewer": "t14",
			"review": {"verdict": "pass", "summary": "합성 독립 검토 통과", "findings": []}}
		self.path = self.root / "review.json"

	def save(self, value=None):
		self.path.write_text(json.dumps(value or self.value), encoding="utf-8")
		self.path.chmod(0o600)
		return review_evidence.load(self.path)

	def test_resume_passes_evidence_to_normal_flow(self):
		self.save()
		with mock.patch.object(cli.Run, "load", return_value=self.run), \
				mock.patch.object(cli, "locked", return_value=nullcontext()), \
				mock.patch.object(self.run, "event"), mock.patch.object(self.run, "save"), \
				mock.patch.object(cli, "_execute_locked", return_value=0) as execute:
			self.assertEqual(cli.cmd_resume(self.run.run_id, ["--review-evidence", str(self.path)]), 0)
		self.assertEqual(execute.call_args.args[1][0], self.value)

	def test_verified_external_review_runs_normal_approval_path(self):
		worker = Flow(self.run, None, {}, external_review=self.save())
		with mock.patch.object(worker, "_review_answer") as call, mock.patch.object(worker, "_save_review") as save, \
				mock.patch.object(self.run, "event") as event, mock.patch.object(self.run, "finish") as finish:
			worker.review()
		call.assert_not_called()
		self.assertIn("AGY t14", save.call_args.args[0]["summary"])
		self.assertTrue(any(c.args[0] == "external_review" for c in event.call_args_list))
		finish.assert_called_once_with("승인 필요", "독립 검토와 기계 검증을 모두 통과했습니다")

	def test_mismatch_or_unexecuted_cannot_be_approved(self):
		for field in ("run_id", "base_commit", "commit", "diff_sha256"):
			value = dict(self.value)
			value[field] = "0" * (64 if field == "diff_sha256" else 40)
			with self.subTest(field=field), self.assertRaises(ValueError):
				review_evidence.validate(self.save(value), self.run, self.diff)
		loaded = self.save()
		self.run.checks[-1]["executed"] = False
		with self.assertRaises(ValueError):
			review_evidence.validate(loaded, self.run, self.diff)

	def test_changed_worktree_is_rejected_before_review(self):
		worker = Flow(self.run, None, {}, external_review=self.save())
		(self.repo / "logic.py").write_text("VALUE = 3\n")
		with self.assertRaises(ValueError):
			worker.review()

	def test_duplicate_keys_and_sensitive_summary_are_rejected(self):
		self.path.write_text('{"source":"agy-text","source":"agy-text"}', encoding="utf-8")
		self.path.chmod(0o600)
		with self.assertRaises(ValueError):
			review_evidence.load(self.path)
		value = dict(self.value)
		value["review"] = {"verdict": "pass", "summary": str(Path.home()) + "/private", "findings": []}
		with self.assertRaises(ValueError):
			self.save(value)

	def test_invalid_review_and_unsafe_file_mode_are_rejected(self):
		value = dict(self.value)
		value["review"] = {"verdict": "changes", "summary": "합성", "findings": []}
		with self.assertRaises(ValueError):
			self.save(value)
		self.save()
		self.path.chmod(0o644)
		with self.assertRaises(ValueError):
			review_evidence.load(self.path)
