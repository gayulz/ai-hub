"""명시적 보존 옵션이 반영 검사와 실행 원장을 유지하는지 확인한다."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import __main__ as cli
from orchestrator.record import Run
import test_orchestrator_branch_apply as branch_fixtures
import test_orchestrator as orchestrator_fixtures

BRANCH = branch_fixtures.BRANCH


class RetentionTest(unittest.TestCase):
	def setUp(self):
		self.case = branch_fixtures.BranchApplyTest(methodName="runTest")
		self.case.setUp()
		self.addCleanup(self.case.doCleanups)

	def test_project_branch_apply_preserves_worktree_and_writes_run(self):
		case = self.case
		run = case.approved()
		worktree = Path(run.worktree)
		before = case.snapshot()
		with mock.patch.object(cli, "finish_outputs") as output, contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["apply", run.run_id, "--onto", BRANCH, "--keep-worktree"]), 0)
		self.assertEqual(Run.load(run.run_id).status, "완료")
		self.assertTrue(worktree.is_dir())
		self.assertTrue(case.git("show-ref", "--verify", "refs/heads/" + run.branch))
		self.assertEqual(case.git("rev-parse", BRANCH + "^{tree}"), case.git("rev-parse", run.branch + "^{tree}"))
		self.assertEqual(case.snapshot(), before)
		output.assert_called_once()

	def test_preservation_does_not_bypass_moved_branch_refusal(self):
		case = self.case
		run = case.approved()
		case.git("update-ref", "refs/heads/" + BRANCH, case.moved_commit(), case.base)
		with contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(cli.main(["apply", run.run_id, "--onto", BRANCH, "--keep-worktree"]), 1)
		case.assert_pending(run)

	def test_main_branch_apply_preserves_worktree_and_records_completion(self):
		case = orchestrator_fixtures.OrchestratorTest(methodName="runTest")
		case.setUp()
		self.addCleanup(case.doCleanups)
		self.addCleanup(case.tearDown)
		with mock.patch("orchestrator.test_isolation.requires", return_value=False):
			run = case.approved({"src/a.py": "VALUE = 2\n"})
		with mock.patch.object(cli, "finish_outputs") as output, contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["apply", run.run_id, "--keep-worktree"]), 0)
		self.assertEqual(Run.load(run.run_id).status, "완료")
		self.assertEqual((case.repo / "src/a.py").read_text(), "VALUE = 2\n")
		self.assertTrue(Path(run.worktree).is_dir())
		self.assertTrue(case.repo.joinpath(".git/refs/heads", run.branch).exists())
		output.assert_called_once()


if __name__ == "__main__":
	unittest.main()
