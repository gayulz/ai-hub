"""합성 Git 실행의 재개와 정리가 같은 실행 잠금으로 보호되는지 검사한다."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from agency import project, runner
from orchestrator import __main__ as orchestration
from orchestrator.record import Run
from tests import test_agency_runner as fixtures
from tests import test_agency_runner_cleanup as cleanup


class ResumeSafetyTest(unittest.TestCase):
	setUp = fixtures.RunnerTest.setUp
	git = fixtures.RunnerTest.git
	snapshot = fixtures.RunnerTest.snapshot
	new = fixtures.RunnerTest.new
	start = fixtures.RunnerTest.start
	execution = cleanup.CleanupTest.execution
	quota = cleanup.CleanupTest.quota
	records = cleanup.CleanupTest.records
	ref_exists = cleanup.CleanupTest.ref_exists
	registered = cleanup.CleanupTest.registered
	assert_removed = cleanup.CleanupTest.assert_removed

	def test_other_process_lock_keeps_run_and_events_unchanged(self):
		row = self.new()
		execution = self.quota(row)
		lock_path = execution.folder / ".lock"
		script = ("import sys; from unittest import mock; "
			"from orchestrator import __main__ as cli; "
			"cli.Flow = mock.Mock(side_effect=AssertionError('실제 AI 호출 금지')); "
			"sys.exit(cli.main(['resume', sys.argv[1], '--calls', '3', '--minutes', '2']))")
		with orchestration.locked(execution):
			lock_path.write_bytes(b"lock remains")
			before = self.records(execution)
			result = subprocess.run([sys.executable, "-c", script, execution.run_id],
				env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=10)
			self.assertEqual(result.returncode, 1)
			self.assertIn("이미 실행 중", result.stderr)
			self.assertEqual(self.records(execution), before)
			self.assertEqual(lock_path.read_bytes(), b"lock remains")
			self.assertTrue(self.registered(execution))
			self.assertTrue(self.ref_exists(execution.branch))

	def test_resume_reloads_budget_and_holds_lock_through_flow_and_report(self):
		row = self.new()
		execution = self.quota(row)
		original = orchestration.locked
		@contextmanager
		def update_before_lock(current):
			latest = Run.load(execution.run_id)
			latest.budget["calls"] = 20
			latest.budget["seconds"] = 600
			latest.save()
			with original(current):
				yield
		def run_flow(current, *_):
			self.assertEqual(current.budget["calls"], 23)
			self.assertEqual(current.budget["seconds"], 720)
			self.assertEqual(current.status, "작업 중")
			with self.assertRaises(RuntimeError), original(current):
				self.fail("실행 중 잠금이 해제됐습니다")
			current.finish("승인 필요", "합성 검토 통과")
		def report(current):
			with self.assertRaises(RuntimeError), original(current):
				self.fail("보고 중 잠금이 해제됐습니다")
		with mock.patch.object(orchestration, "locked", side_effect=update_before_lock), \
				mock.patch.object(orchestration, "Flow") as flow, \
				mock.patch.object(orchestration, "finish_outputs", side_effect=report):
			flow.side_effect = lambda *args: mock.Mock(run_all=lambda: run_flow(*args))
			self.assertEqual(orchestration.cmd_resume(execution.run_id, ["--calls", "3", "--minutes", "2"]), 0)
		self.assertEqual(Run.load(execution.run_id).status, "승인 필요")
		self.assertIn('"event": "resume"', (execution.folder / "events.jsonl").read_text(encoding="utf-8"))

	def test_terminal_status_is_checked_again_after_lock(self):
		for status in ("승인 필요", "완료", "취소"):
			with self.subTest(status=status):
				row = self.new()
				execution = self.quota(row)
				original = orchestration.locked
				@contextmanager
				def finish_before_lock(current):
					latest = Run.load(execution.run_id)
					latest.finish(status, "합성 완료")
					with original(current):
						yield
				with mock.patch.object(orchestration, "locked", side_effect=finish_before_lock), \
						mock.patch.object(orchestration, "Flow") as flow, redirect_stdout(io.StringIO()) as output:
					self.assertEqual(orchestration.cmd_resume(execution.run_id, ["--calls", "3"]), 0)
				flow.assert_not_called()
				self.assertIn(status, output.getvalue())
				self.assertEqual(Run.load(execution.run_id).status, status)
				self.assertNotIn('"event": "resume"', (execution.folder / "events.jsonl").read_text(encoding="utf-8"))

	def test_terminal_run_with_a_held_lock_does_not_change_records(self):
		row = self.new()
		execution = self.quota(row)
		execution.finish("완료", "합성 완료")
		before = self.records(execution)
		with orchestration.locked(execution), self.assertRaises(RuntimeError):
			orchestration.cmd_resume(execution.run_id, ["--calls", "3"])
		self.assertEqual(self.records(execution), before)

	def test_resume_before_prepare_still_works_without_a_worktree(self):
		row = self.new()
		execution = self.quota(row, prepare=False)
		self.assertEqual((execution.step, execution.worktree), ("prepare", ""))
		def run_flow(current, *_):
			self.assertEqual((current.step, current.status), ("prepare", "작업 중"))
			current.finish("완료", "합성 재개 완료")
		with mock.patch.object(orchestration, "Flow") as flow:
			flow.side_effect = lambda *args: mock.Mock(run_all=lambda: run_flow(*args))
			self.assertEqual(orchestration.cmd_resume(execution.run_id, []), 0)
		self.assertEqual(Run.load(execution.run_id).status, "완료")

	def test_active_resume_cannot_be_cleaned_and_failed_resume_can_be_retried(self):
		row = self.new()
		execution = self.quota(row)
		self.start_mock.reset_mock()
		def run_flow(current, *_):
			before = self.records(execution)
			with self.assertRaises(RuntimeError):
				runner.run(row["id"])
			self.start_mock.assert_not_called()
			self.assertEqual(self.records(execution), before)
			self.assertTrue(self.registered(execution))
			self.assertTrue(self.ref_exists(execution.branch))
			current.finish("실패", "합성 재개 실패")
		with mock.patch.object(orchestration, "Flow") as flow:
			flow.side_effect = lambda *args: mock.Mock(run_all=lambda: run_flow(*args))
			self.assertEqual(orchestration.cmd_resume(execution.run_id, []), 1)
		with self.assertRaises(ValueError):
			runner.run(row["id"])
		self.start_mock.assert_not_called()
		self.assertTrue(self.registered(execution))
		self.assertTrue(self.ref_exists(execution.branch))
		with mock.patch.object(orchestration, "Flow") as flow:
			flow.side_effect = lambda current, *_: mock.Mock(run_all=lambda: current.finish("완료", "합성 완료"))
			self.assertEqual(orchestration.cmd_resume(execution.run_id, []), 0)
		self.assertEqual(Run.load(execution.run_id).status, "완료")

	def test_usage_cannot_overwrite_a_locked_or_stale_run(self):
		row = self.new()
		execution = self.quota(row)
		before, used = self.records(execution), project.load(row["id"])["used"]
		with orchestration.locked(execution), self.assertRaises(RuntimeError):
			runner._usage(row["id"], execution)
		self.assertEqual(self.records(execution), before)
		self.assertEqual(project.load(row["id"])["used"], used)
		latest = Run.load(execution.run_id)
		latest.calls.append({"ok": True, "seconds": 3})
		latest.save()
		runner._usage(row["id"], execution)
		self.assertEqual(Run.load(execution.run_id).calls, latest.calls)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 3, "seconds": 5.5})

	def test_cleaned_execution_cannot_resume_without_its_worktree(self):
		row = self.new()
		execution = self.quota(row)
		before = self.records(execution)
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.assert_removed(execution)
		with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
			orchestration.cmd_resume(execution.run_id, ["--calls", "3"])
		flow.assert_not_called()
		self.assertEqual(self.records(execution), before)
		self.assertFalse(Path(execution.worktree).exists())

	def test_replaced_unprepared_execution_cannot_resume_during_new_start(self):
		row = self.new()
		execution = self.quota(row, prepare=False)
		before = self.records(execution)
		def start_after_cleanup(request_path):
			self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], "")
			with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
				orchestration.cmd_resume(execution.run_id, ["--calls", "3"])
			flow.assert_not_called()
			self.assertEqual(self.records(execution), before)
			return self.start(request_path)
		self.start_mock.side_effect = start_after_cleanup
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.assertEqual(self.records(execution), before)
		self.assertNotEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)

	def test_unprepared_cleanup_holds_lock_until_task_is_replaced(self):
		row = self.new()
		execution = self.quota(row, prepare=False)
		before = self.records(execution)
		original, checked = project.update_task, []
		def competing_update(identity, task_id, **kwargs):
			if kwargs.get("status") == "실행 중" and kwargs.get("run_id") == "":
				with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
					orchestration.cmd_resume(execution.run_id, ["--calls", "3"])
				flow.assert_not_called()
				self.assertEqual(self.records(execution), before)
				checked.append(True)
			return original(identity, task_id, **kwargs)
		with mock.patch.object(project, "update_task", side_effect=competing_update):
			self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.assertEqual(checked, [True])
		self.assertEqual(self.records(execution), before)


if __name__ == "__main__":
	unittest.main()
