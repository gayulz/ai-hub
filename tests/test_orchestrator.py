"""오케스트레이션 실행기 테스트. 가짜 AI(호출 함수 대역)와 임시 Git 저장소만 쓴다. 실제 AI는 부르지 않는다."""

import json
import os
import subprocess
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

from collector import vault
from collector.config import load_plans
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
			raise ProviderError("exit", f"{plan.id} 종료 코드 1")
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

	def test_extract_json_accepts_fenced_and_rejects_garbage(self):
		self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
		self.assertEqual(extract_json('앞말 {"a": 2} 뒷말'), {"a": 2})
		with self.assertRaises(ProviderError):
			extract_json("JSON 없음")


if __name__ == "__main__":
	unittest.main()
