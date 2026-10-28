"""코드 실행 연결과 중단 뒤 사용량 정산을 합성 원장으로 검사한다."""

import shutil
import unittest
from unittest import mock

from agency import project, runner
from orchestrator import __main__ as orchestration
from orchestrator import checks
from orchestrator.flow import Flow
from orchestrator.providers import ProviderError
from orchestrator.record import Run, new_run_id
from tests import test_agency_runner as fixtures


class AgencyRunLinkTest(unittest.TestCase):
	setUp = fixtures.RunnerTest.setUp
	git = fixtures.RunnerTest.git
	new = fixtures.RunnerTest.new
	start = fixtures.RunnerTest.start

	def execution(self, row):
		task = project.load(row["id"])["tasks"][0]
		request = orchestration.read_request(task["request_path"])["request"]
		run = Run(new_run_id(), request, str(self.repo))
		shutil.copyfile(task["request_path"], run.folder / "request.toml")
		run.save()
		return run

	def active(self):
		row = self.new()
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		project.set_checkpoint(row["id"], "task", "t1")
		return row

	def test_link_precedes_worktree_and_survives_interruption(self):
		row = self.new()
		created = []
		def start(request_path, *, on_created):
			run = self.execution(row)
			on_created(run)
			self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], run.run_id)
			self.assertEqual((run.worktree, run.step), ("", "prepare"))
			created.append(run)
			raise RuntimeError("합성 중단")
		self.start_mock.side_effect = start
		with self.assertRaisesRegex(RuntimeError, "합성 중단"):
			runner.run(row["id"])
		self.assertEqual(len(created), 1)
		self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], created[0].run_id)
		self.assertEqual(Run.load(created[0].run_id).handoff["agency_owner"],
			{"project": row["id"], "task": "t1"})

	def test_link_rejects_changed_task_before_recording_owner(self):
		row = self.active()
		run = self.execution(row)
		project.update_task(row["id"], "t1", run_id="2026-10-28-999")
		with self.assertRaises(ValueError):
			runner._link(row["id"], "t1", self.repo, row["branch"], run)
		self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], "2026-10-28-999")
		self.assertEqual(run.worktree, "")

	def test_usage_save_interruption_does_not_charge_the_same_calls_twice(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.calls = [{"ok": True, "seconds": 2.5}, {"ok": False, "seconds": 1}]
		run.save()
		with mock.patch.object(Run, "save", side_effect=OSError("합성 중단")):
			with self.assertRaises(OSError):
				runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 3.5})
		runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 3.5})

	def test_old_run_cannot_resume_after_task_was_replaced(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		project.update_task(row["id"], "t1", run_id="2026-10-28-999")
		with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
			orchestration.cmd_resume(run.run_id, [])
		flow.assert_not_called()

	def test_legacy_unprepared_run_without_usage_cannot_resume_after_replacement(self):
		row = self.active()
		run = self.execution(row)
		project.update_task(row["id"], "t1", run_id="2026-10-28-999")
		with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
			orchestration.cmd_resume(run.run_id, [])
		flow.assert_not_called()

	def test_failed_link_is_not_automatically_resumed(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.finish("실패", "실행 번호 연결 실패")
		with mock.patch.object(orchestration, "Flow") as flow, self.assertRaises(RuntimeError):
			orchestration.cmd_resume(run.run_id, [])
		flow.assert_not_called()

	def test_interrupted_call_without_duration_cannot_resume_or_charge_zero(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.calls = [{"ok": None, "started": "2026-10-28T12:00:00+09:00"}]
		run.save()
		with self.assertRaisesRegex(ValueError, "시간"):
			runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 0, "seconds": 0})
		with mock.patch.object(orchestration, "Flow") as flow, self.assertRaisesRegex(RuntimeError, "시간"):
			orchestration.cmd_resume(run.run_id, [])
		flow.assert_not_called()

	def test_recorded_calls_are_charged_before_resuming_an_over_budget_run(self):
		row = self.new(budget={"calls": 1, "minutes": 10})
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		project.set_checkpoint(row["id"], "task", "t1")
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.calls = [{"ok": True, "seconds": 1}, {"ok": False, "seconds": 1}]
		run.finish("실패", "합성 중단")
		with mock.patch.object(orchestration, "cmd_resume") as resume:
			result = runner.run(row["id"])
		resume.assert_not_called()
		self.assertEqual(result["status"], "막힘")
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 2})

	def test_legacy_usage_requires_reconciliation_when_project_already_has_usage(self):
		row = self.active()
		run = self.execution(row)
		project.update_task(row["id"], "t1", run_id=run.run_id)
		project.add_usage(row["id"], 2, 5)
		run.calls = [{"ok": True, "seconds": 1}]
		run.save()
		with self.assertRaisesRegex(ValueError, "이전 사용량"):
			runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 5})

	def test_legacy_usage_rejects_a_run_for_a_different_request(self):
		row = self.active()
		run = self.execution(row)
		run.request["goal"] = "다른 합성 요청"
		run.calls = [{"ok": True, "seconds": 1}]
		run.save()
		project.update_task(row["id"], "t1", run_id=run.run_id)
		with self.assertRaisesRegex(ValueError, "요청서"):
			runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 0, "seconds": 0})

	def test_cancelled_project_does_not_settle_or_apply_an_inflight_run(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.calls = [{"ok": True, "seconds": 1}]
		run.save()
		project.cancel(row["id"])
		with self.assertRaises(ValueError):
			runner._usage(row["id"], run)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 0, "seconds": 0})

	def test_new_run_uses_only_remaining_project_budget(self):
		row = self.new(budget={"calls": 3, "minutes": 2})
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		project.set_checkpoint(row["id"], "task", "t1")
		project.add_usage(row["id"], 2, 100)
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		self.assertEqual(run.budget["calls"], 1)
		self.assertEqual(run.budget["seconds"], 20)
		self.assertEqual(Run.load(run.run_id).budget, run.budget)

	def test_exactly_exhausted_project_does_not_start_a_new_run(self):
		for used in ((1, 0), (0, 600)):
			with self.subTest(used=used):
				row = self.new(budget={"calls": 1, "minutes": 10})
				project.add_usage(row["id"], *used)
				before = self.start_mock.call_count
				result = runner.run(row["id"])
				self.assertEqual(result["status"], "막힘")
				self.assertIn("예산", result["blocked_reason"])
				self.assertEqual(self.start_mock.call_count, before)
				self.assertFalse(project.load(row["id"])["tasks"][0]["run_id"])

	def test_resume_options_cannot_exceed_remaining_project_budget(self):
		row = self.new(budget={"calls": 3, "minutes": 2})
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		project.set_checkpoint(row["id"], "task", "t1")
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		project.add_usage(row["id"], 1, 40)
		run.calls = [{"ok": True, "seconds": 30}]
		run.elapsed = 45
		run.finish("실패", "합성 중단")
		with mock.patch.object(orchestration, "_execute_locked", return_value=0) as execute:
			orchestration.cmd_resume(run.run_id, ["--calls", "100", "--minutes", "100"])
		resumed = execute.call_args.args[0]
		self.assertEqual(resumed.budget["calls"], 2)
		self.assertEqual(resumed.budget["seconds"], 95)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 1, "seconds": 40})

	def test_project_run_with_under_minimum_timeout_makes_no_provider_call(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.budget["seconds"] = 29
		plan = mock.Mock(id="fake", tool="codex")
		caller = mock.Mock(return_value=mock.Mock(seconds=1, usage={}, data={"ok": True}))
		flow = Flow(run, mock.Mock(), {}, caller=caller)
		with mock.patch("orchestrator.flow.role_plans", return_value=[plan]):
			self.assertIsNone(flow.ask("implement", "합성", {}, True))
		caller.assert_not_called()
		self.assertEqual(run.calls, [])
		self.assertEqual(run.status, "시간 초과")

	def test_legacy_resume_with_under_minimum_project_time_makes_no_provider_call(self):
		row = self.new(budget={"calls": 3, "minutes": 1})
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		run = self.execution(row)
		project.update_task(row["id"], "t1", run_id=run.run_id)
		run.calls = [{"ok": False, "started": "2026-01-01T00:00:00+00:00", "seconds": 40}]
		run.finish("실패", "합성 중단")
		caller = mock.Mock(return_value=mock.Mock(seconds=30, usage={}, data={}))
		def execute(current):
			flow = Flow(current, mock.Mock(), {}, caller=caller)
			with mock.patch("orchestrator.flow.role_plans", return_value=[mock.Mock(id="fake", tool="codex")]):
				flow.ask("implement", "합성", {}, True)
			return 0
		with mock.patch.object(orchestration, "_execute_locked", side_effect=execute):
			orchestration.cmd_resume(run.run_id, [])
		caller.assert_not_called()
		self.assertEqual(Run.load(run.run_id).status, "시간 초과")

	def test_legacy_resume_rechecks_project_usage_before_provider_call(self):
		row = self.active()
		run = self.execution(row)
		project.update_task(row["id"], "t1", run_id=run.run_id)
		project.settle_run_usage(row["id"], "t1", run.run_id, 0, 0,
			legacy_previous={"calls": 0, "seconds": 0})
		run.finish("실패", "합성 중단")
		caller = mock.Mock(return_value=mock.Mock(seconds=1, usage={}, data={}))
		def execute(current):
			project.add_usage(row["id"], row["budget"]["calls"], 0)
			flow = Flow(current, mock.Mock(), {}, caller=caller)
			with mock.patch("orchestrator.flow.role_plans", return_value=[mock.Mock(id="fake", tool="codex")]):
				flow.ask("implement", "합성", {}, True)
			return 0
		with mock.patch.object(orchestration, "_execute_locked", side_effect=execute):
			orchestration.cmd_resume(run.run_id, [])
		caller.assert_not_called()
		self.assertEqual(Run.load(run.run_id).status, "호출 한도 소진")

	def test_each_provider_call_rechecks_current_project_usage(self):
		row = self.new(budget={"calls": 1, "minutes": 10})
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		project.set_checkpoint(row["id"], "task", "t1")
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		project.add_usage(row["id"], 1, 0)
		caller = mock.Mock()
		flow = Flow(run, mock.Mock(), {}, caller=caller)
		with mock.patch("orchestrator.flow.role_plans", return_value=[mock.Mock(id="fake", tool="codex")]):
			self.assertIsNone(flow.ask("implement", "합성", {}, True))
		caller.assert_not_called()
		self.assertEqual(run.calls, [])
		self.assertEqual(run.status, "호출 한도 소진")

	def test_project_call_duration_is_not_rounded_down(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		caller = mock.Mock(return_value=mock.Mock(seconds=0.049, usage={}, data={}))
		flow = Flow(run, mock.Mock(), {}, caller=caller)
		with mock.patch("orchestrator.flow.role_plans", return_value=[mock.Mock(id="fake", tool="codex")]):
			flow.ask("implement", "합성", {}, True)
		self.assertGreaterEqual(run.calls[0]["seconds"], 0.049)

	def test_failed_project_call_duration_is_not_rounded_down(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		clock = iter(index * 0.013 for index in range(100))
		caller = mock.Mock(side_effect=ProviderError("exit", "합성 실패"))
		with mock.patch("orchestrator.flow.time.monotonic", side_effect=lambda: next(clock)), \
				mock.patch("orchestrator.flow.role_plans", return_value=[mock.Mock(id="fake", tool="codex")]):
			Flow(run, mock.Mock(), {}, caller=caller).ask("implement", "합성", {}, True)
		self.assertGreaterEqual(run.calls[0]["seconds"], 0.0129)

	def test_absorb_preserves_owner_and_usage(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.handoff["agency_usage"] = {"calls": 1, "seconds": 2}
		flow = Flow(run, mock.Mock(), {})
		with mock.patch("orchestrator.flow.checks.commit_owned", return_value=([], [])):
			self.assertTrue(flow.absorb({"summary": "합성 변경", "blockers": []}))
		self.assertEqual(run.handoff["agency_owner"], {"project": row["id"], "task": "t1"})
		self.assertEqual(run.handoff["agency_usage"], {"calls": 1, "seconds": 2})

	def test_rejected_commit_preserves_owner_and_usage(self):
		row = self.active()
		run = self.execution(row)
		runner._link(row["id"], "t1", self.repo, row["branch"], run)
		run.handoff["agency_usage"] = {"calls": 1, "seconds": 2}
		flow = Flow(run, mock.Mock(), {})
		with mock.patch("orchestrator.flow.checks.commit_owned", side_effect=checks.CommitRejected("합성 거부")):
			self.assertFalse(flow.absorb({"summary": "합성 변경", "blockers": []}))
		self.assertEqual(run.handoff["agency_owner"], {"project": row["id"], "task": "t1"})
		self.assertEqual(run.handoff["agency_usage"], {"calls": 1, "seconds": 2})
