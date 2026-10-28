"""자동 진행기를 가짜 단계 함수와 합성 임시 홈에서 검사한다."""

import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from agency import acceptance, hooks, intake, planner, project, runner, tick, tick_cli
from agency.__main__ import main
from agency.departments import Department
from collector.config import ConfigError


class AgencyTickTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve() / "hub"
		self.stamp = datetime(2030, 1, 2, 12, tzinfo=ZoneInfo("Asia/Seoul"))
		self.department = Department("sample", "합성 부서", "로컬 시험", ("sample",), ("sample",),
			True, "sample", (), "", (), (), {"calls": 10, "minutes": 5})
		self.departments = {"sample": self.department}
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(project, "now", return_value=self.stamp),
				mock.patch.object(tick, "now", return_value=self.stamp),
				mock.patch.object(tick, "load_departments", return_value=self.departments),
				mock.patch("agency.ai.ask", side_effect=AssertionError("실제 AI 호출 금지")),
				mock.patch("subprocess.Popen", side_effect=AssertionError("실제 AI·Git 호출 금지")),
				mock.patch("socket.create_connection", side_effect=AssertionError("네트워크 호출 금지")),
				mock.patch("agency.delivery.approve", side_effect=AssertionError("납품 승인 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		self.steps = {}
		for action, module, name in (("intake", intake, "step"), ("plan", planner, "plan"),
				("run", runner, "run"), ("review", acceptance, "review")):
			patch = mock.patch.object(module, name)
			self.steps[action] = patch.start()
			self.addCleanup(patch.stop)
		hooks.clear()
		self.addCleanup(hooks.clear)

	def new(self, status="접수 중", tasks=False):
		row = project.new(self.department, "합성 프로젝트", "합성 요청")
		identity = row["id"]
		if status == "취소":
			project.cancel(identity)
		elif status != "접수 중":
			project.update_spec(identity, goal="합성 기능", target="로컬 결과", out_of_scope="외부 실행",
				done_criteria=["합성 시험 통과"])
			project.confirm_spec(identity, self.department)
			for following in ("계획", "실행 중", "검수", "납품 대기", "납품 중", "완료"):
				if project.load(identity)["status"] == status:
					break
				project.transition(identity, following)
		if tasks:
			project.add_task(identity, "합성 작업", str(project._folder(identity) / "t1.toml"))
		return project.load(identity)

	def blocked(self, status="계획", minutes=61, reason="AI 한도 소진", tasks=False):
		row = self.new(status, tasks)
		row = project.transition(row["id"], "막힘", reason=reason)
		row["updated"] = (self.stamp - timedelta(minutes=minutes)).isoformat()
		(project._folder(row["id"]) / "project.json").write_text(json.dumps(row), encoding="utf-8")
		return row

	def files(self, identity):
		folder = project._folder(identity)
		return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}

	def cli(self, arguments, entry=tick_cli.main):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = entry(arguments)
		return code, out.getvalue(), err.getvalue()

	def test_each_status_calls_only_its_one_step_and_waits_without_writes(self):
		cases = (("접수 중", False, "intake"), ("명세 확정", False, "plan"),
			("계획", False, "plan"), ("계획", True, "run"), ("실행 중", True, "run"),
			("검수", True, "review"), ("납품 대기", False, "wait"), ("납품 중", False, "wait"),
			("완료", False, "wait"), ("취소", False, "wait"))
		rows = [(self.new(status, tasks), action) for status, tasks, action in cases]
		snapshots = {row["id"]: self.files(row["id"]) for row, action in rows if action == "wait"}
		result = tick.tick()
		self.assertEqual(set(result), {"skipped", "hook_failures", "projects"})
		self.assertEqual((result["skipped"], result["hook_failures"]), (False, []))
		for (row, action), report in zip(rows, result["projects"], strict=True):
			self.assertEqual(report, {"id": row["id"], "before": row["status"], "after": row["status"],
				"action": action, "error": ""})
		for action, function in self.steps.items():
			self.assertEqual(function.call_args_list, [mock.call(row["id"]) for row, expected in rows if expected == action])
		for identity, snapshot in snapshots.items():
			self.assertEqual(self.files(identity), snapshot)

	def test_changing_status_does_not_execute_the_following_stage(self):
		first, second, third = self.new("명세 확정"), self.new("계획", tasks=True), self.new("검수")
		self.steps["plan"].side_effect = lambda identity: project.transition(identity, "계획")

		def completed(identity):
			project.transition(identity, "실행 중")
			return project.transition(identity, "검수")

		self.steps["run"].side_effect = completed
		self.steps["review"].side_effect = lambda identity: project.transition(identity, "납품 대기")
		self.assertEqual([row["after"] for row in tick.tick()["projects"]], ["계획", "검수", "납품 대기"])
		for action, row in (("plan", first), ("run", second), ("review", third)):
			self.steps[action].assert_called_once_with(row["id"])
		self.steps["intake"].assert_not_called()

	def test_projects_are_processed_by_id_even_if_listing_is_unsorted(self):
		rows = [self.new() for _ in range(3)]
		with mock.patch.object(project, "list_projects", return_value=list(reversed(rows))):
			result = tick.tick()
		self.assertEqual([row["id"] for row in result["projects"]], [row["id"] for row in rows])
		self.assertEqual(self.steps["intake"].call_args_list, [mock.call(row["id"]) for row in rows])

	def test_intake_records_answer_count_and_only_runs_again_for_new_answers(self):
		identity = self.new()["id"]
		self.assertEqual(tick.tick()["projects"][0]["action"], "intake")
		self.assertEqual(project.load(identity)["checkpoint"], {"step": "intake", "detail": "0"})
		snapshot = self.files(identity)
		self.assertEqual(tick.tick()["projects"][0]["action"], "wait")
		self.assertEqual(self.files(identity), snapshot)
		project.add_answer(identity, "합성 질문", "합성 답")
		self.assertEqual(tick.tick()["projects"][0]["action"], "intake")
		self.assertEqual(project.load(identity)["checkpoint"], {"step": "intake", "detail": "1"})
		self.assertEqual(tick.tick()["projects"][0]["action"], "wait")
		self.assertEqual(self.steps["intake"].call_args_list, [mock.call(identity), mock.call(identity)])

	def test_open_questions_wait_even_when_answer_count_changes(self):
		identity = self.new()["id"]
		project.set_checkpoint(identity, "intake", "0")
		project.add_answer(identity, "지난 질문", "합성 답")
		questions = {"open": [{"no": 1, "question": "남은 질문", "asked": self.stamp.isoformat()}], "next_no": 2}
		(project._folder(identity) / "questions.json").write_text(json.dumps(questions), encoding="utf-8")
		snapshot = self.files(identity)
		self.assertEqual(tick.tick()["projects"][0]["action"], "wait")
		self.steps["intake"].assert_not_called()
		self.assertEqual(self.files(identity), snapshot)

	def test_checkpoints_only_affect_intake_in_intake_status(self):
		first, second = self.new(), self.new("계획", tasks=True)
		project.set_checkpoint(first["id"], "task", "0")
		project.set_checkpoint(second["id"], "intake", "0")
		tick.tick()
		self.steps["intake"].assert_called_once_with(first["id"])
		self.steps["run"].assert_called_once_with(second["id"])
		self.assertEqual(project.load(second["id"])["checkpoint"], {"step": "intake", "detail": "0"})

	def test_failed_intake_does_not_record_a_successful_checkpoint(self):
		identity = self.new()["id"]
		self.steps["intake"].side_effect = RuntimeError("합성 호출 실패")
		self.assertIn("RuntimeError", tick.tick()["projects"][0]["error"])
		self.assertEqual(project.load(identity)["checkpoint"], {"step": "", "detail": ""})

	def test_quota_waits_at_59_minutes_and_retries_at_60_and_61(self):
		rows = [self.blocked(minutes=minutes) for minutes in (59, 60, 61)]
		snapshot = self.files(rows[0]["id"])
		with mock.patch.object(project, "resume", wraps=project.resume) as resume:
			result = tick.tick()
		self.assertEqual([row["action"] for row in result["projects"]], ["wait", "retry", "retry"])
		self.assertEqual(resume.call_args_list, [mock.call(row["id"]) for row in rows[1:]])
		self.assertEqual(self.steps["plan"].call_args_list, [mock.call(row["id"]) for row in rows[1:]])
		self.assertEqual(self.files(rows[0]["id"]), snapshot)

	def test_quota_resume_uses_restored_status_for_one_step(self):
		cases = (("접수 중", False, "intake"), ("명세 확정", False, "plan"), ("계획", False, "plan"),
			("계획", True, "run"), ("실행 중", True, "run"), ("검수", True, "review"))
		rows = [(self.blocked(status, tasks=tasks), action) for status, tasks, action in cases]
		with mock.patch.object(hooks, "changed", wraps=hooks.changed) as changed:
			result = tick.tick()
		self.assertEqual([row["action"] for row in result["projects"]], ["retry"] * len(rows))
		for action, function in self.steps.items():
			self.assertEqual(function.call_args_list, [mock.call(row["id"]) for row, expected in rows if expected == action])
		self.assertEqual(changed.call_args_list,
			[mock.call(row["id"], "막힘", row["blocked_from"]) for row, _ in rows])

	def test_non_quota_blocks_are_left_untouched(self):
		rows = [self.blocked(reason=reason) for reason in ("원인 미상", "검수 불합격", "납품 반려", "형식 오류")]
		snapshots = {row["id"]: self.files(row["id"]) for row in rows}
		with mock.patch.object(project, "resume", wraps=project.resume) as resume:
			result = tick.tick()
		self.assertEqual([row["action"] for row in result["projects"]], ["wait"] * len(rows))
		resume.assert_not_called()
		for identity, snapshot in snapshots.items():
			self.assertEqual(self.files(identity), snapshot)

	def test_invalid_quota_timestamps_report_errors_without_resuming(self):
		for updated in ("잘못된 시각", self.stamp.replace(tzinfo=None).isoformat()):
			with self.subTest(updated=updated):
				first, second = self.blocked(), self.new("계획")
				first["updated"] = updated
				self.steps["plan"].reset_mock()
				with mock.patch.object(project, "list_projects", return_value=[first, second]), \
						mock.patch.object(project, "resume", wraps=project.resume) as resume:
					result = tick.tick()["projects"]
				self.assertTrue(result[0]["error"].startswith("ValueError: "))
				self.assertEqual(result[0]["after"], "막힘")
				resume.assert_not_called()
				self.steps["plan"].assert_called_once_with(second["id"])

	def test_missing_or_external_ai_forbidden_departments_are_skipped(self):
		rows = [self.new(), self.blocked()]
		snapshots = {row["id"]: self.files(row["id"]) for row in rows}
		for departments in ({}, {"sample": replace(self.department, external_ai=False)}):
			with self.subTest(departments=bool(departments)), \
					mock.patch.object(tick, "load_departments", return_value=departments), \
					mock.patch.object(project, "resume", wraps=project.resume) as resume:
				self.assertEqual([row["action"] for row in tick.tick()["projects"]], ["skip", "skip"])
				resume.assert_not_called()
		for function in self.steps.values():
			function.assert_not_called()
		for identity, snapshot in snapshots.items():
			self.assertEqual(self.files(identity), snapshot)

	def test_project_errors_are_bounded_and_do_not_stop_the_next_project(self):
		for exception in (ValueError, RuntimeError, ConfigError, OSError):
			with self.subTest(exception=exception.__name__):
				rows = [self.new("계획"), self.new("계획")]
				self.steps["plan"].reset_mock()
				self.steps["plan"].side_effect = [exception("가" * 250), None]
				with mock.patch.object(project, "list_projects", return_value=rows):
					result = tick.tick()["projects"]
				self.assertEqual(result[0]["error"], exception.__name__ + ": " + "가" * 200)
				self.assertEqual((result[0]["action"], result[1]["action"], result[1]["error"]), ("plan", "plan", ""))
				self.assertEqual(self.steps["plan"].call_args_list, [mock.call(row["id"]) for row in rows])

	def test_status_readback_failure_does_not_stop_the_next_project(self):
		rows = [self.new("계획"), self.new("계획")]
		load = project.load

		def read(identity):
			if identity == rows[0]["id"]:
				raise OSError("합성 상태 읽기 실패")
			return load(identity)

		with mock.patch.object(project, "list_projects", return_value=rows), \
				mock.patch.object(project, "load", side_effect=read):
			result = tick.tick()["projects"]
		self.assertEqual(result[0]["error"], "OSError: 합성 상태 읽기 실패")
		self.assertEqual(result[1]["error"], "")
		self.assertEqual(self.steps["plan"].call_args_list, [mock.call(row["id"]) for row in rows])

	def test_changed_gets_persisted_status_even_if_step_raises(self):
		first, second = self.new("계획"), self.new("검수")

		def failed(identity):
			project.transition(identity, "막힘", reason="한도 소진")
			raise RuntimeError("합성 실패")

		self.steps["plan"].side_effect = failed
		self.steps["review"].side_effect = lambda identity: project.transition(identity, "납품 대기")
		with mock.patch.object(hooks, "changed", wraps=hooks.changed) as changed:
			result = tick.tick()["projects"]
		self.assertEqual([row["after"] for row in result], ["막힘", "납품 대기"])
		self.assertEqual(changed.call_args_list, [mock.call(first["id"], "계획", "막힘"),
			mock.call(second["id"], "검수", "납품 대기")])
		self.steps["run"].assert_not_called()

	def test_unchanged_status_does_not_call_changed(self):
		self.new("계획")
		with mock.patch.object(hooks, "changed") as changed:
			tick.tick()
		changed.assert_not_called()

	def test_before_tick_reports_failure_and_imports_answers_before_progress(self):
		identity = self.new()["id"]
		project.set_checkpoint(identity, "intake", "0")

		def broken():
			raise RuntimeError("합성 연결 실패")

		hooks.on_before_tick(broken)
		hooks.on_before_tick(lambda: project.add_answer(identity, "합성 질문", "합성 답"))
		result = tick.tick()
		self.assertEqual(result["hook_failures"], ["broken"])
		self.steps["intake"].assert_called_once_with(identity)

	def test_busy_lock_skips_hooks_and_listing(self):
		path = self.hub / "run" / "locks" / "agency-tick.lock"
		path.parent.mkdir(parents=True)
		with path.open("a+") as lock, mock.patch.object(hooks, "before_tick") as before, \
				mock.patch.object(project, "list_projects") as listing:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			self.assertEqual(tick.tick(), {"skipped": True})
			before.assert_not_called()
			listing.assert_not_called()
		self.assertFalse(tick.tick()["skipped"])

	def test_lock_rejects_links_and_non_regular_files_before_hooks(self):
		path = self.hub / "run" / "locks" / "agency-tick.lock"
		path.parent.mkdir(parents=True)
		target = self.hub / "target"
		target.write_text("보존할 합성 자료", encoding="utf-8")
		with mock.patch.object(hooks, "before_tick") as before:
			path.symlink_to(target)
			with self.assertRaises(ValueError):
				tick.tick()
			self.assertEqual(target.read_text(encoding="utf-8"), "보존할 합성 자료")
			path.unlink()
			os.mkfifo(path)
			with self.assertRaises(ValueError):
				tick.tick()
			before.assert_not_called()

	def test_lock_is_held_through_hooks_and_project_step(self):
		self.new()
		nested = []
		hooks.on_before_tick(lambda: nested.append(tick.tick()))
		self.steps["intake"].side_effect = lambda identity: nested.append(tick.tick())
		self.assertFalse(tick.tick()["skipped"])
		self.assertEqual(nested, [{"skipped": True}, {"skipped": True}])

	def test_lock_is_released_when_listing_fails(self):
		with mock.patch.object(project, "list_projects", side_effect=OSError("합성 읽기 실패")):
			with self.assertRaises(OSError):
				tick.tick()
		self.assertEqual(tick.tick(), {"skipped": False, "hook_failures": [], "projects": []})

	def test_cli_prints_project_results_and_errors_and_loads_link_first(self):
		identity = self.new()["id"]
		report = {"skipped": False, "hook_failures": [], "projects": [
			{"id": identity, "before": "계획", "after": "막힘", "action": "plan", "error": "RuntimeError: 합성 실패"}]}
		order = []
		with mock.patch.object(tick_cli.importlib, "import_module", side_effect=lambda name: order.append(name)), \
				mock.patch.object(tick_cli, "tick", side_effect=lambda: order.append("tick") or report):
			self.assertEqual(self.cli(["tick"]), (0,
				f"{identity} 계획 -> 막힘 (plan)\n  오류: RuntimeError: 합성 실패\n", ""))
		self.assertEqual(order, ["agency.telegram_link", "tick"])

	def test_cli_prints_hook_failures_before_projects(self):
		report = {"skipped": False, "hook_failures": ["sync"], "projects": []}
		with mock.patch.object(tick_cli.importlib, "import_module"), mock.patch.object(tick_cli, "tick", return_value=report):
			self.assertEqual(self.cli(["tick"]), (0, "알림 연결 실패: sync\n", ""))

	def test_cli_missing_telegram_module_and_busy_message(self):
		with mock.patch.object(tick_cli.importlib, "import_module", side_effect=ModuleNotFoundError), \
				mock.patch.object(tick_cli, "tick", return_value={"skipped": True}):
			self.assertEqual(self.cli(["tick"]), (0, "다른 진행이 이미 돌고 있습니다\n", ""))

	def test_main_delegates_tick_without_telegram_module(self):
		identity = self.new()["id"]
		with mock.patch.dict(sys.modules, {"agency.telegram_link": None}):
			self.assertEqual(self.cli(["tick"], entry=main), (0, f"{identity} 접수 중 -> 접수 중 (intake)\n", ""))
		self.steps["intake"].assert_called_once_with(identity)

	def test_cli_valid_arguments_always_return_zero_on_failure(self):
		for exception in (ValueError, RuntimeError, ConfigError, OSError, Exception):
			with self.subTest(exception=exception.__name__), \
					mock.patch.dict(sys.modules, {"agency.telegram_link": None}), \
					mock.patch.object(tick_cli, "tick", side_effect=exception("합성 오류")):
				code, out, err = self.cli(["tick"])
				self.assertEqual((code, out), (0, ""))
				self.assertIn("오류:", err)
				self.assertIn("합성 오류", err)

	def test_cli_rejects_invalid_arguments_before_importing_or_running(self):
		with mock.patch.object(tick_cli.importlib, "import_module") as importing, \
				mock.patch.object(tick_cli, "tick") as progress:
			for arguments in ([], ["unknown"], ["tick", "extra"], ["tick", "--help"]):
				with self.subTest(arguments=arguments):
					code, out, err = self.cli(arguments)
					self.assertEqual((code, out), (2, ""))
					self.assertIn("사용법:", err)
			importing.assert_not_called()
			progress.assert_not_called()


if __name__ == "__main__":
	unittest.main()
