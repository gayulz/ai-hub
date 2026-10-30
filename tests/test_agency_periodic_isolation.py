"""격리 자료가 준비되지 않으면 블로그 자동 경로가 닫히는지 확인한다."""

import unittest
import tempfile
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agency import acceptance, planner, project, runner, schedule
from orchestrator import checks, draft, flow, targets, test_isolation
from orchestrator.record import Run
from tests.test_orchestrator_targets import init_repo


class AutomaticIsolationTest(unittest.TestCase):
	def execution(self):
		run = Run("synthetic-run", {"goal": "합성", "done": ["합성"], "test_command": "python3 -m unittest"},
			"/synthetic-repository", worktree="/synthetic-worktree", base_commit="synthetic")
		return run, flow.Flow(run, None, {})

	def test_prepare_and_implementation_do_not_touch_unprepared_blog_source(self):
		for kind in ("code", "analysis"):
			for step in ("prepare", "implement", "review", "fix"):
				run, worker = self.execution()
				run.kind = kind
				with self.subTest(kind=kind, step=step), mock.patch.object(test_isolation, "requires", return_value=True), \
						mock.patch.object(checks, "git") as git, mock.patch.object(worker, "ask") as ask, \
						mock.patch.object(run, "event"):
					getattr(worker, step)()
				self.assertEqual(run.status, "실패")
				git.assert_not_called()
				ask.assert_not_called()

	def test_implementation_verification_never_starts_unisolated_tests(self):
		run, worker = self.execution()
		with mock.patch.object(test_isolation, "requires", return_value=True), \
				mock.patch.object(checks, "git", return_value="") as git, mock.patch.object(checks, "_names", return_value=[]), \
				mock.patch.object(checks, "run_tests") as tests, mock.patch.object(run, "event"), \
				mock.patch.object(worker, "save"):
			worker.verify()
		tests.assert_not_called()
		git.assert_not_called()
		result = next(item for item in run.checks if item["name"] == "테스트")
		self.assertFalse(result["ok"])
		self.assertEqual(result["unverified"], 1)
		self.assertIn({"name": "허용 경로", "ok": True, "detail": ""}, run.checks)
		self.assertFalse(checks.verify(Path("/synthetic"), "base", {}, ["outside"], isolated=True)[0]["ok"])

	def test_agency_does_not_prepare_a_branch_or_call_a_provider_before_isolation(self):
		row = {"department": "synthetic", "status": "계획", "blocked_reason": "",
			"tasks": [{"id": "t1", "status": "대기"}]}
		blocked = {**row, "status": "막힘", "blocked_reason": "합성 격리 미준비"}
		department = SimpleNamespace(deliverable="code", external_ai=True)
		selected = targets.Target("blog", Path("/synthetic"))
		with mock.patch.object(runner.project, "load", return_value=row), \
				mock.patch.object(runner, "load_departments", return_value={"synthetic": department}), \
				mock.patch.object(runner.schedule, "target", return_value="blog"), \
				mock.patch.object(runner.targets, "target", return_value=selected), \
				mock.patch.object(runner, "locked", return_value=nullcontext()), \
				mock.patch.object(runner.project, "transition", return_value=blocked), \
				mock.patch.object(runner, "git") as git, mock.patch.object(runner.meeting, "ensure") as meeting:
			result = runner.run("synthetic")
		self.assertEqual(result["status"], "막힘")
		git.assert_not_called()
		meeting.assert_not_called()

	def test_unexecuted_periodic_report_can_start_without_isolation_inputs(self):
		task = {"id": "t1", "status": "대기"}
		row = {"department": "synthetic", "status": "계획", "blocked_reason": "", "tasks": [task],
			"schedule_repo": "synthetic", "audit_report_mode": "explicit-v1"}
		department = SimpleNamespace(deliverable="code", external_ai=True)
		selected = targets.Target("blog", Path("/synthetic"))
		def transition(identity, status, **kwargs):
			return {**row, "status": status, "blocked_reason": kwargs.get("reason", "")}
		with mock.patch.object(runner.project, "load", return_value=row), \
				mock.patch.object(runner, "load_departments", return_value={"synthetic": department}), \
				mock.patch.object(runner.schedule, "target", return_value="blog"), \
				mock.patch.object(runner.targets, "target", return_value=selected), \
				mock.patch.object(runner, "locked", return_value=nullcontext()), \
				mock.patch.object(runner.project, "transition", side_effect=transition), \
				mock.patch.object(runner.project, "next_ready_tasks", return_value=[task]), \
				mock.patch.object(runner, "_task_is_document", return_value=True), \
				mock.patch.object(runner.meeting, "ensure", side_effect=ValueError("합성")) as meeting:
			result = runner.run("synthetic")
		self.assertEqual(result["status"], "막힘")
		meeting.assert_called_once()

	def test_final_review_does_not_checkout_or_run_unisolated_tests(self):
		selected = targets.Target("blog", Path("/synthetic-repository"), "synthetic test")
		with mock.patch.object(acceptance, "git") as git, mock.patch.object(acceptance.subprocess, "run") as execute:
			result = acceptance._test(selected, "synthetic-commit")
		git.assert_not_called()
		execute.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["unverified"], 1)

	def test_only_builtin_repo_is_not_isolated(self):
		with tempfile.TemporaryDirectory() as folder:
			root = Path(folder).resolve()
			builtin, other = root / "builtin", root / "other"
			builtin.mkdir(); other.mkdir()
			with mock.patch.object(targets, "REPO", builtin):
				self.assertFalse(test_isolation.requires(str(builtin)))
				self.assertTrue(test_isolation.requires(str(other)))
				self.assertTrue(test_isolation.requires(str(root / "missing")))

	def test_evidence_creation_stays_in_the_open_parent_if_renamed(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			parent, protected = root / "entry", root / "protected"
			parent.mkdir(mode=0o700)
			protected.mkdir(mode=0o700)
			backend = test_isolation.Backend(parent / "evidence")
			original = os.mkdir
			def raced(name, mode=0o777, *, dir_fd=None):
				if name == "evidence":
					parent.rename(root / "preserved")
					parent.symlink_to(protected, target_is_directory=True)
				return original(name, mode, dir_fd=dir_fd)
			with mock.patch.object(os, "mkdir", raced):
				path = backend._reserve("c" * 64)
			with self.assertRaises((OSError, ValueError)):
				backend._save(path, {"status": "failed"})
			self.assertFalse((protected / "evidence").exists())
			self.assertTrue((root / "preserved/evidence" / path.name).is_file())

	def test_evidence_changes_during_save_are_not_accepted(self):
		for replacement in ("directory", "file"):
			with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
				root = Path(temporary).resolve()
				backend = test_isolation.Backend(root / "evidence")
				path = backend._reserve("d" * 64)
				protected = root / "protected"
				protected.mkdir(mode=0o700)
				original = os.fsync
				def raced(fd):
					original(fd)
					if replacement == "directory":
						path.parent.rename(root / "preserved-directory")
						path.parent.symlink_to(protected, target_is_directory=True)
					else:
						path.rename(root / "preserved-file")
					path.write_bytes(b"synthetic-protected")
				with mock.patch.object(os, "fsync", raced), self.assertRaises((OSError, ValueError)):
					backend._save(path, {"status": "failed"})
				self.assertEqual(path.read_bytes(), b"synthetic-protected")

	def test_copied_runtime_is_checked_again_before_modification(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			framework = root / "framework/Versions/3.14"
			stdlib = framework / "lib/python3.14"
			stdlib.mkdir(parents=True)
			(stdlib / "fixture.py").write_bytes(b"synthetic")
			expected = checks.isolation_runtime_tree(stdlib)
			boundary = {"python_root": str(framework), "stdlib_sha256": expected, "runtime": []}
			original = checks.isolation_runtime_tree
			def corrupt(path, destination=None):
				digest = original(path, destination)
				if destination is not None:
					(destination / "fixture.py").chmod(0o600)
					(destination / "fixture.py").write_bytes(b"changed")
				return digest
			with mock.patch.object(checks, "isolation_runtime_tree", side_effect=corrupt), \
					self.assertRaisesRegex(ValueError, "표준 라이브러리"):
				checks.copied_isolation_runtime(boundary, root)

	def test_unknown_repository_is_held_even_if_blog_registration_disappears(self):
		with tempfile.TemporaryDirectory() as folder:
			root = Path(folder).resolve()
			real = root / "repo"
			real.mkdir()
			alias = root / "alias"
			alias.symlink_to(real, target_is_directory=True)
			blog = targets.Target("blog", real)
			other = targets.Target("other", real)
			with mock.patch.object(targets, "targets", return_value={"other": other, "blog": blog}):
				self.assertTrue(test_isolation.requires(str(real)))
				self.assertTrue(test_isolation.requires(str(alias)))
			with mock.patch.object(targets, "targets", return_value={}):
				self.assertTrue(test_isolation.requires(str(root)))
			with mock.patch.object(targets, "targets", side_effect=targets.ConfigError("합성 설정 오류")):
				self.assertTrue(test_isolation.requires(str(real)))

	def test_blog_alias_is_blocked_before_every_automatic_entry_point(self):
		with tempfile.TemporaryDirectory() as folder:
			repo = Path(folder).resolve()
			blog, alias = targets.Target("blog", repo), targets.Target("other", repo)
			department = SimpleNamespace(deliverable="code", external_ai=True)
			plan_row = {"department": "synthetic", "status": "명세 확정", "tasks": []}
			run_row = {"department": "synthetic", "status": "계획", "blocked_reason": "",
				"tasks": [{"id": "t1", "status": "대기"}]}
			review_row = {"id": "synthetic", "department": "synthetic", "status": "검수",
				"tasks": [{"status": "완료"}], "spec": {"done_criteria": ["합성"]}}
			with mock.patch.object(targets, "targets", return_value={"other": alias, "blog": blog}), \
					mock.patch.object(targets, "target", return_value=alias), \
					mock.patch.object(schedule, "target", return_value="other"), \
					mock.patch.object(planner, "load_departments", return_value={"synthetic": department}), \
					mock.patch.object(runner, "load_departments", return_value={"synthetic": department}), \
					mock.patch.object(acceptance, "load_departments", return_value={"synthetic": department}), \
					mock.patch.object(runner, "locked", return_value=nullcontext()), \
					mock.patch.object(acceptance, "locked", return_value=nullcontext()), \
					mock.patch.object(project, "transition", return_value={**run_row, "status": "막힘"}), \
					mock.patch.object(project, "load", side_effect=[plan_row, run_row, run_row, review_row, review_row]), \
					mock.patch.object(checks, "git") as checks_git, mock.patch.object(runner, "git") as run_git, \
					mock.patch.object(acceptance, "git") as review_git, mock.patch.object(runner.meeting, "ensure") as meeting, \
					mock.patch.object(planner.ai, "ask") as ask, mock.patch.object(draft.providers, "call") as call, \
					mock.patch.object(acceptance.subprocess, "run") as execute:
				with self.assertRaisesRegex(ValueError, "격리"):
					planner.plan("synthetic")
				self.assertEqual(runner.run("synthetic")["status"], "막힘")
				self.assertFalse(acceptance.review("synthetic")["passed"])
				self.assertFalse(acceptance._test(alias, "synthetic")["executed"])
				with self.assertRaisesRegex(ValueError, "격리"):
					draft.create("backend", repo / "missing", repo / "output", target_id="other")
				for operation in (checks_git, run_git, review_git, meeting, ask, call, execute):
					operation.assert_not_called()

	def test_final_review_never_reads_diff_or_calls_ai_before_isolation(self):
		row = {"id": "synthetic", "department": "synthetic", "status": "검수",
			"tasks": [{"status": "완료"}], "spec": {"done_criteria": ["합성"]}}
		department = SimpleNamespace(external_ai=True, deliverable="code")
		selected = targets.Target("blog", Path("/synthetic"))
		with mock.patch.object(acceptance.project, "load", return_value=row), \
				mock.patch.object(acceptance, "load_departments", return_value={"synthetic": department}), \
				mock.patch.object(acceptance.schedule, "target", return_value="blog"), \
				mock.patch.object(acceptance.targets, "target", return_value=selected), \
				mock.patch.object(acceptance, "locked", return_value=nullcontext()), \
				mock.patch.object(acceptance.project, "transition", return_value={"status": "막힘"}), \
				mock.patch.object(acceptance, "git") as git, mock.patch.object(acceptance.ai, "ask") as ask:
			result = acceptance.review("synthetic")
		self.assertFalse(result["passed"])
		self.assertEqual(result["unverified"], 1)
		self.assertEqual(result["failed"], [])
		git.assert_not_called()
		ask.assert_not_called()

	def test_planner_and_draft_do_not_expose_unprepared_blog_source(self):
		row = {"department": "synthetic", "status": "명세 확정", "tasks": []}
		department = SimpleNamespace(external_ai=True, deliverable="code")
		selected = targets.Target("blog", Path("/synthetic"))
		with mock.patch.object(planner.project, "load", return_value=row), \
				mock.patch.object(planner, "load_departments", return_value={"synthetic": department}), \
				mock.patch.object(planner.schedule, "target", return_value="blog"), \
				mock.patch.object(planner.targets, "target", return_value=selected), \
				mock.patch.object(planner.project, "transition"), mock.patch.object(planner.checks, "git") as git, \
				mock.patch.object(planner.ai, "ask") as ask, self.assertRaisesRegex(ValueError, "격리"):
			planner.plan("synthetic")
		git.assert_not_called()
		ask.assert_not_called()
		with mock.patch.object(draft.targets, "target", return_value=selected), \
				mock.patch.object(draft.providers, "call") as call, self.assertRaisesRegex(ValueError, "격리"):
			draft.create("backend", Path("/unused"), Path("/unused-output"), target_id="blog")
		call.assert_not_called()


if __name__ == "__main__":
	unittest.main()
