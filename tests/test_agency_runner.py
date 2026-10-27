"""임시 홈·Git 저장소와 가짜 실행으로 순차 실행·반영·재개를 검사한다."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agency import project, runner, runner_cli
from agency.__main__ import main
from agency.departments import load_departments
from collector.backfill_common import locked
from collector.config import ConfigError, load_plans
from orchestrator import __main__ as orchestration, checks
from orchestrator.branch_apply import apply_to_branch
from orchestrator.flow import Flow
from orchestrator.record import Run, new_run_id
from tests.test_orchestrator_targets import init_repo, write_repos


class RunnerTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.repo = init_repo(self.root / "repo")
		(self.repo / "value.txt").write_text("시작\n", encoding="utf-8")
		self.git("add", "--", "value.txt")
		self.git("commit", "-q", "-m", "시작")
		self.head = self.git("rev-parse", "HEAD")
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest"})
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "writer"
tool = "codex"
owner = "personal"
allow = ["personal"]
[[plans]]
id = "reviewer"
tool = "agy"
owner = "personal"
allow = ["personal"]
[orchestration]
implementer = ["writer"]
reviewer = ["reviewer"]
''', encoding="utf-8")
		(self.hub / "departments.toml").write_text('''[[department]]
id = "sample"
name = "합성 부서"
scope = "로컬 시험"
repos = ["sample"]
allow_plans = ["writer", "reviewer"]
playbook = "sample"
budget = { calls = 20, minutes = 10 }
''', encoding="utf-8")
		self.order, self.bases, self.outcomes = [], [], []
		self.calls = [{"ok": False}, {"ok": True, "seconds": 2.5}]
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(orchestration, "finish_outputs"),
				mock.patch("orchestrator.flow.call", side_effect=AssertionError("실제 AI 호출 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		self.department = load_departments()["sample"]
		patch = mock.patch.object(orchestration, "start_run", side_effect=self.start)
		self.start_mock = patch.start()
		self.addCleanup(patch.stop)

	def git(self, *args):
		return checks.git(self.repo, *args).strip()

	def snapshot(self):
		return (self.git("rev-parse", "HEAD"), (self.repo / ".git/HEAD").read_bytes(),
			(self.repo / ".git/index").read_bytes(),
			{str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
				if path.is_file() and ".git" not in path.relative_to(self.repo).parts})

	def new(self, count=1, budget=None, status="계획"):
		row = project.new(self.department, "합성 프로젝트", "합성 요청")
		identity = row["id"]
		if status != "접수 중":
			project.update_spec(identity, goal="합성 기능", target="로컬 결과", out_of_scope="외부 작업",
				done_criteria=["합성 시험 통과"], budget=budget or self.department.budget)
			project.confirm_spec(identity, self.department)
			for following in ("계획", "실행 중", "검수", "납품 대기", "납품 중", "완료"):
				if project.load(identity)["status"] == status:
					break
				project.transition(identity, following)
		for number in range(1, count + 1):
			path = self.hub / "projects" / identity / f"t{number}.toml"
			request = {"repo": str(self.repo), "goal": f"합성 작업 {number}", "done": ["변경 반영"],
				"owned_paths": ["value.txt"], "base_branch": row["branch"]}
			path.write_text("[request]\n" + "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
				for key, value in request.items()), encoding="utf-8")
			project.add_task(identity, f"합성 작업 {number}", str(path), [f"t{number - 1}"] if number > 1 else [])
		return project.load(identity)

	def execution(self, task, status="승인 필요", reason="합성 검토 통과"):
		request = orchestration.read_request(task["request_path"])["request"]
		execution = Run(new_run_id(), request, str(self.repo))
		execution.calls = [dict(call) for call in self.calls]
		execution.save()
		if status == "승인 필요":
			Flow(execution, load_plans(), orchestration.orchestration_config()).prepare()
			self.bases.append(execution.base_commit)
			path = Path(execution.worktree) / "value.txt"
			path.write_text(path.read_text(encoding="utf-8") + task["id"] + "\n", encoding="utf-8")
			checks.git(execution.worktree, "add", "--", "value.txt")
			checks.git(execution.worktree, "commit", "-q", "-m", "합성 구현 " + task["id"])
		execution.finish(status, reason)
		return execution

	def start(self, request_path):
		identity = Path(request_path).parent.name
		row = project.load(identity)
		task = next(task for task in row["tasks"] if task["request_path"] == request_path)
		self.assertEqual(task["status"], "실행 중")
		self.assertEqual(task["run_id"], "")
		self.assertEqual(sum(task["status"] == "실행 중" for task in row["tasks"]), 1)
		self.assertEqual(row["checkpoint"], {"step": "task", "detail": task["id"]})
		with self.assertRaises(ConfigError), locked("agency-repo:" + str(self.repo)):
			self.fail("저장소 잠금이 유지되어야 합니다")
		self.order.append(task["id"])
		return self.execution(task, *self.outcomes.pop(0)) if self.outcomes else self.execution(task)

	def running(self, row, execution):
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중", run_id=execution.run_id)

	def test_creates_branch_at_head_and_applies_two_dependencies_without_touching_main(self):
		row = self.new(2)
		(self.repo / "value.txt").write_text("스테이징 보존\n", encoding="utf-8")
		self.git("add", "--", "value.txt")
		(self.repo / "value.txt").write_text("작업 파일 보존\n", encoding="utf-8")
		(self.repo / "local.txt").write_text("추적하지 않는 파일 보존", encoding="utf-8")
		before = self.snapshot()
		with mock.patch.object(project, "set_checkpoint", wraps=project.set_checkpoint) as checkpoint, \
				mock.patch.object(runner, "git", wraps=runner.git) as git:
			result = runner.run(row["id"])
		self.assertEqual(result, {"status": "검수", "done": ["t1", "t2"], "blocked_reason": ""})
		self.assertEqual(self.order, ["t1", "t2"])
		saved = project.load(row["id"])
		commits = [task["commit"] for task in saved["tasks"]]
		self.assertEqual(self.bases, [self.head, commits[0]])
		self.assertEqual(self.git("rev-list", "--count", self.head + ".." + row["branch"]), "2")
		self.assertEqual(self.git("show", row["branch"] + ":value.txt"), "시작\nt1\nt2")
		self.assertEqual([self.git("rev-parse", commit + "^") for commit in commits], self.bases)
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(saved["used"], {"calls": 4, "seconds": 5.0})
		self.assertEqual(checkpoint.call_args_list[0].args, (row["id"], "branch", self.head))
		self.assertIn(mock.call(self.repo, "branch", row["branch"], self.head), git.call_args_list)
		self.assertFalse(any(call.args[1] == "checkout" for call in git.call_args_list))
		self.assertTrue(all(Run.load(task["run_id"]).status == "완료" for task in saved["tasks"]))

	def test_project_without_tasks_is_refused_without_state_change(self):
		row = self.new(count=0)
		with self.assertRaisesRegex(ValueError, "등록된 작업이 없습니다"):
			runner.run(row["id"])
		self.assertEqual(project.load(row["id"])["status"], "계획")

	def test_existing_project_branch_keeps_its_tip(self):
		row = self.new()
		self.git("branch", row["branch"], self.head)
		(self.repo / "main.txt").write_text("본 가지의 다른 변경", encoding="utf-8")
		self.git("add", "--", "main.txt")
		self.git("commit", "-q", "-m", "본 가지 전진")
		before = self.snapshot()
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.assertEqual(self.bases, [self.head])
		self.assertEqual(self.snapshot(), before)

	def test_quota_failure_waits_and_next_invocation_finishes_remaining_dependencies(self):
		row = self.new(2)
		reason = "종료 코드 1 (한도 소진) " + "가" * 240
		self.outcomes = [("실패", reason)]
		result = runner.run(row["id"])
		self.assertEqual(result, {"status": "막힘", "done": [], "blocked_reason": "한도 소진으로 대기: " + reason[:200]})
		blocked = project.load(row["id"])
		self.assertEqual([task["status"] for task in blocked["tasks"]], ["대기", "대기"])
		self.assertEqual(blocked["used"], {"calls": 2, "seconds": 2.5})
		self.assertEqual(self.git("rev-parse", row["branch"]), self.head)
		self.assertEqual(runner.run(row["id"]), {"status": "검수", "done": ["t1", "t2"], "blocked_reason": ""})
		resumed = project.load(row["id"])
		self.assertNotEqual(resumed["tasks"][0]["run_id"], blocked["tasks"][0]["run_id"])
		self.assertEqual(self.order, ["t1", "t1", "t2"])
		self.assertEqual(resumed["used"], {"calls": 6, "seconds": 7.5})

	def test_general_failure_stops_before_next_task_and_counts_failed_calls(self):
		row = self.new(2)
		reason = "합성 검증 실패 " + "가" * 230
		self.outcomes = [("시간 초과", reason)]
		self.calls = [{"ok": False, "seconds": 7}, {"ok": False}]
		result = runner.run(row["id"])
		self.assertEqual(result["status"], "막힘")
		self.assertEqual(result["blocked_reason"], "작업 t1 실패: " + reason[:200])
		self.assertEqual([task["status"] for task in project.load(row["id"])["tasks"]], ["실패", "대기"])
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 7})
		self.assertEqual(self.order, ["t1"])

	def test_call_and_time_budget_excess_stop_before_the_second_task(self):
		for budget, seconds in (({"calls": 1, "minutes": 10}, 2.5), ({"calls": 20, "minutes": 1}, 60.5)):
			with self.subTest(budget=budget):
				row = self.new(2, budget)
				self.calls = [{"ok": False}, {"ok": True, "seconds": seconds}]
				self.assertEqual(runner.run(row["id"]), {"status": "막힘", "done": ["t1"],
					"blocked_reason": "프로젝트 예산을 넘었습니다"})
				self.assertEqual(project.load(row["id"])["tasks"][1]["status"], "대기")

	def test_already_exceeded_budget_does_not_make_more_calls(self):
		row = self.new()
		project.add_usage(row["id"], 21, 0)
		self.assertEqual(runner.run(row["id"])["blocked_reason"], "프로젝트 예산을 넘었습니다")
		self.start_mock.assert_not_called()

	def test_running_approved_execution_is_applied_without_starting_again(self):
		row = self.new()
		self.git("branch", row["branch"], self.head)
		execution = self.execution(row["tasks"][0])
		self.running(row, execution)
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.start_mock.assert_not_called()
		self.assertEqual(project.load(row["id"])["tasks"][0]["commit"], self.git("rev-parse", row["branch"]))
		self.assertEqual(project.load(row["id"])["used"], {"calls": 2, "seconds": 2.5})

	def test_completed_execution_recovers_tip_without_reapplying_or_double_counting(self):
		row = self.new()
		self.git("branch", row["branch"], self.head)
		execution = self.execution(row["tasks"][0])
		self.running(row, execution)
		runner._usage(row["id"], execution)
		commit = apply_to_branch(execution.run_id, row["branch"])
		with mock.patch.object(runner, "apply_to_branch") as apply:
			self.assertEqual(runner.run(row["id"])["status"], "검수")
		apply.assert_not_called()
		self.start_mock.assert_not_called()
		saved = project.load(row["id"])
		self.assertEqual(saved["tasks"][0]["commit"], commit)
		self.assertEqual(saved["used"], {"calls": 2, "seconds": 2.5})

	def test_running_and_interrupted_executions_resume_same_run_and_count_new_calls(self):
		for status in ("작업 중", "실패", "시간 초과", "호출 한도 소진", "예산 소진"):
			with self.subTest(status=status):
				row = self.new()
				self.git("branch", row["branch"], self.head)
				execution = self.execution(row["tasks"][0])
				execution.finish(status, "합성 중단")
				self.running(row, execution)
				runner._usage(row["id"], execution)
				def finish(resumed):
					self.assertEqual(resumed.run_id, execution.run_id)
					resumed.handoff = {"summary": "재개한 구현의 새 결과"}
					resumed.calls.append({"ok": True, "seconds": 3})
					resumed.finish("승인 필요", "합성 재개 성공")
					return 0
				with mock.patch.object(orchestration, "execute", side_effect=finish), \
						mock.patch.object(orchestration, "cmd_resume", wraps=orchestration.cmd_resume) as resume:
					self.assertEqual(runner.run(row["id"])["status"], "검수")
				resume.assert_called_once_with(execution.run_id, [])
				self.assertEqual(project.load(row["id"])["used"], {"calls": 3, "seconds": 5.5})
		self.start_mock.assert_not_called()

	def test_running_task_without_a_run_id_is_retried(self):
		row = self.new()
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중")
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.start_mock.assert_called_once_with(row["tasks"][0]["request_path"])

	def test_apply_error_fails_task_and_blocks_project(self):
		row = self.new(2)
		with mock.patch.object(runner, "apply_to_branch", side_effect=RuntimeError("합성 충돌")):
			self.assertEqual(runner.run(row["id"]), {"status": "막힘", "done": [], "blocked_reason": "작업 t1 실패: 합성 충돌"})
		self.assertEqual([task["status"] for task in project.load(row["id"])["tasks"]], ["실패", "대기"])
		self.assertEqual(self.order, ["t1"])

	def test_rejects_non_executable_states(self):
		for status in ("접수 중", "명세 확정", "검수", "납품 대기", "납품 중", "완료", "취소"):
			with self.subTest(status=status):
				row = self.new(status="계획" if status == "취소" else status)
				if status == "취소":
					project.cancel(row["id"])
				with self.assertRaises(ValueError):
					runner.run(row["id"])
		self.start_mock.assert_not_called()

	def test_blocked_plan_is_resumed_and_no_ready_tasks_stay_blocked(self):
		row = self.new()
		project.transition(row["id"], "막힘", reason="합성 계획 대기")
		self.assertEqual(runner.run(row["id"])["status"], "검수")
		row = self.new()
		project.update_task(row["id"], "t1", status="실패")
		self.assertEqual(runner.run(row["id"])["status"], "막힘")

	def test_repository_lock_prevents_mutating_project_or_branch(self):
		row = self.new()
		with locked("agency-repo:" + str(self.repo)), self.assertRaises(ConfigError):
			runner.run(row["id"])
		self.assertEqual(project.load(row["id"]), row)
		self.assertEqual(checks.git(self.repo, "rev-parse", "--verify", "refs/heads/" + row["branch"], check=False), "")
		self.start_mock.assert_not_called()

	def test_request_must_match_the_locked_repository_branch_and_code_kind(self):
		for change in ({"repo": str(self.root)}, {"base_branch": "other"}, {"kind": "analysis"}):
			with self.subTest(change=change):
				row = self.new()
				path = Path(row["tasks"][0]["request_path"])
				request = orchestration.read_request(str(path))["request"] | change
				path.write_text("[request]\n" + "\n".join(key + "=" + json.dumps(value)
					for key, value in request.items()), encoding="utf-8")
				with self.assertRaises(ValueError):
					runner.run(row["id"])
		self.start_mock.assert_not_called()

	def cli(self, arguments, entry=runner_cli.main):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = entry(arguments)
		return code, out.getvalue(), err.getvalue()

	def test_cli_success_and_blocked_codes_and_main_delegation(self):
		row = self.new()
		code, out, err = self.cli(["run", row["id"]], main)
		self.assertEqual((code, out, err), (0, "상태: 검수\n완료 작업: t1\n", ""))
		row = self.new()
		self.outcomes = [("실패", "합성 실패")]
		code, out, err = self.cli(["run", row["id"]])
		self.assertEqual((code, err), (3, ""))
		self.assertIn("상태: 막힘\n완료 작업: 없음\n막힘 이유: 작업 t1 실패: 합성 실패\n", out)

	def test_cli_argument_and_exception_codes(self):
		for arguments in ([], ["run"], ["run", "id", "extra"], ["show", "id"], ["run", "--bad"]):
			with self.subTest(arguments=arguments):
				code, out, err = self.cli(arguments)
				self.assertEqual((code, out), (2, ""))
				self.assertIn("사용법:", err)
		for error in (ValueError, ConfigError, RuntimeError):
			with self.subTest(error=error), mock.patch.object(runner_cli, "run", side_effect=error("합성 오류")):
				self.assertEqual(self.cli(["run", "id"]), (1, "", "오류: 합성 오류\n"))
		self.assertEqual(self.cli(["run", "invalid"])[0], 1)


if __name__ == "__main__":
	unittest.main()
