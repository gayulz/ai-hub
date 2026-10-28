"""임시 홈·볼트·Git과 가짜 AI로 CLI 보고서 출력과 반영 후 기록을 검사한다."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agency import project
from agency.__main__ import main as agency_main
from agency.departments import load_departments
from collector import vault
from collector.config import load_plans
from orchestrator import __main__ as cli, checks
from orchestrator.flow import Flow
from orchestrator.record import Run, new_run_id
from tests.test_orchestrator import FakeAI, PASS, PLANS
from tests.test_orchestrator_targets import init_repo, write_repos


class AgencyReportTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub, self.vault = self.root / "hub", self.root / "vault"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "plans.toml").write_text(PLANS, encoding="utf-8")
		(self.hub / "sources.toml").write_text(f'vault = "{self.vault}"\n', encoding="utf-8")
		for folder in vault.landing_dirs(self.vault):
			folder.mkdir(parents=True)
		self.repo = init_repo(self.root / "repo")
		(self.repo / "src").mkdir()
		(self.repo / "src/a.py").write_text("VALUE = 1\n", encoding="utf-8")
		(self.repo / "tests").mkdir()
		(self.repo / "tests/test_a.py").write_text(
			"import unittest\nfrom src.a import VALUE\nclass T(unittest.TestCase):\n"
			"\tdef test_value(self):\n\t\tself.assertEqual(VALUE, 2)\n", encoding="utf-8")
		self.git("add", "--", "src/a.py", "tests/test_a.py")
		self.git("commit", "-q", "-m", "합성 시작")
		self.head = self.git("rev-parse", "HEAD")
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": checks.DEFAULT_TEST})
		(self.hub / "departments.toml").write_text('''[[department]]
id = "sample"
name = "합성 부서"
scope = "로컬 시험"
repos = ["sample"]
allow_plans = ["codex-personal", "claude-personal"]
playbook = "sample"
budget = { calls = 20, minutes = 10 }
''', encoding="utf-8")
		self.fake = FakeAI([PASS], {"src/a.py": "VALUE = 2\n"})

	def git(self, *args):
		return checks.git(self.repo, *args).strip()

	def write_request(self, path, request, calls=8):
		path.write_text("[request]\n" + "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
			for key, value in request.items()) + f"\n[budget]\ncalls={calls}\n", encoding="utf-8")
		return str(path)

	def new_project(self, calls=8):
		department = load_departments()["sample"]
		row = project.new(department, "합성 프로젝트", "합성 요청")
		identity = row["id"]
		project.update_spec(identity, goal="합성 값 변경", target="로컬 결과", out_of_scope="외부 작업",
			done_criteria=["시험 통과"], budget=department.budget)
		project.confirm_spec(identity, department)
		project.transition(identity, "계획")
		request = {"repo": str(self.repo), "goal": "합성 값 변경", "done": ["VALUE 가 2"],
			"owned_paths": ["src/*"], "test_command": checks.DEFAULT_TEST, "base_branch": row["branch"]}
		path = self.hub / "projects" / identity / "t1.toml"
		project.add_task(identity, "합성 작업", self.write_request(path, request, calls))
		return project.load(identity)

	def invoke(self, main, args):
		with mock.patch("orchestrator.flow.call", self.fake), \
				redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
			code = main(args)
		return code, output.getvalue(), error.getvalue()

	def assert_outputs(self, execution, output):
		header = f"# 오케스트레이션 실행 {execution.run_id}"
		self.assertEqual(output.splitlines().count(header), 1)
		self.assertEqual((execution.folder / "report.md").read_text(encoding="utf-8"), cli.report(execution))
		journals = list(self.vault.rglob("*.md"))
		self.assertEqual(len(journals), 1)
		journal = journals[0]
		self.assertEqual(journal.read_text(encoding="utf-8").count(f"<!-- ai-hub:run:{execution.run_id} -->"), 1)
		body = vault.block_body(journal, "run:" + execution.run_id)
		self.assertIn(f"- 상태: {execution.status} — {execution.end_reason}", body)
		return body

	def assert_delivered(self, row, output):
		saved = project.load(row["id"])
		task = saved["tasks"][0]
		execution = Run.load(task["run_id"])
		self.assertEqual((saved["status"], task["status"], execution.status), ("검수", "완료", "완료"))
		commit = task["commit"]
		self.assertEqual(self.git("rev-parse", row["branch"]), commit)
		self.assertIn(commit[:12], execution.end_reason)
		body = self.assert_outputs(execution, output)
		self.assertIn(commit[:12], body)
		self.assertIn("- 상태: 승인 필요", output)
		self.assertTrue(output.endswith("상태: 검수\n완료 작업: t1\n"))
		self.assertEqual([call["role"] for call in execution.calls if call.get("ok")], ["implement", "review"])
		self.assertTrue(all(check["ok"] for check in execution.checks))
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
		self.assertEqual((self.repo / "src/a.py").read_text(encoding="utf-8"), "VALUE = 1\n")
		return execution

	def test_new_agency_run_prints_one_report_through_branch_apply(self):
		row = self.new_project()
		code, output, error = self.invoke(agency_main, ["run", row["id"]])
		self.assertEqual((code, error), (0, ""))
		execution = self.assert_delivered(row, output)
		self.assertEqual(len(execution.calls), 2)

	def test_agency_resumes_the_same_run_and_prints_once_through_branch_apply(self):
		row = self.new_project()
		self.git("branch", row["branch"], self.head)
		request = cli.read_request(row["tasks"][0]["request_path"])["request"]
		execution = Run(new_run_id(), request, str(self.repo))
		execution.save()
		(execution.folder / "request.toml").write_bytes(Path(row["tasks"][0]["request_path"]).read_bytes())
		Flow(execution, load_plans(), cli.orchestration_config()).prepare()
		execution.calls = [{"role": "implement", "plan": "codex-personal", "tool": "codex",
			"started": "2026-01-01T00:00:00+00:00", "seconds": 0, "ok": None}]
		execution.finish("호출 한도 소진", "합성 중단")
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", status="실행 중", run_id=execution.run_id)
		code, output, error = self.invoke(agency_main, ["run", row["id"]])
		self.assertEqual((code, error), (0, ""))
		resumed = self.assert_delivered(row, output)
		self.assertEqual(resumed.run_id, execution.run_id)
		self.assertEqual(len(resumed.calls), 3)
		self.assertFalse(resumed.calls[0]["ok"])
		events = [json.loads(line)["event"] for line in (resumed.folder / "events.jsonl").read_text().splitlines()]
		self.assertIn("resume", events)

	def test_agency_failure_and_call_limit_still_print_one_terminal_report(self):
		for expected, calls, failed_tools in (("실패", 8, ("codex",)), ("호출 한도 소진", 1, ())):
			with self.subTest(status=expected):
				row = self.new_project(calls)
				self.fake = FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}, fail_tools=failed_tools)
				code, output, error = self.invoke(agency_main, ["run", row["id"]])
				self.assertEqual((code, error), (3, ""))
				saved = project.load(row["id"])
				task = saved["tasks"][0]
				execution = Run.load(task["run_id"])
				self.assertEqual((saved["status"], task["status"], execution.status), ("막힘", "실패", expected))
				self.assert_outputs(execution, output)
				self.assertTrue(output.startswith(cli.report(execution) + "\n"))
				self.assertIn("상태: 막힘\n완료 작업: 없음\n막힘 이유: ", output)
				self.assertEqual(task["commit"], "")
				self.assertEqual(self.git("rev-parse", row["branch"]), self.head)
				self.assertTrue(Path(execution.worktree).exists())

	def test_plain_run_status_apply_and_finished_resume_keep_their_output_and_codes(self):
		request = {"repo": str(self.repo), "goal": "합성 값 변경", "done": ["VALUE 가 2"],
			"owned_paths": ["src/*"], "test_command": checks.DEFAULT_TEST}
		path = self.write_request(self.root / "request.toml", request)
		code, output, error = self.invoke(cli.main, ["run", path])
		self.assertEqual((code, error), (0, ""))
		execution = Run.load(next((self.hub / "runs").iterdir()).name)
		self.assertEqual(execution.status, "승인 필요")
		self.assertEqual(output, cli.report(execution) + "\n")
		self.assert_outputs(execution, output)
		self.assertEqual(self.invoke(cli.main, ["status", execution.run_id]), (0, output, ""))
		code, applied, error = self.invoke(cli.main, ["apply", execution.run_id])
		self.assertEqual((code, error), (0, ""))
		execution = Run.load(execution.run_id)
		self.assertEqual(execution.status, "완료")
		self.assertEqual(applied, cli.report(execution) + "\n")
		self.assert_outputs(execution, applied)
		self.assertEqual(self.invoke(cli.main, ["status", execution.run_id]), (0, applied, ""))
		self.assertEqual(self.invoke(cli.main, ["resume", execution.run_id]),
			(0, f"{execution.run_id} 는 이미 끝났습니다: 완료\n", ""))
		code, output, error = self.invoke(cli.main, ["apply", execution.run_id])
		self.assertEqual((code, output), (1, ""))
		self.assertIn("반영할 수 없는 상태", error)

	def test_plain_call_limit_then_resume_keep_terminal_output_and_exit_codes(self):
		request = {"repo": str(self.repo), "goal": "합성 값 변경", "done": ["VALUE 가 2"],
			"owned_paths": ["src/*"], "test_command": checks.DEFAULT_TEST}
		path = self.write_request(self.root / "request.toml", request, calls=1)
		code, output, error = self.invoke(cli.main, ["run", path])
		self.assertEqual((code, error), (1, ""))
		execution = Run.load(next((self.hub / "runs").iterdir()).name)
		self.assertEqual(execution.status, "호출 한도 소진")
		self.assertEqual(output, cli.report(execution) + "\n")
		self.assert_outputs(execution, output)
		code, output, error = self.invoke(cli.main, ["resume", execution.run_id, "--calls", "1"])
		self.assertEqual((code, error), (0, ""))
		resumed = Run.load(execution.run_id)
		self.assertEqual((resumed.status, len(resumed.calls)), ("승인 필요", 2))
		self.assertEqual(output, cli.report(resumed) + "\n")
		self.assert_outputs(resumed, output)

	def test_plain_failed_run_and_resume_each_print_once_and_keep_failure_codes(self):
		request = {"repo": str(self.repo), "goal": "합성 값 변경", "done": ["VALUE 가 2"],
			"owned_paths": ["src/*"], "test_command": checks.DEFAULT_TEST}
		path = self.write_request(self.root / "request.toml", request)
		self.fake = FakeAI([], fail_tools=("codex",))
		code, output, error = self.invoke(cli.main, ["run", path])
		self.assertEqual((code, error), (1, ""))
		execution = Run.load(next((self.hub / "runs").iterdir()).name)
		for command in (None, ["resume", execution.run_id]):
			with self.subTest(command=command):
				if command:
					code, output, error = self.invoke(cli.main, command)
					execution = Run.load(execution.run_id)
				self.assertEqual((code, error, execution.status), (1, "", "실패"))
				self.assertEqual(output, cli.report(execution) + "\n")
				self.assert_outputs(execution, output)


if __name__ == "__main__":
	unittest.main()
