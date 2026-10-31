"""합성 저장소로 격리 커밋과 훅 거부를 확인한다."""
import hashlib
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import checks, flow, isolated_commit, isolated_inputs
from orchestrator.record import Run
from tests.test_orchestrator_targets import init_repo, write_repos


@unittest.skipUnless(platform.system() == "Darwin", "Seatbelt는 macOS에서만 사용할 수 있습니다")
class IsolatedCommitTest(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		result, _ = isolated_commit.test_isolation._execute(
			["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)(deny process-fork)", "/usr/bin/true"],
			Path("/private/tmp"), {"HOME": "/private/tmp"}, 10)
		cls.sandbox_available = result["status"] == "passed"

	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.repo = init_repo(self.root / "repo")
		for name in ("allowed.txt", "staged.txt", "outside.txt"):
			(self.repo / name).write_text("base\n")
		checks.git(self.repo, "add", "--", "allowed.txt", "staged.txt", "outside.txt")
		checks.git(self.repo, "commit", "-qm", "합성 기준")
		self.base = checks.git(self.repo, "rev-parse", "HEAD").strip()
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest",
			"isolated_inputs": ["allowed.txt", "staged.txt", "outside.txt"]})
		self.enterContext(mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}))
		self.hook = self.repo / ".git/hooks/pre-commit"
		self.hook.write_text("#!/bin/sh\nread value < allowed.txt\n[ \"$value\" != forbidden ] || { echo PRIVATE_HOOK_OUTPUT; exit 1; }\n"
			"if (echo bad > forbidden-output) 2>/dev/null; then exit 1; fi\nexit 0\n")
		self.hook.chmod(0o755)
		self.enterContext(mock.patch.object(isolated_commit, "HOOK_SHA256", hashlib.sha256(self.hook.read_bytes()).hexdigest()))
		self.enterContext(mock.patch.object(isolated_commit, "AUTO", Path("/usr/bin/true")))
		self.enterContext(mock.patch.object(isolated_commit, "AUTO_SHA256", hashlib.sha256(Path("/usr/bin/true").read_bytes()).hexdigest()))
		self.worktree = self.root / "worktree"
		isolated_inputs.create_worktree(self.repo, self.worktree, self.base, "synthetic")

	def test_commit_preserves_staged_and_outside_and_worktree_is_read_only(self):
		if not self.sandbox_available:
			self.skipTest("현재 실행 환경에서 Seatbelt 적용이 거부됐습니다")
		(self.worktree / "allowed.txt").write_text("approved\n")
		(self.worktree / "outside.txt").write_text("outside\n")
		(self.worktree / "staged.txt").write_text("existing staged\n")
		checks.git(self.worktree, "add", "--", "staged.txt")
		inside, outside = isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt", "staged.txt"], "합성 변경", self.repo)
		self.assertEqual(inside, ["allowed.txt"])
		self.assertCountEqual(outside, ["staged.txt", "outside.txt"])
		self.assertEqual(checks.git(self.worktree, "show", "HEAD:allowed.txt"), "approved\n")
		self.assertEqual(checks.git(self.worktree, "show", "HEAD:staged.txt"), "base\n")
		self.assertEqual(checks.git(self.worktree, "show", ":staged.txt"), "existing staged\n")
		self.assertEqual((self.worktree / "outside.txt").read_text(), "outside\n")
		self.assertFalse((self.worktree / "forbidden-output").exists())

	def test_hook_rejects_and_does_not_leak_output_or_advance_head(self):
		if not self.sandbox_available:
			self.skipTest("현재 실행 환경에서 Seatbelt 적용이 거부됐습니다")
		(self.worktree / "allowed.txt").write_text("forbidden\n")
		with mock.patch.object(isolated_commit.test_isolation, "_execute", wraps=isolated_commit.test_isolation._execute) as execute:
			with self.assertRaises(checks.CommitRejected) as raised:
				isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성 거부", self.repo)
		self.assertTrue(any("commit" in call.args[0][8:] for call in execute.call_args_list))
		self.assertNotIn("PRIVATE_HOOK_OUTPUT", str(raised.exception))
		self.assertEqual(checks.git(self.worktree, "rev-parse", "HEAD").strip(), self.base)
		self.assertEqual((self.worktree / "allowed.txt").read_text(), "forbidden\n")

	def test_unapproved_hook_and_missing_sandbox_have_no_host_fallback(self):
		(self.worktree / "allowed.txt").write_text("approved\n")
		self.hook.write_text("#!/bin/sh\nexit 0\n")
		with self.assertRaises(checks.CommitRejected):
			isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성", self.repo)
		with mock.patch.object(isolated_commit.platform, "system", return_value="Other"), \
				mock.patch.object(isolated_commit.test_isolation, "_execute") as execute:
			with self.assertRaises(checks.CommitRejected):
				isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성", self.repo)
			execute.assert_not_called()
		self.assertEqual(checks.git(self.worktree, "rev-parse", "HEAD").strip(), self.base)

	def test_sandbox_failure_never_falls_back_to_host_git(self):
		(self.worktree / "allowed.txt").write_text("approved\n")
		original = checks.git
		with mock.patch.object(isolated_commit.test_isolation, "_execute", return_value=({"status": "failed"}, b"private")) as execute, \
				mock.patch.object(checks, "git", wraps=original) as git:
			with self.assertRaises(checks.CommitRejected) as raised:
				isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성", self.repo)
		execute.assert_called_once()
		self.assertEqual(execute.call_args.args[2]["GIT_LITERAL_PATHSPECS"], "1")
		self.assertNotIn("private", str(raised.exception))
		self.assertFalse(any("commit" in call.args[1:] or "add" in call.args[1:] for call in git.call_args_list))
		self.assertEqual(checks.git(self.worktree, "rev-parse", "HEAD").strip(), self.base)

	def test_commit_timeout_stops_without_automatic_retry(self):
		(self.worktree / "allowed.txt").write_text("approved\n")
		def simulated(args, *_):
			command = args[8:]
			if command[0] == "rev-parse":
				return {"status": "passed"}, (str(self.hook) + "\n").encode()
			if command[0] == "--no-optional-locks":
				return {"status": "passed"}, b" M allowed.txt\0"
			return {"status": "timeout" if command[0] == "commit" else "passed"}, b""
		with mock.patch.object(isolated_commit.test_isolation, "_execute", side_effect=simulated) as execute:
			with self.assertRaises(isolated_commit.CommitStateUnknown):
				isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성", self.repo)
		self.assertEqual(sum(call.args[0][8] == "commit" for call in execute.call_args_list), 1)

	def test_index_failure_after_commit_stops_without_retry(self):
		(self.worktree / "allowed.txt").write_text("approved\n")
		def simulated(args, *_):
			command = args[8:]
			if command[0] == "rev-parse":
				return {"status": "passed"}, (str(self.hook) + "\n").encode()
			if command[0] == "--no-optional-locks":
				return {"status": "passed"}, b" M allowed.txt\0"
			return {"status": "failed" if command[0] == "reset" else "passed"}, b""
		with mock.patch.object(isolated_commit.test_isolation, "_execute", side_effect=simulated) as execute:
			with self.assertRaises(isolated_commit.IndexUpdateError):
				isolated_commit.commit_owned_isolated(self.worktree, ["allowed.txt"], "합성", self.repo)
		self.assertEqual(sum(call.args[0][8] == "commit" for call in execute.call_args_list), 1)

	def test_profile_keeps_source_read_only_and_network_denied(self):
		gitdir, common, ref = isolated_commit._gitdirs(self.worktree, self.repo)
		profile = isolated_commit._profile(self.worktree, gitdir, common, ref, self.root / "scratch", self.hook)
		self.assertIn("(deny network*)", profile)
		self.assertIn('(allow file-read* (subpath "' + str(self.worktree) + '"))', profile)
		self.assertNotIn('(allow file-read* file-write* (subpath "' + str(self.worktree) + '"))', profile)
		self.assertNotIn('(allow file-write* (subpath "' + str(common) + '"))', profile)

	def test_other_hook_and_fsmonitor_remain_blocked(self):
		other = self.repo / ".git/hooks/commit-msg"
		other.write_text("#!/bin/sh\nexit 0\n")
		other.chmod(0o755)
		with self.assertRaises(ValueError):
			isolated_inputs.check_git_safety(isolated_inputs.selected(self.repo), self.base, self.worktree)
		other.chmod(0o644)
		checks.git(self.repo, "config", "core.fsmonitor", "synthetic-command")
		with self.assertRaises(ValueError):
			isolated_inputs.check_git_safety(isolated_inputs.selected(self.repo), self.base, self.worktree)

	def test_flow_routes_only_external_commits_to_isolated_function(self):
		run = Run("synthetic", {"owned_paths": ["allowed.txt"]}, str(self.repo), worktree=str(self.worktree), base_commit=self.base)
		worker = flow.Flow(run, None, {})
		with mock.patch.object(run, "event"), mock.patch.object(isolated_inputs, "check_git_safety"), \
				mock.patch.object(isolated_inputs, "write_edits"), \
				mock.patch.object(isolated_commit, "commit_owned_isolated", return_value=([], [])) as external, \
				mock.patch.object(checks, "commit_owned", return_value=([], [])) as builtin:
			with mock.patch.object(flow.test_isolation, "requires", return_value=True):
				self.assertTrue(worker.absorb({"edits": []}))
			external.assert_called_once()
			builtin.assert_not_called()
			with mock.patch.object(flow.test_isolation, "requires", return_value=False):
				self.assertTrue(worker.absorb({}))
			builtin.assert_called_once()
