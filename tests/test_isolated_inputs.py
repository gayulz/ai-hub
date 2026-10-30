"""명시적 대상 입력에서 자동 격리 시험과 보고까지 합성 자료로 확인한다."""
import json
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency import delivery, documents, periodic_results, project
from collector.config import ConfigError
from orchestrator import checks, draft, isolated_inputs, targets, test_isolation
from orchestrator import flow
from orchestrator.flow import Flow
from orchestrator.record import Run
from tests.test_orchestrator_targets import init_repo, write_repos


class InputsTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.repo = init_repo(self.root / "repo")
		(self.repo / "tests").mkdir()
		(self.repo / "logic.py").write_text("VALUE = 1\n")
		(self.repo / "tests/test_logic.py").write_text(
			"import unittest,logic\nclass Check(unittest.TestCase):\n def test_value(self): self.assertEqual(logic.VALUE,1)\n")
		(self.repo / "unapproved.txt").write_text("private synthetic sentinel")
		checks.git(self.repo, "add", "--", "logic.py", "tests/test_logic.py", "unapproved.txt")
		checks.git(self.repo, "commit", "-qm", "합성 기준")
		self.base = checks.git(self.repo, "rev-parse", "HEAD").strip()
		self.paths = ["logic.py", "tests/test_logic.py"]
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest discover -s tests",
			"isolated_inputs": self.paths})
		self.enterContext(mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}))

	def test_target_contract_and_snapshot_exclude_unlisted_content(self):
		selected = targets.target("sample")
		self.assertEqual(selected.isolated_inputs, tuple(self.paths))
		files = isolated_inputs.snapshot(selected)
		self.assertEqual([item.name for item in files], self.paths)
		self.assertTrue(all(b"private synthetic sentinel" not in item.content for item in files))
		self.assertEqual(files, isolated_inputs.snapshot(selected, commit=self.base))
		for value in ("../escape.py", ".git/config", ".env", "ops.db", "tests/*.py", ["bad"]):
			with self.subTest(value=value):
				write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest",
					"isolated_inputs": [value]})
				with self.assertRaises(ConfigError):
					targets.targets()

	def test_missing_contract_and_symlink_never_start_backend(self):
		selected = targets.target("sample")
		(self.repo / "logic.py").unlink()
		(self.repo / "logic.py").symlink_to(self.repo / "unapproved.txt")
		with self.assertRaises((OSError, ValueError)):
			isolated_inputs.snapshot(selected)
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest"})
		with mock.patch.object(test_isolation, "run_isolated") as backend:
			result = isolated_inputs.run(self.repo, self.root / "evidence")
		self.assertFalse(result["ok"])
		backend.assert_not_called()

	def test_flow_verify_automatically_uses_registered_inputs(self):
		run = Run("synthetic-run", {"goal": "합성", "owned_paths": ["logic.py"]}, str(self.repo),
			worktree=str(self.repo), base_commit=self.base)
		worker = Flow(run, None, {})
		result = {"name": "테스트", "ok": True, "detail": "실행 2개, 실패 0개, 오류 0개, 건너뜀 1개",
			"counts": {"ran": 2, "failures": 0, "errors": 0, "skipped": 1}}
		with mock.patch.object(isolated_inputs, "run", return_value=result) as backend, \
				mock.patch.object(worker, "save"), mock.patch.object(run, "event"):
			worker.verify()
		backend.assert_called_once_with(str(self.repo), run.folder / "isolation", root=self.repo)
		self.assertIn(result, run.checks)
		self.assertTrue(checks.checks_passed(run.checks))
		self.assertEqual(run.step, "review")
		self.assertIn("건너뜀 1개", result["detail"])
		self.assertIn("전체 시험은 미검증", result["detail"])

	def test_final_review_passes_isolated_tests_without_claiming_full_coverage(self):
		from agency import acceptance
		result = {"name": "테스트", "ok": True, "detail": "실행 2개, 실패 0개, 오류 0개, 건너뜀 1개", "unverified": 1}
		with mock.patch.object(isolated_inputs, "run", return_value=result):
			actual = acceptance._test(targets.target("sample"), self.base)
		self.assertTrue(actual["test_ok"])
		self.assertTrue(actual["ok"])
		self.assertIn("건너뜀 1개", actual["summary"])
		self.assertIn("전체 시험 범위 미검증", actual["summary"])

	def test_external_worktree_only_checks_out_explicit_inputs(self):
		run = Run("synthetic-run", {"goal": "합성", "owned_paths": ["logic.py"]}, str(self.repo))
		worker = Flow(run, None, {})
		with mock.patch.object(flow, "worktrees_dir", return_value=self.root / "worktrees"), \
				mock.patch.object(worker, "save"), mock.patch.object(run, "event"):
			worker.prepare()
		worktree = Path(run.worktree)
		self.assertTrue((worktree / "logic.py").is_file())
		self.assertTrue((worktree / "tests/test_logic.py").is_file())
		self.assertFalse((worktree / "unapproved.txt").exists())
		self.assertEqual(checks.changed_paths(worktree), [])

	def test_external_commit_never_executes_repository_hooks_or_filters(self):
		marker = self.root / "synthetic-hook-ran"
		hook = self.repo / ".git/hooks/pre-commit"
		hook.write_text("#!/bin/sh\nprintf ran > " + str(marker) + "\n")
		hook.chmod(0o700)
		with self.assertRaises(ValueError):
			isolated_inputs.create_worktree(self.repo, self.root / "hook-worktree", self.base, "hook-test")
		self.assertFalse(marker.exists())
		hook.chmod(0o600)
		(self.repo / ".gitattributes").write_text("logic.py filter=synthetic\n")
		checks.git(self.repo, "add", "--", ".gitattributes")
		checks.git(self.repo, "commit", "-qm", "합성 필터")
		filtered = checks.git(self.repo, "rev-parse", "HEAD").strip()
		with self.assertRaises(ValueError):
			isolated_inputs.create_worktree(self.repo, self.root / "filter-worktree", filtered, "filter-test")

	def test_review_material_rejects_changes_outside_inputs_and_links(self):
		checks.git(self.repo, "checkout", "-qb", "candidate")
		(self.repo / "unapproved.txt").write_text("other synthetic data")
		checks.git(self.repo, "add", "--", "unapproved.txt")
		checks.git(self.repo, "commit", "-qm", "합성 범위 밖")
		tip = checks.git(self.repo, "rev-parse", "HEAD").strip()
		with self.assertRaises(ValueError):
			isolated_inputs.review_material(targets.target("sample"), self.base, tip)
		checks.git(self.repo, "checkout", "-q", "main")
		checks.git(self.repo, "checkout", "-qb", "candidate-link")
		(self.repo / "logic.py").unlink()
		(self.repo / "logic.py").symlink_to("unapproved.txt")
		checks.git(self.repo, "add", "--", "logic.py")
		checks.git(self.repo, "commit", "-qm", "합성 링크")
		tip = checks.git(self.repo, "rev-parse", "HEAD").strip()
		with self.assertRaises(ValueError):
			isolated_inputs.review_material(targets.target("sample"), self.base, tip)

	def test_external_merge_checks_selected_commit_without_checkout(self):
		checks.git(self.repo, "checkout", "-qb", "candidate")
		(self.repo / "logic.py").write_text("VALUE = 2\n")
		checks.git(self.repo, "add", "--", "logic.py")
		checks.git(self.repo, "commit", "-qm", "합성 변경")
		tip = checks.git(self.repo, "rev-parse", "HEAD").strip()
		checks.git(self.repo, "checkout", "-q", "main")
		bundle = {"project": "p-synthetic", "branch_tip": tip}
		with mock.patch.object(isolated_inputs, "run", return_value={"ok": True}) as backend, \
				mock.patch.object(delivery, "_git", wraps=delivery._git) as git:
			merge = delivery._merge_test(self.repo, self.base, bundle, "합성", "python3 -m unittest")
		self.assertEqual(backend.call_args.kwargs.get("commit"), merge)
		self.assertFalse(any(call.args[1:3] == ("worktree", "add") for call in git.call_args_list))
		self.assertEqual(checks.git(self.repo, "rev-list", "--parents", "-n", "1", merge).split()[1:], [self.base, tip])

	def test_ai_receives_only_configured_files_and_edits_stay_in_scope(self):
		seen = []
		def caller(plan, writer, prompt, cwd, schema, timeout):
			seen.append(prompt)
			self.assertFalse(writer)
			self.assertEqual(list(cwd.iterdir()), [])
			return "synthetic"
		isolated_inputs.call(caller, None, True, "합성 구현", self.repo, {}, 30, self.repo)
		self.assertIn("VALUE = 1", seen[0])
		self.assertNotIn("private synthetic sentinel", seen[0])
		request = {"owned_paths": ["logic.py", "unapproved.txt"]}
		with self.assertRaises(ValueError):
			isolated_inputs.write_edits(self.repo, request, [{"path": "logic.py", "content": "VALUE = 2\n"}], self.repo)
		worktree = self.root / "edits"
		isolated_inputs.create_worktree(self.repo, worktree, self.base, "synthetic-edits")
		with self.assertRaises(ValueError):
			isolated_inputs.write_edits(worktree, request, [{"path": "unapproved.txt", "content": "changed"}], self.repo)
		isolated_inputs.write_edits(worktree, request, [{"path": "logic.py", "content": "VALUE = 2\n"}], self.repo)
		self.assertEqual((worktree / "logic.py").read_text(), "VALUE = 2\n")
		self.assertEqual((self.repo / "logic.py").read_text(), "VALUE = 1\n")

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	@unittest.skipIf(os.environ.get("AIHUB_SKIP_NESTED_SANDBOX") == "1", "중첩 격리는 별도 실행")
	def test_registered_sources_automatically_reach_report_through_real_sandbox(self):
		identity = "p-20261029-112"
		folder = project._folder(identity)
		folder.mkdir(parents=True)
		request_path = folder / "t1.toml"
		request = {"request": {"kind": "analysis", "workdir": str(folder / "work"), "role": "backend",
			"goal": "합성 보고", "done": ["실행 결과"], "report_mode": "periodic-v1", "report_project": identity,
			"report_task": "t1", "report_checks": ["tests"]}, "budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		request_path.write_text(draft.serialize(request)); request_path.chmod(0o600)
		import hashlib
		task = {"id": "t1", "status": "실행 중", "run_id": "", "request_path": str(request_path),
			"request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest()}
		task["report_evidence"] = periodic_results.prepare(identity, task, targets.target("sample"), self.base)
		report = documents.render_evidence(documents.read_evidence(identity, task))
		self.assertIn("실행 1개, 실패 0개, 오류 0개, 건너뜀 0개", report)
		self.assertNotIn("private synthetic sentinel", report)


if __name__ == "__main__":
	unittest.main()
