"""오케스트레이션 실행기 테스트. 가짜 AI(호출 함수 대역)와 임시 Git 저장소만 쓴다. 실제 AI는 부르지 않는다."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector import vault
from collector.config import Plan, Plans, load_plans
from orchestrator import __main__ as cli, checks, targets
from orchestrator.flow import Flow
from orchestrator.providers import ProviderError, Reply, extract_json
from orchestrator.record import Run, new_run_id
from test_orchestrator_targets import init_repo, write_repos

PLANS = """
[[plans]]
id = "codex-personal"
tool = "codex"
owner = "personal"
allow = ["personal"]

[[plans]]
id = "claude-personal"
tool = "claude"
owner = "personal"
allow = ["personal"]

[[plans]]
id = "agy-personal"
tool = "agy"
owner = "personal"
allow = ["personal"]

[routing]
personal = ["codex-personal"]

[orchestration]
implementer = ["codex-personal"]
reviewer = ["claude-personal", "agy-personal"]
"""

PASS = {"verdict": "pass", "summary": "문제 없음", "findings": []}
CHANGES = {"verdict": "changes", "summary": "고칠 것", "findings": [{"id": "F1", "severity": "major", "file": "src/a.py",
	"line": 1, "problem": "값이 틀림", "evidence": "테스트 기대값", "suggestion": "2로 바꾼다"}]}


def sh(cwd, *args):
	subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class FakeAI:
	"""역할별로 정해 둔 응답을 순서대로 돌려준다. 구현 담당은 작업 폴더에 실제로 파일을 쓴다."""

	def __init__(self, reviews, writes=None, fail_tools=()):
		self.reviews, self.writes, self.fail_tools, self.log = list(reviews), writes or {}, fail_tools, []

	def __call__(self, plan, writer, prompt, cwd, schema, timeout):
		self.log.append((plan.id, writer))
		if plan.tool in self.fail_tools:
			raise ProviderError("exit", f"{plan.id} 종료 코드 1 (원인 미분류)")
		if "verdict" in schema["properties"]:
			return Reply(self.reviews.pop(0), 0.1, "미측정")
		if "document" in schema["properties"]:
			return Reply({"summary": "설계", "document": "# 설계안\n내용", "blockers": []}, 0.1, "미측정")
		for name, text in self.writes.items():
			(cwd / name).parent.mkdir(parents=True, exist_ok=True)
			(cwd / name).write_text(text, encoding="utf-8")
		return Reply({"summary": "구현", "changed_files": list(self.writes), "verification": ["테스트"],
			"blockers": [], "next_required_step": ""}, 0.1, {"input_tokens": 10})


class OrchestratorTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		root = Path(self.tmp.name).resolve()
		self.home, self.repo, self.vault = root / "hub", root / "repo", root / "vault"
		(self.home / "redaction").mkdir(parents=True)
		(self.home / "redaction" / "dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "⟦회사명-1⟧"}]}), encoding="utf-8")
		(self.home / "plans.toml").write_text(PLANS, encoding="utf-8")
		(self.home / "sources.toml").write_text(f'vault = "{self.vault}"\n', encoding="utf-8")
		for folder in vault.landing_dirs(self.vault):
			folder.mkdir(parents=True)
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.home)})
		self.env.start()
		sh(root, "git", "init", "-q", "-b", "main", str(self.repo))
		for key, value in (("user.name", "test"), ("user.email", "test@localhost")):
			sh(self.repo, "git", "config", key, value)
		(self.repo / "src").mkdir()
		(self.repo / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
		(self.repo / "tests").mkdir()
		(self.repo / "tests" / "test_a.py").write_text(
			"import unittest\nclass T(unittest.TestCase):\n\tdef test_ok(self):\n\t\tself.assertTrue(True)\n", encoding="utf-8")
		sh(self.repo, "git", "add", "src/a.py", "tests/test_a.py")
		sh(self.repo, "git", "commit", "-q", "-m", "시작")

	def tearDown(self):
		self.env.stop()
		self.tmp.cleanup()

	def new_run(self, kind="code", **budget):
		request = {"goal": "값을 2로", "done": ["VALUE 가 2"], "owned_paths": ["src/*"], "commit_message": "값을 2로 바꾼다"}
		run = Run(run_id=new_run_id(), request=request, repo=str(self.repo), kind=kind)
		run.budget.update(budget)
		run.save()
		return run

	def flow(self, run, fake):
		return Flow(run, load_plans(), cli.orchestration_config(), caller=fake).run_all()

	def approved(self, changes):
		run = self.new_run()
		Flow(run, load_plans(), cli.orchestration_config()).prepare()
		for name, value in changes.items():
			path = Path(run.worktree) / name
			if value is None:
				path.unlink()
			else:
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(value)
		sh(run.worktree, "git", "add", "--", *changes)
		sh(run.worktree, "git", "commit", "-q", "-m", "합성 변경")
		run.finish("승인 필요", "합성 검토 통과")
		return run

	def snapshot(self):
		return (checks.git(self.repo, "rev-parse", "HEAD"), (self.repo / ".git/index").read_bytes(),
			{str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
				if path.is_file() and ".git" not in path.relative_to(self.repo).parts})

	def test_pass_reaches_approval_then_apply_makes_one_commit(self):
		run = self.flow(self.new_run(), FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "승인 필요")
		self.assertTrue(run.approval_pending)
		self.assertEqual([c["role"] for c in run.calls], ["implement", "review"])
		with mock.patch.object(cli, "finish_outputs"):
			self.assertEqual(cli.cmd_apply(run.run_id), 0)
		log = subprocess.run(["git", "log", "--format=%s"], cwd=self.repo, capture_output=True, text=True).stdout.split("\n")
		self.assertEqual(log[0], "값을 2로 바꾼다")
		self.assertEqual((self.repo / "src" / "a.py").read_text(encoding="utf-8"), "VALUE = 2\n")
		self.assertFalse(Path(run.worktree).exists())

	def test_read_request_validates_optional_base_branch_with_git(self):
		path = self.repo.parent / "request.toml"
		request = '[request]\ngoal="합성 작업"\ndone=["완료"]\nowned_paths=["src/*"]\n'
		path.write_text(request, encoding="utf-8")
		self.assertNotIn("base_branch", cli.read_request(str(path))["request"])
		for branch in ("agency/project", "agency/합성"):
			with self.subTest(branch=branch):
				path.write_text(request + "base_branch=" + json.dumps(branch, ensure_ascii=False), encoding="utf-8")
				self.assertEqual(cli.read_request(str(path))["request"]["base_branch"], branch)
		for value in ('42', 'true', '["agency/project"]', '{}', '""', '"bad name"', '"bad..name"',
				'"agency//bad"', '"../bad"', '"bad.lock"', '"bad@{name"', '"-bad"', '"HEAD"', '"bad\\u0000name"'):
			with self.subTest(value=value):
				path.write_text(request + "base_branch=" + value, encoding="utf-8")
				with self.assertRaisesRegex(ValueError, "^base_branch"):
					cli.read_request(str(path))

	def test_apply_refuses_project_branch_run_without_mutation(self):
		run = self.approved({"src/a.py": "VALUE = 2\n"})
		run.request["base_branch"] = "agency/project"
		run.save()
		before = self.snapshot()
		with mock.patch.object(cli, "finish_outputs") as finish:
			with self.assertRaisesRegex(RuntimeError, "^프로젝트 가지 기준 실행은 apply <run_id> --onto <가지> 로만 반영합니다$"):
				cli.cmd_apply(run.run_id)
			finish.assert_not_called()
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(Run.load(run.run_id).status, "승인 필요")
		self.assertTrue(Path(run.worktree).exists())

	def test_apply_rejects_dirty_modified_deleted_untracked_and_directory_collisions_without_mutation(self):
		cases = [("src/a.py", "추가 합성 내용", "수정"), ("src/a.py", None, "삭제"),
			("src/new file.py", "합성 새 파일", "추적 안 됨"), ("src/새\n파일.py", "합성 줄바꿈 파일", "추적 안 됨"),
			("src/new.pyc", "합성 캐시", "추적 안 됨"), ("src/newdir/file.py", "합성 파일", "상위 파일"),
			("src/newfile", "합성 파일", "하위 파일")]
		for name, value, kind in cases:
			with self.subTest(kind=kind, name=name):
				run = self.approved({name: value if value is not None else "브랜치의 변경"})
				path = self.repo / name
				if kind == "삭제":
					path.unlink()
				elif kind == "상위 파일":
					path.parent.write_text("상위 합성 파일")
				elif kind == "하위 파일":
					path.mkdir()
					(path / "local.txt").write_text("하위 합성 파일")
				else:
					path.write_text("보존할 미커밋 내용")
				before = self.snapshot()
				with mock.patch.object(cli, "finish_outputs") as finish:
					with self.assertRaisesRegex(RuntimeError, "^작업 중인 파일과 겹쳐 반영하지 않습니다: 1개$"):
						cli.cmd_apply(run.run_id)
					finish.assert_not_called()
				self.assertEqual(self.snapshot(), before)
				self.assertEqual(Run.load(run.run_id).status, "승인 필요")
				if kind == "상위 파일":
					path.parent.unlink()
				elif kind == "하위 파일":
					(path / "local.txt").unlink()
					path.rmdir()
				elif name == "src/a.py":
					path.write_text("VALUE = 1\n")
				else:
					path.unlink()

	def test_apply_keeps_unrelated_tracked_and_untracked_changes_and_staged_refusal(self):
		run = self.approved({"src/a.py": "VALUE = 2\n"})
		path = self.repo / "tests/test_a.py"
		path.write_text("보존할 테스트 수정")
		untracked = self.repo / "notes.txt"
		untracked.write_text("보존할 새 파일")
		sh(self.repo, "git", "add", "tests/test_a.py")
		before = self.snapshot()
		with self.assertRaisesRegex(RuntimeError, "스테이징된 변경"):
			cli.cmd_apply(run.run_id)
		self.assertEqual(self.snapshot(), before)
		sh(self.repo, "git", "reset", "-q", "HEAD", "--", "tests/test_a.py")
		with mock.patch.object(cli, "finish_outputs"):
			self.assertEqual(cli.cmd_apply(run.run_id), 0)
		self.assertEqual(path.read_text(), "보존할 테스트 수정")
		self.assertEqual(untracked.read_text(), "보존할 새 파일")
		self.assertEqual((self.repo / "src/a.py").read_text(), "VALUE = 2\n")
		self.assertEqual(checks.git(self.repo, "diff", "--cached", "--name-only"), "")

	def test_target_trailer_only_for_successful_claude_implementation_and_restart_last_line(self):
		other = init_repo(self.repo.parent / "other")
		patch = mock.patch.object(targets, "REPO", other)
		patch.start()
		self.addCleanup(patch.stop)
		row = {"id": "blog-x", "path": str(self.repo), "test_command": checks.DEFAULT_TEST,
			"claude_trailer": "합성 Claude 서명", "restart_paths": ["src/"], "restart_notice": "사용자 승인 회차에 재기동"}
		calls = [([{"role": "implement", "tool": "claude", "ok": True}], True),
			([{"role": "implement", "tool": "codex", "ok": True}, {"role": "review", "tool": "claude", "ok": True}], False),
			([{"role": "implement", "tool": "claude", "ok": False}, {"role": "implement", "tool": "codex", "ok": True}], False),
			([{"role": "implement", "tool": "claude", "ok": None}], False)]
		for number, (history, expected) in enumerate(calls):
			with self.subTest(history=history):
				write_repos(self.home, row)
				run = self.approved({"src/a.py": f"VALUE = {number + 2}\n"})
				run.calls = history
				run.save()
				with mock.patch.object(cli, "finish_outputs", side_effect=lambda _: print("합성 보고서")), \
						contextlib.redirect_stdout(io.StringIO()) as output:
					self.assertEqual(cli.cmd_apply(run.run_id), 0)
				message = checks.git(self.repo, "log", "-1", "--format=%B").rstrip()
				self.assertEqual(message, run.request["commit_message"] + ("\n\n" + row["claude_trailer"] if expected else ""))
				self.assertEqual(output.getvalue().splitlines()[-1], "재기동 필요: " + row["restart_notice"])
		write_repos(self.home, {**row, "claude_trailer": ""})
		run = self.approved({"tests/test_a.py": "합성 테스트 변경"})
		run.calls = calls[0][0]
		run.save()
		with mock.patch.object(cli, "finish_outputs"), contextlib.redirect_stdout(io.StringIO()) as output:
			cli.cmd_apply(run.run_id)
		self.assertEqual(output.getvalue(), "")
		self.assertEqual(checks.git(self.repo, "log", "-1", "--format=%B").strip(), run.request["commit_message"])

	def test_major_finding_is_fixed_then_rechecked(self):
		run = self.flow(self.new_run(), FakeAI([CHANGES, PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "승인 필요")
		self.assertEqual(run.fixes, 1)
		self.assertEqual([c["role"] for c in run.calls], ["implement", "review", "implement", "review"])

	def test_call_limit_stops_new_calls(self):
		run = self.flow(self.new_run(calls=2), FakeAI([CHANGES, CHANGES], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "호출 한도 소진")
		self.assertIn("--calls", run.end_reason)
		self.assertEqual(len(run.calls), 2)

	def test_timeout_at_end_of_time_limit_is_reported_as_time_over(self):
		run = self.new_run(seconds=1)

		def slow(*_args):
			run.elapsed = run.budget["seconds"]  # 호출하는 사이 시간을 다 쓴 것처럼 만든다
			raise ProviderError("timeout", "시간 초과")
		result = self.flow(run, slow)
		self.assertEqual(result.status, "시간 초과")
		self.assertIn("--minutes", result.end_reason)

	def test_reviewer_failure_falls_back_once(self):
		fake = FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}, fail_tools=("claude",))
		run = self.flow(self.new_run(), fake)
		self.assertEqual(run.status, "승인 필요")
		self.assertEqual([c["plan"] for c in run.calls], ["codex-personal", "claude-personal", "agy-personal"])
		self.assertFalse(run.calls[1]["ok"])

	def test_outside_path_and_company_name_fail_machine_checks(self):
		fake = FakeAI([PASS, PASS, PASS], {"src/a.py": "# acme\nVALUE = 2\n", "README.md": "밖"})
		run = self.flow(self.new_run(), fake)
		self.assertEqual(run.status, "실패")  # 보완 2회 뒤에도 기계 검증이 실패
		failed = {c["name"] for c in run.checks if not c["ok"]}
		self.assertIn("허용 경로", failed)
		self.assertIn("회사·서비스 이름", failed)
		self.assertNotIn("acme", json.dumps(run.checks))

	def test_name_check_looks_only_at_lines_added_by_the_change(self):
		(self.repo / "src" / "b.py").write_text("# acme 이미 있던 이름\nVALUE = 1\n", encoding="utf-8")
		sh(self.repo, "git", "add", "src/b.py")
		sh(self.repo, "git", "commit", "-q", "-m", "기존 파일")
		base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
		(self.repo / "src" / "b.py").write_text("# acme 이미 있던 이름\nVALUE = 2\n", encoding="utf-8")
		sh(self.repo, "git", "commit", "-q", "-am", "값만 바꾼다")
		results = {r["name"]: r["ok"] for r in checks.verify(self.repo, base, {"test_command": "true"}, [])}
		self.assertTrue(results["회사·서비스 이름"])
		(self.repo / "src" / "b.py").write_text("# acme 이미 있던 이름\nVALUE = 2  # acme 새로 넣음\n", encoding="utf-8")
		sh(self.repo, "git", "commit", "-q", "-am", "이름을 새로 넣는다")
		results = {r["name"]: r["ok"] for r in checks.verify(self.repo, base, {"test_command": "true"}, [])}
		self.assertFalse(results["회사·서비스 이름"])

	def test_interrupted_call_counts_and_resume_continues(self):
		run = self.new_run(calls=2)
		self.flow(run, FakeAI([CHANGES], {"src/a.py": "VALUE = 2\n"}))  # 예산으로 보완 단계 앞에서 멈춘다
		saved = Run.load(run.run_id)
		saved.calls.append({"role": "implement", "plan": "codex-personal", "tool": "codex", "ok": None})  # 끊긴 호출
		saved.status, saved.step, saved.budget["calls"] = "작업 중", "fix", 8
		saved.save()
		resumed = self.flow(Run.load(run.run_id), FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(resumed.status, "승인 필요")
		self.assertEqual(resumed.calls[2]["error"], "중단됨(결과 미확인)")
		self.assertEqual(len(resumed.calls), 5)

	def test_analysis_keeps_document_and_writes_nothing_to_repo(self):
		run = self.flow(self.new_run(kind="analysis"), FakeAI([PASS]))
		self.assertEqual(run.status, "승인 필요")
		self.assertIn("설계안", (run.folder / "document.md").read_text(encoding="utf-8"))
		status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
		self.assertEqual(status.strip(), "")

	def test_run_command_writes_report_events_and_journal(self):
		request = self.repo.parent / "req.toml"
		request.write_text(f'[request]\nrepo = "{self.repo}"\ngoal = "값을 2로"\ndone = ["VALUE 가 2"]\n'
			'owned_paths = ["src/*"]\nkind = "code"\n\n[budget]\ncalls = 4\nminutes = 5\n', encoding="utf-8")
		with mock.patch("orchestrator.flow.call", FakeAI([PASS], {"src/a.py": "VALUE = 2\n"})), \
				mock.patch("builtins.print"):
			self.assertEqual(cli.main(["run", str(request)]), 0)
		run_id = sorted(p.name for p in (self.home / "runs").iterdir())[-1]
		folder = self.home / "runs" / run_id
		self.assertIn("승인 필요", (folder / "report.md").read_text(encoding="utf-8"))
		events = [json.loads(line)["event"] for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
		self.assertEqual(events[0], "start")
		self.assertEqual(events[-1], "finish")
		journal = next((self.vault / "📥 Inbox" / "AI").glob("*.md")).read_text(encoding="utf-8")
		self.assertIn(f"run:{run_id}", journal)

	def hook(self, body):
		path = self.repo / ".git" / "hooks" / "pre-commit"
		path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
		path.chmod(0o755)

	def test_commit_hook_rejection_goes_to_fix_with_hook_output(self):
		marker = Path(self.tmp.name) / "hook-ran"
		self.hook(f'if [ ! -f "{marker}" ]; then touch "{marker}"; echo "합성 검사 실패: 줄 끝 공백"; exit 1; fi\n')
		run = self.flow(self.new_run(), FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "승인 필요")
		self.assertEqual(run.fixes, 1)
		events = [json.loads(line)["event"] for line in (run.folder / "events.jsonl").read_text().splitlines()]
		self.assertIn("commit_rejected", events)

	def test_commit_hook_rejection_without_fix_budget_fails_with_reason(self):
		self.hook('echo "합성 검사 실패: 규칙 위반"; exit 1\n')
		run = self.flow(self.new_run(fixes=0), FakeAI([], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "실패")
		self.assertIn("합성 검사 실패: 규칙 위반", run.end_reason)
		self.assertIn("합성 검사 실패", run.open_findings[0]["evidence"])

	def test_unexpected_step_error_is_recorded_as_failure(self):
		with mock.patch.object(checks, "verify", side_effect=RuntimeError("합성 오류")):
			run = self.flow(self.new_run(), FakeAI([PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "실패")
		self.assertIn("verify 단계 오류: RuntimeError: 합성 오류", run.end_reason)
		self.assertEqual(Run.load(run.run_id).status, "실패")

	def test_extract_json_accepts_fenced_and_rejects_garbage(self):
		self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
		self.assertEqual(extract_json('앞말 {"a": 2} 뒷말'), {"a": 2})
		with self.assertRaises(ProviderError):
			extract_json("JSON 없음")


class PlanSelectionTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.root / "hub")})
		patch.start()
		self.addCleanup(patch.stop)
		self.ids = ["p01", "p02", "p03", "p04"]
		self.plans = Plans({pid: Plan(pid, "agy", "합성", "personal", ("personal",), None, None) for pid in self.ids}, {})

	def engine(self, caller, **budget):
		run = Run(new_run_id(), {"goal": "합성 분석", "done": [], "owned_paths": []}, str(self.root),
			kind="analysis", worktree=str(self.root))
		run.budget.update(budget)
		run.save()
		return Flow(run, self.plans, {"implementer": self.ids, "reviewer": self.ids}, caller=caller)

	def test_third_plan_is_called_after_two_failures_for_each_role(self):
		for role in ("implement", "review"):
			with self.subTest(role=role):
				caller = mock.Mock(side_effect=[ProviderError("exit", "합성 실패")] * 2 + [Reply(PASS, .1, {})])
				engine = self.engine(caller)
				self.assertEqual(engine.ask(role, "질문", {}, False), PASS)
				self.assertEqual([c.args[0].id for c in caller.call_args_list], self.ids[:3])
				self.assertEqual([c["ok"] for c in engine.run.calls], [False, False, True])
				self.assertEqual(engine.run.budget["calls"], 8)

	def test_fourth_plan_is_not_tried_after_three_failures(self):
		for role in ("implement", "review"):
			with self.subTest(role=role):
				caller = mock.Mock(side_effect=ProviderError("exit", "합성 실패"))
				engine = self.engine(caller)
				self.assertIsNone(engine.ask(role, "질문", {}, False))
				self.assertEqual([c.args[0].id for c in caller.call_args_list], self.ids[:3])
				self.assertEqual(engine.run.status, "실패")

	def test_call_and_time_budgets_stop_before_third_attempt(self):
		for limit, expected in (("calls", "호출 한도 소진"), ("seconds", "시간 초과")):
			with self.subTest(limit=limit):
				def fail(*args):
					if limit == "seconds" and len(engine.run.calls) == 2:
						engine.run.elapsed = engine.run.budget["seconds"]
					raise ProviderError("exit", "합성 실패")
				caller = mock.Mock(side_effect=fail)
				engine = self.engine(caller, **({"calls": 2} if limit == "calls" else {}))
				self.assertIsNone(engine.ask("implement", "질문", {}, True))
				self.assertEqual(caller.call_count, 2)
				self.assertEqual(engine.run.status, expected)

	def test_review_skips_all_successful_implementers_before_try_limit(self):
		caller = mock.Mock(side_effect=[ProviderError("exit", "합성 실패")] * 2 + [Reply(PASS, .1, {})])
		engine = self.engine(caller)
		engine.run.calls = [{"role": "implement", "plan": "p01", "ok": True}]
		self.assertEqual(engine.ask("review", "질문", {}, False), PASS)
		self.assertEqual([c.args[0].id for c in caller.call_args_list], self.ids[1:])
		events = [json.loads(line) for line in (engine.run.folder / "events.jsonl").read_text().splitlines()]
		self.assertEqual([e["plan"] for e in events if e["event"] == "review_skip_same_plan"], ["p01"])
		self.assertEqual(len(engine.run.calls), 4)

	def test_failed_pending_and_review_calls_do_not_exclude_plans(self):
		caller = mock.Mock(return_value=Reply(PASS, .1, {}))
		engine = self.engine(caller)
		engine.run.calls = [{"role": "implement", "plan": "p01", "ok": False},
			{"role": "implement", "plan": "p01", "ok": None}, {"role": "review", "plan": "p01", "ok": True}]
		self.assertEqual(engine.ask("review", "질문", {}, False), PASS)
		self.assertEqual(caller.call_args.args[0].id, "p01")
		self.assertNotIn("review_skip_same_plan", (engine.run.folder / "events.jsonl").read_text())

	def test_empty_review_list_after_skips_finishes_run_as_failed(self):
		caller = mock.Mock()
		engine = self.engine(caller)
		engine.run.calls = [{"role": "implement", "plan": pid, "ok": True} for pid in self.ids]
		engine.run.step, engine.run.document = "review", "합성 문서"
		result = engine.run_all()
		caller.assert_not_called()
		self.assertEqual(result.status, "실패")
		self.assertIn("review 담당 호출이 모두 실패했습니다", result.end_reason)
		self.assertEqual(len(result.calls), 4)
		events = [json.loads(line) for line in (result.folder / "events.jsonl").read_text().splitlines()]
		self.assertEqual([e["plan"] for e in events if e["event"] == "review_skip_same_plan"], self.ids)
		self.assertEqual(Run.load(result.run_id).status, "실패")


if __name__ == "__main__":
	unittest.main()
