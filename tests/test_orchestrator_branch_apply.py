"""임시 Git 저장소와 가짜 AI로 기준 가지 실행·반영·본 작업 보존을 검사한다."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import vault
from collector.config import load_plans
from orchestrator import __main__ as cli, checks, targets
from orchestrator.branch_apply import apply_to_branch
from orchestrator.flow import Flow
from orchestrator.record import Run, new_run_id
from tests.test_orchestrator import FakeAI, PASS, PLANS
from tests.test_orchestrator_targets import init_repo

BRANCH = "agency/project"
REF = "refs/heads/" + BRANCH
MOVED = "기준 가지가 실행 뒤에 바뀌어 반영하지 않습니다"


class BranchApplyTest(unittest.TestCase):
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
		self.enterContext(mock.patch("orchestrator.test_isolation.requires", side_effect=lambda path: Path(path).resolve() != self.repo.resolve()))
		(self.repo / "src").mkdir()
		(self.repo / "src/a.py").write_text("VALUE = 1\n", encoding="utf-8")
		(self.repo / "tests").mkdir()
		(self.repo / "tests/test_a.py").write_text(
			"import unittest\nfrom src.a import VALUE\nclass T(unittest.TestCase):\n"
			"\tdef test_value(self):\n\t\tself.assertGreater(VALUE, 0)\n", encoding="utf-8")
		self.git("add", "--", "src/a.py", "tests/test_a.py")
		self.git("commit", "-q", "-m", "시작")
		self.git("checkout", "-q", "-b", BRANCH)
		(self.repo / "src/a.py").write_text("VALUE = 10\n", encoding="utf-8")
		(self.repo / "project.txt").write_text("앞 프로젝트 작업", encoding="utf-8")
		self.git("add", "--", "src/a.py", "project.txt")
		self.git("commit", "-q", "-m", "프로젝트의 앞 작업")
		self.base = self.git("rev-parse", "HEAD")
		self.git("checkout", "-q", "main")
		(self.repo / "src/a.py").write_text("VALUE = 20\n", encoding="utf-8")
		(self.repo / "main.txt").write_text("본 가지의 다른 작업", encoding="utf-8")
		self.git("add", "--", "src/a.py", "main.txt")
		self.git("commit", "-q", "-m", "본 가지의 다른 작업")
		self.head = self.git("rev-parse", "HEAD")

	def git(self, *args, **kwargs):
		return checks.git(self.repo, *args, **kwargs).strip()

	def new_run(self):
		request = {"goal": "합성 값 변경", "done": ["새 값 반영"], "owned_paths": ["src/*"],
			"test_command": f"{sys.executable} -m unittest discover -s tests",
			"base_branch": BRANCH, "commit_message": "프로젝트 값 변경"}
		run = Run(new_run_id(), request, str(self.repo))
		run.save()
		return run

	def flow(self, run, caller=None):
		return Flow(run, load_plans(), cli.orchestration_config(), caller=caller)

	def approved(self, change=True):
		run = self.new_run()
		self.flow(run).prepare()
		if change:
			for _ in range(2):
				path = Path(run.worktree) / "src/a.py"
				path.write_text(path.read_text(encoding="utf-8") + "VALUE += 1\n", encoding="utf-8")
				checks.git(run.worktree, "add", "--", "src/a.py")
				checks.git(run.worktree, "commit", "-q", "-m", "합성 구현")
		else:
			checks.git(run.worktree, "commit", "-q", "--allow-empty", "-m", "합성 빈 커밋")
		run.finish("승인 필요", "합성 검토 통과")
		return run

	def snapshot(self):
		return (self.git("rev-parse", "HEAD"), (self.repo / ".git/HEAD").read_bytes(),
			(self.repo / ".git/index").read_bytes(),
			{str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
				if path.is_file() and ".git" not in path.relative_to(self.repo).parts})

	def moved_commit(self):
		return self.git("commit-tree", self.git("rev-parse", self.base + "^{tree}"),
			"-p", self.base, "-m", "다른 실행의 변경")

	def assert_pending(self, run):
		self.assertEqual(Run.load(run.run_id).status, "승인 필요")
		self.assertTrue(Path(run.worktree).exists())
		self.assertTrue(self.git("show-ref", "--verify", "refs/heads/" + run.branch))

	def test_prepare_starts_at_project_tip_when_main_has_a_different_commit(self):
		run = self.new_run()
		before = self.snapshot()
		self.flow(run).prepare()
		self.assertNotEqual(self.base, self.head)
		self.assertEqual(run.base_commit, self.base)
		self.assertEqual(checks.git(run.worktree, "rev-parse", "HEAD").strip(), self.base)
		self.assertEqual((Path(run.worktree) / "src/a.py").read_text(), "VALUE = 10\n")
		self.assertTrue((Path(run.worktree) / "project.txt").is_file())
		self.assertFalse((Path(run.worktree) / "main.txt").exists())
		self.assertEqual(self.snapshot(), before)

	def test_prepare_without_base_branch_keeps_head_behavior(self):
		run = self.new_run()
		run.request.pop("base_branch")
		self.flow(run).prepare()
		self.assertEqual(run.base_commit, self.head)
		self.assertEqual(checks.git(run.worktree, "rev-parse", "HEAD").strip(), self.head)

	def test_missing_branch_fails_even_when_a_tag_with_that_name_exists(self):
		self.git("tag", "missing", self.base)
		run = self.new_run()
		run.request["base_branch"] = "missing"
		caller = mock.Mock(side_effect=AssertionError("실제 AI 호출 금지"))
		before = self.snapshot()
		self.flow(run, caller).run_all()
		self.assertEqual((run.status, run.end_reason), ("실패", "기준 가지가 없습니다"))
		self.assertEqual(Run.load(run.run_id).status, "실패")
		self.assertEqual(run.worktree, "")
		self.assertFalse((self.hub / "run/worktrees" / run.run_id).exists())
		self.assertEqual(self.git("show-ref", "--verify", "refs/heads/orch/" + run.run_id, check=False), "")
		caller.assert_not_called()
		self.assertEqual(self.snapshot(), before)

	def test_read_request_refuses_previous_checkout_shorthand_as_a_branch_name(self):
		path = self.repo / "request.toml"
		path.write_text('[request]\ngoal="합성 작업"\ndone=["완료"]\n'
			'owned_paths=["src/*"]\nbase_branch="@{-1}"\n', encoding="utf-8")
		self.assertEqual(self.git("check-ref-format", "--branch", "@{-1}"), BRANCH)
		with self.assertRaisesRegex(ValueError, "^base_branch"):
			cli.read_request(str(path))

	def test_sequential_fake_ai_runs_start_at_each_previous_delivery(self):
		previous = self.base
		before = self.snapshot()
		for value in (2, 3):
			with self.subTest(value=value):
				run = self.flow(self.new_run(), FakeAI([PASS], {"src/a.py": f"VALUE = {value}\n"})).run_all()
				self.assertEqual(run.status, "승인 필요")
				self.assertEqual(run.base_commit, previous)
				self.assertTrue(all(check["ok"] for check in run.checks))
				with contextlib.redirect_stdout(io.StringIO()):
					commit = apply_to_branch(run.run_id, BRANCH)
				self.assertEqual(self.git("rev-list", "--parents", "-n", "1", commit).split(), [commit, previous])
				self.assertEqual(self.git("rev-parse", REF), commit)
				previous = commit
		self.assertEqual(self.snapshot(), before)

	def test_apply_makes_one_commit_with_run_tree_and_preserves_dirty_main(self):
		run = self.approved()
		tree = self.git("rev-parse", run.branch + "^{tree}")
		self.assertEqual(self.git("rev-list", "--count", self.base + ".." + run.branch), "2")
		(self.repo / "src/a.py").write_text("VALUE = 99\n", encoding="utf-8")
		self.git("add", "--", "src/a.py")
		(self.repo / "src/a.py").write_text("VALUE = 100\n", encoding="utf-8")
		(self.repo / "tests/test_a.py").unlink()
		(self.repo / "새 파일.txt").write_text("보존할 미커밋 파일", encoding="utf-8")
		before = self.snapshot()
		with contextlib.redirect_stdout(io.StringIO()) as output:
			commit = apply_to_branch(run.run_id, BRANCH)
		self.assertEqual(output.getvalue(), "")
		self.assertEqual(commit, self.git("rev-parse", REF))
		self.assertEqual(self.git("rev-list", "--parents", "-n", "1", commit).split(), [commit, self.base])
		self.assertEqual(self.git("rev-parse", commit + "^{tree}"), tree)
		self.assertEqual(self.git("rev-list", "--count", self.base + ".." + REF), "1")
		self.assertEqual(self.snapshot(), before)
		self.assertFalse(Path(run.worktree).exists())
		self.assertEqual(self.git("show-ref", "--verify", "refs/heads/" + run.branch, check=False), "")
		saved = Run.load(run.run_id)
		self.assertEqual((saved.status, saved.approval_pending), ("완료", False))
		self.assertEqual(saved.end_reason, f"가지 {BRANCH}에 반영 {commit[:12]}(push 안 함)")
		self.assertIn(saved.end_reason, (run.folder / "report.md").read_text(encoding="utf-8"))
		journal = next((self.vault / "📥 Inbox/AI").glob("*.md")).read_text(encoding="utf-8")
		self.assertIn(saved.end_reason, journal)

	def test_apply_refuses_a_branch_moved_after_prepare(self):
		run = self.approved()
		moved = self.moved_commit()
		self.git("update-ref", REF, moved, self.base)
		before = self.snapshot()
		with mock.patch.object(cli, "finish_outputs") as finish, self.assertRaisesRegex(RuntimeError, "^" + MOVED + "$"):
			apply_to_branch(run.run_id, BRANCH)
		finish.assert_not_called()
		self.assertEqual(self.git("rev-parse", REF), moved)
		self.assertEqual(self.snapshot(), before)
		self.assert_pending(run)

	def test_update_ref_refuses_a_branch_moved_after_the_initial_check(self):
		run = self.approved()
		moved = self.moved_commit()
		real_git = checks.git
		before = self.snapshot()

		def race(cwd, *args, **kwargs):
			if args[:2] == ("update-ref", REF):
				real_git(cwd, "update-ref", REF, moved, self.base)
			return real_git(cwd, *args, **kwargs)

		with mock.patch.object(checks, "git", side_effect=race), self.assertRaisesRegex(RuntimeError, "^" + MOVED + "$"):
			apply_to_branch(run.run_id, BRANCH)
		self.assertEqual(self.git("rev-parse", REF), moved)
		self.assertEqual(self.snapshot(), before)
		self.assert_pending(run)

	def test_apply_refuses_another_branch_and_a_request_without_base_branch(self):
		run = self.approved()
		other = BRANCH + "-other"
		self.git("branch", other, self.base)
		before = self.snapshot()
		for branch in (other, BRANCH):
			with self.subTest(branch=branch):
				if branch == BRANCH:
					run.request.pop("base_branch")
					run.save()
				with self.assertRaisesRegex(RuntimeError, "요청서의 기준 가지"):
					apply_to_branch(run.run_id, branch)
				self.assertEqual(self.git("rev-parse", REF), self.base)
				self.assertEqual(self.git("rev-parse", "refs/heads/" + other), self.base)
				self.assertEqual(self.snapshot(), before)
				self.assert_pending(run)

	def test_update_ref_failure_without_branch_movement_keeps_its_reason(self):
		run = self.approved()
		real_git = checks.git
		before = self.snapshot()

		def fail(cwd, *args, **kwargs):
			if args[:2] == ("update-ref", REF):
				raise RuntimeError("합성 잠금 실패")
			return real_git(cwd, *args, **kwargs)

		with mock.patch.object(checks, "git", side_effect=fail), self.assertRaisesRegex(RuntimeError, "^합성 잠금 실패$"):
			apply_to_branch(run.run_id, BRANCH)
		self.assertEqual(self.git("rev-parse", REF), self.base)
		self.assertEqual(self.snapshot(), before)
		self.assert_pending(run)

	def test_apply_refuses_non_code_and_non_approval_states(self):
		run = self.approved()
		before = self.snapshot()
		cases = [("analysis", "승인 필요")] + [("code", status) for status in
			("작업 중", "완료", "실패", "취소", "호출 한도 소진", "시간 초과", "사용자 입력 필요")]
		for kind, status in cases:
			with self.subTest(kind=kind, status=status):
				run.kind, run.status = kind, status
				run.save()
				with self.assertRaisesRegex(RuntimeError, "반영할 수 없는 상태"):
					apply_to_branch(run.run_id, BRANCH)
				self.assertEqual(self.git("rev-parse", REF), self.base)
				self.assertEqual(self.snapshot(), before)
				self.assertEqual(Run.load(run.run_id).status, status)
				self.assertTrue(Path(run.worktree).exists())

	def test_apply_refuses_a_different_commit_with_the_same_tree(self):
		run = self.approved(change=False)
		self.assertNotEqual(self.git("rev-parse", run.branch), self.base)
		before = self.snapshot()
		with self.assertRaisesRegex(RuntimeError, "^합칠 변경이 없습니다$"):
			apply_to_branch(run.run_id, BRANCH)
		self.assertEqual(self.git("rev-parse", REF), self.base)
		self.assertEqual(self.snapshot(), before)
		self.assert_pending(run)

	def test_apply_refuses_the_checked_out_base_branch_to_preserve_head(self):
		run = self.approved()
		self.git("checkout", "-q", BRANCH)
		before = self.snapshot()
		with self.assertRaisesRegex(RuntimeError, "현재 작업 폴더에서 사용 중인 기준 가지"):
			apply_to_branch(run.run_id, BRANCH)
		self.assertEqual(self.git("rev-parse", REF), self.base)
		self.assertEqual(self.snapshot(), before)
		self.assert_pending(run)

	def test_apply_uses_goal_or_commit_message_and_only_successful_claude_trailer(self):
		selected = targets.Target("p01", self.repo, claude_trailer="합성 Claude 서명")
		cases = [(None, [{"role": "implement", "tool": "claude", "ok": True}], True),
			("합성 반영  \n", [{"role": "implement", "tool": "claude", "ok": True}], True),
			("합성 반영", [{"role": "review", "tool": "claude", "ok": True}], False),
			("합성 반영", [{"role": "implement", "tool": "claude", "ok": False}], False),
			("합성 반영", [{"role": "implement", "tool": "claude", "ok": None}], False)]
		for message, calls, trailer in cases:
			with self.subTest(message=message, calls=calls):
				run = self.approved()
				run.request["commit_message"] = message
				run.calls = [{"plan": "p01", **call} for call in calls]
				run.save()
				with mock.patch.object(targets, "targets", return_value={"p01": selected}), \
						contextlib.redirect_stdout(io.StringIO()):
					commit = apply_to_branch(run.run_id, BRANCH)
				expected = message or run.request["goal"]
				if trailer:
					expected = expected.rstrip() + "\n\n" + selected.claude_trailer
				self.assertEqual(self.git("log", "-1", "--format=%B", commit), expected)

	def test_cli_dispatches_onto_and_prints_the_new_short_hash_last(self):
		run = self.approved()
		self.assertIn(f"apply {run.run_id} --onto {BRANCH}", cli.report(run))
		with mock.patch.object(cli, "apply_to_branch", wraps=apply_to_branch) as apply, \
				contextlib.redirect_stdout(io.StringIO()) as output:
			self.assertEqual(cli.main(["apply", run.run_id, "--onto", BRANCH]), 0)
			apply.assert_called_once_with(run.run_id, BRANCH)
		self.assertEqual(output.getvalue(), self.git("rev-parse", REF)[:12] + "\n")
		self.assertEqual(Run.load(run.run_id).status, "완료")

	def test_execution_then_onto_prints_once_and_updates_the_existing_journal_block(self):
		run = self.new_run()
		fake = FakeAI([PASS], {"src/a.py": "VALUE = 2\n"})
		with mock.patch("orchestrator.flow.call", fake), contextlib.redirect_stdout(io.StringIO()) as output:
			self.assertEqual(cli.execute(run), 0)
			first = output.getvalue()
			self.assertEqual(first, cli.report(run) + "\n")
			journal = next((self.vault / "📥 Inbox/AI").glob("*.md"))
			self.assertIn("- 상태: 승인 필요", vault.block_body(journal, "run:" + run.run_id))
			with journal.open("a", encoding="utf-8") as fh:
				fh.write("\n사람이 남긴 메모\n")
			self.assertEqual(cli.main(["apply", run.run_id, "--onto", BRANCH]), 0)
		commit = self.git("rev-parse", REF)
		self.assertEqual(output.getvalue(), first + commit[:12] + "\n")
		self.assertEqual(output.getvalue().splitlines().count(f"# 오케스트레이션 실행 {run.run_id}"), 1)
		saved = Run.load(run.run_id)
		self.assertEqual(saved.status, "완료")
		self.assertEqual((run.folder / "report.md").read_text(encoding="utf-8"), cli.report(saved))
		self.assertIn(commit[:12], saved.end_reason)
		body = vault.block_body(journal, "run:" + run.run_id)
		self.assertIn("- 상태: 완료", body)
		self.assertIn(saved.end_reason, body)
		text = journal.read_text(encoding="utf-8")
		self.assertEqual(text.count(f"<!-- ai-hub:run:{run.run_id} -->"), 1)
		self.assertIn("사람이 남긴 메모", text)
		with contextlib.redirect_stdout(io.StringIO()) as status:
			self.assertEqual(cli.main(["status", run.run_id]), 0)
		self.assertEqual(status.getvalue(), cli.report(saved) + "\n")

	def test_module_cli_applies_to_project_branch_in_a_separate_process(self):
		run = self.approved()
		before = self.snapshot()
		result = subprocess.run([sys.executable, "-m", "orchestrator", "apply", run.run_id, "--onto", BRANCH],
			capture_output=True, text=True, check=False)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout, self.git("rev-parse", REF)[:12] + "\n")
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(Run.load(run.run_id).status, "완료")


if __name__ == "__main__":
	unittest.main()
