"""오케스트레이션 실행기 테스트. 가짜 AI(호출 함수 대역)와 임시 Git 저장소만 쓴다. 실제 AI는 부르지 않는다."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import vault
from collector.config import load_plans
from orchestrator import __main__ as cli
from orchestrator.flow import Flow
from orchestrator.providers import ProviderError, Reply, extract_json
from orchestrator.record import Run, new_run_id

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
		root = Path(self.tmp.name)
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

	def test_major_finding_is_fixed_then_rechecked(self):
		run = self.flow(self.new_run(), FakeAI([CHANGES, PASS], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "승인 필요")
		self.assertEqual(run.fixes, 1)
		self.assertEqual([c["role"] for c in run.calls], ["implement", "review", "implement", "review"])

	def test_budget_stops_new_calls(self):
		run = self.flow(self.new_run(calls=2), FakeAI([CHANGES, CHANGES], {"src/a.py": "VALUE = 2\n"}))
		self.assertEqual(run.status, "예산 소진")
		self.assertEqual(len(run.calls), 2)

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
