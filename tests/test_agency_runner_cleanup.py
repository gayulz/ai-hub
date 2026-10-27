"""한도 실행만 임시 Git 저장소에서 정리하고 기록과 다른 실행을 보존한다."""

import json
import shutil
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agency import project, runner
from collector.backfill_common import locked
from collector.config import ConfigError, load_plans
from orchestrator import __main__ as orchestration, checks
from orchestrator.flow import Flow, worktrees_dir
from orchestrator.record import Run, new_run_id
from tests import test_agency_runner as fixtures


class CleanupTest(unittest.TestCase):
	setUp = fixtures.RunnerTest.setUp
	git = fixtures.RunnerTest.git
	snapshot = fixtures.RunnerTest.snapshot
	new = fixtures.RunnerTest.new
	start = fixtures.RunnerTest.start

	def execution(self, task, status="승인 필요", reason="합성 검토 통과"):
		if "(한도 소진)" not in reason:
			return fixtures.RunnerTest.execution(self, task, status, reason)
		request = orchestration.read_request(task["request_path"])["request"]
		execution = Run(new_run_id(), request, str(self.repo))
		shutil.copyfile(task["request_path"], execution.folder / "request.toml")
		execution.calls = [dict(call) for call in self.calls]
		execution.save()
		if getattr(self, "prepare_quota", True):
			Flow(execution, load_plans(), orchestration.orchestration_config()).prepare()
		execution.finish(status, reason)
		(execution.folder / "report.md").write_text("합성 보고서\n", encoding="utf-8")
		return execution

	def quota(self, row, *, prepare=True):
		self.prepare_quota = prepare
		self.outcomes = [("실패", "합성 실패 (한도 소진)")]
		self.assertEqual(runner.run(row["id"])["status"], "막힘")
		task = project.load(row["id"])["tasks"][0]
		self.assertEqual(task["status"], "대기")
		return Run.load(task["run_id"])

	def other_execution(self, task, status):
		request = orchestration.read_request(task["request_path"])["request"]
		execution = Run(new_run_id(), request, str(self.repo))
		shutil.copyfile(task["request_path"], execution.folder / "request.toml")
		execution.save()
		Flow(execution, load_plans(), orchestration.orchestration_config()).prepare()
		Path(execution.worktree, "value.txt").write_text("스테이징 보존\n", encoding="utf-8")
		checks.git(execution.worktree, "add", "--", "value.txt")
		Path(execution.worktree, "value.txt").write_text("작업 파일 보존\n", encoding="utf-8")
		Path(execution.worktree, "보존.txt").write_text(status, encoding="utf-8")
		execution.finish(status, "합성 보호 대상")
		(execution.folder / "report.md").write_text("보호 보고서\n", encoding="utf-8")
		return execution

	def records(self, execution):
		return {name: (execution.folder / name).read_bytes()
			for name in ("run.json", "request.toml", "report.md", "events.jsonl")}

	def ref_exists(self, branch):
		result = subprocess.run(["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet",
			"refs/heads/" + branch], capture_output=True)
		self.assertIn(result.returncode, (0, 1))
		return result.returncode == 0

	def worktree_snapshot(self, execution):
		root = Path(execution.worktree)
		index = Path(checks.git(root, "rev-parse", "--git-path", "index").strip())
		return (checks.git(root, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
			(root / index).read_bytes(),
			{str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()})

	def registered(self, execution):
		return str(worktrees_dir() / execution.run_id) in self.git("worktree", "list", "--porcelain")

	def assert_removed(self, execution):
		self.assertFalse((worktrees_dir() / execution.run_id).exists())
		self.assertFalse(self.registered(execution))
		self.assertFalse(self.ref_exists("orch/" + execution.run_id))
		self.assertNotIn("refs/heads/orch/" + execution.run_id,
			self.git("for-each-ref", "--format=%(refname)", "refs/heads/orch/"))

	def assert_retry_blocked(self, row, execution):
		before = self.snapshot()
		self.start_mock.reset_mock()
		with self.assertRaises((ValueError, RuntimeError, OSError)):
			runner.run(row["id"])
		self.start_mock.assert_not_called()
		self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)
		self.assertEqual(self.snapshot(), before)
		self.assertTrue(self.registered(execution))
		self.assertTrue(self.ref_exists("orch/" + execution.run_id))

	def test_two_quota_failures_are_cleaned_before_each_new_start(self):
		row = self.new()
		(self.repo / "value.txt").write_text("스테이징 보존\n", encoding="utf-8")
		self.git("add", "--", "value.txt")
		(self.repo / "value.txt").write_text("작업 파일 보존\n", encoding="utf-8")
		(self.repo / "local.txt").write_text("추적하지 않는 파일", encoding="utf-8")
		first = self.quota(row)
		others = [self.other_execution(row["tasks"][0], status) for status in ("작업 중", "승인 필요")]
		main_snapshot = self.snapshot()
		branch_tip = self.git("rev-parse", row["branch"])
		other_records = [self.records(run) for run in others]
		other_values = [self.worktree_snapshot(run) for run in others]
		other_tips = [self.git("rev-parse", run.branch) for run in others]
		previous = first
		for retry in range(2):
			with self.subTest(retry=retry):
				old_records = self.records(previous)
				def start_after_cleanup(request_path):
					self.assert_removed(previous)
					self.assertEqual(self.records(previous), old_records)
					self.assertEqual(Run.load(previous.run_id).run_id, previous.run_id)
					self.assertEqual(self.snapshot(), main_snapshot)
					self.assertEqual(self.git("rev-parse", row["branch"]), branch_tip)
					for index, other in enumerate(others):
						self.assertTrue(self.registered(other))
						self.assertTrue(self.ref_exists(other.branch))
						self.assertEqual(self.git("rev-parse", other.branch), other_tips[index])
						self.assertEqual(self.worktree_snapshot(other), other_values[index])
						self.assertEqual(self.records(other), other_records[index])
					return self.start(request_path)
				self.start_mock.side_effect = start_after_cleanup
				self.outcomes = [("실패", "합성 실패 (한도 소진)")] if retry == 0 else []
				result = runner.run(row["id"])
				current = project.load(row["id"])["tasks"][0]["run_id"]
				self.assertNotEqual(current, previous.run_id)
				self.assertEqual(result["status"], "막힘" if retry == 0 else "검수")
				previous = Run.load(current)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 6, "seconds": 7.5})

	def test_missing_and_partially_removed_resources_can_be_retried(self):
		for state in ("not_created", "branch_only", "path_missing", "worktree_removed", "both_removed"):
			with self.subTest(state=state):
				row = self.new()
				old = self.quota(row, prepare=state not in ("not_created", "branch_only"))
				if state == "branch_only":
					old.branch = "orch/" + old.run_id
					old.save()
					self.git("branch", old.branch, self.head)
				records = self.records(old)
				if state == "path_missing":
					Path(old.worktree).rename(self.root / ("missing-" + old.run_id))
				elif state in ("worktree_removed", "both_removed"):
					self.git("worktree", "remove", "--force", old.worktree)
				if state == "both_removed":
					self.git("branch", "-D", old.branch)
				self.start_mock.reset_mock()
				self.assertEqual(runner.run(row["id"])["status"], "검수")
				self.assert_removed(old)
				self.assertEqual(self.records(old), records)
				self.assertEqual(Run.load(old.run_id).run_id, old.run_id)
				self.start_mock.assert_called_once()

	def test_unqualified_and_protected_statuses_never_start_or_remove(self):
		cases = [("실패", "합성 실패", False), ("작업 중", "합성 (한도 소진)", False),
			("승인 필요", "합성 (한도 소진)", False), ("완료", "합성 (한도 소진)", False),
			("실패", "합성 (한도 소진)", True)]
		for status, reason, pending in cases:
			with self.subTest(status=status, pending=pending):
				row = self.new()
				execution = self.quota(row)
				execution.status, execution.end_reason, execution.approval_pending = status, reason, pending
				execution.save()
				self.assert_retry_blocked(row, execution)

	def test_execution_lock_blocks_cleanup_and_new_start(self):
		row = self.new()
		execution = self.quota(row)
		with orchestration.locked(execution):
			self.assert_retry_blocked(row, execution)

	def test_both_locks_remain_held_during_git_cleanup(self):
		row = self.new()
		execution = self.quota(row)
		original, checked = runner.git, []
		def check_locks(repo, *args, **kwargs):
			if args[:2] == ("worktree", "remove"):
				with self.assertRaises(RuntimeError), orchestration.locked(execution):
					self.fail("실행 잠금이 해제됐습니다")
				with self.assertRaises(ConfigError), locked("agency-repo:" + str(self.repo)):
					self.fail("저장소 잠금이 해제됐습니다")
				checked.append(True)
			return original(repo, *args, **kwargs)
		with mock.patch.object(runner, "git", side_effect=check_locks):
			self.assertEqual(runner.run(row["id"])["status"], "검수")
		self.assertEqual(checked, [True])

	def test_record_is_reread_after_execution_lock(self):
		row = self.new()
		execution = self.quota(row)
		original = orchestration.locked
		@contextmanager
		def change_before_lock(current):
			execution.status = "승인 필요"
			execution.save()
			with original(current):
				yield
		with mock.patch.object(orchestration, "locked", side_effect=change_before_lock):
			self.assert_retry_blocked(row, execution)

	def test_resume_attempt_during_checks_blocks_cleanup(self):
		for check in (1, 2):
			with self.subTest(check=check):
				row = self.new()
				execution = self.quota(row)
				records = self.records(execution)
				original, calls = runner._branch_exists, []
				def resume_during_check(repo, ref):
					result = original(repo, ref)
					calls.append(True)
					if len(calls) == check:
						self.assertEqual(self.registered(execution), check == 1)
						with self.assertRaises(RuntimeError):
							orchestration.cmd_resume(execution.run_id, ["--calls", "3"])
						self.assertEqual(self.records(execution), records)
					return result
				self.start_mock.reset_mock()
				with mock.patch.object(runner, "_branch_exists", side_effect=resume_during_check):
					self.assertEqual(runner.run(row["id"])["status"], "검수")
				self.assertGreaterEqual(len(calls), check)
				self.start_mock.assert_called_once()
				self.assertNotEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)
				self.assertEqual(self.records(execution), records)
				self.assert_removed(execution)

	def test_mismatched_execution_metadata_or_request_is_refused(self):
		cases = ("id", "repo", "request", "base", "kind", "branch", "worktree", "archive")
		for field in cases:
			with self.subTest(field=field):
				row = self.new()
				execution = self.quota(row)
				if field == "id":
					execution.run_id = "2020-01-01-999"
				elif field == "repo":
					execution.repo = str(self.root)
				elif field == "request":
					execution.request["goal"] = "다른 요청"
				elif field == "base":
					execution.request["base_branch"] = "other"
				elif field == "kind":
					execution.kind = "analysis"
				elif field == "branch":
					execution.branch = "orch/other"
				elif field == "worktree":
					execution.worktree = str(self.root / "other")
				elif field == "archive":
					(execution.folder / "request.toml").write_text('[request]\ngoal="다른 요청"\n', encoding="utf-8")
				if field == "id":
					path = self.hub / "runs" / project.load(row["id"])["tasks"][0]["run_id"] / "run.json"
					data = json.loads(path.read_text(encoding="utf-8"))
					data["run_id"] = execution.run_id
					path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
					execution.run_id = path.parent.name
				elif field != "archive":
					execution.save()
				self.assert_retry_blocked(row, execution)

	def test_consistent_but_wrong_or_symlinked_request_repo_is_refused(self):
		for change in ("wrong", "symlink"):
			with self.subTest(change=change):
				row = self.new()
				execution = self.quota(row)
				path = Path(row["tasks"][0]["request_path"])
				if change == "symlink":
					alias = self.root / "repo-alias"
					alias.symlink_to(self.repo, target_is_directory=True)
				else:
					alias = self.root
				execution.request["repo"] = str(alias)
				text = "[request]\n" + "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
					for key, value in execution.request.items())
				path.write_text(text, encoding="utf-8")
				(execution.folder / "request.toml").write_text(text, encoding="utf-8")
				execution.save()
				self.assert_retry_blocked(row, execution)

	def test_invalid_run_id_is_rejected_before_loading_record(self):
		row = self.new()
		execution = self.quota(row)
		for value in ("../outside", "2020-01-01-000", "2020-99-01-001", "bad"):
			with self.subTest(value=value):
				project.update_task(row["id"], "t1", run_id=value)
				self.start_mock.reset_mock()
				with mock.patch.object(Run, "load", side_effect=AssertionError("형식 검사 전에 기록 접근")), \
						self.assertRaises(ValueError):
					runner.run(row["id"])
				self.start_mock.assert_not_called()
				self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], value)
				self.assertTrue(self.registered(execution))

	def test_git_registration_mismatch_is_refused(self):
		for change in ("detached", "other_path", "unregistered"):
			with self.subTest(change=change):
				row = self.new()
				execution = self.quota(row)
				if change == "detached":
					checks.git(execution.worktree, "checkout", "--detach", "-q")
				elif change == "other_path":
					self.git("worktree", "move", execution.worktree, str(self.root / "moved"))
				else:
					self.git("worktree", "remove", "--force", execution.worktree)
					Path(execution.worktree).mkdir()
					Path(execution.worktree, "보존.txt").write_text("다른 자료", encoding="utf-8")
				self.start_mock.reset_mock()
				with self.assertRaises((ValueError, RuntimeError)):
					runner.run(row["id"])
				self.start_mock.assert_not_called()
				self.assertTrue(self.ref_exists(execution.branch))
				if change == "unregistered":
					self.assertEqual(Path(execution.worktree, "보존.txt").read_text(encoding="utf-8"), "다른 자료")
				self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)

	def test_symlinked_worktree_or_record_is_refused(self):
		for link in ("worktree", "run_folder", "lock", "request", "run_json", "report", "events", "task_request"):
			with self.subTest(link=link):
				row = self.new()
				execution = self.quota(row)
				if link == "worktree":
					path = Path(execution.worktree)
					destination = self.root / ("moved-" + execution.run_id)
					self.git("worktree", "move", str(path), str(destination))
				elif link == "run_folder":
					path = execution.folder
					destination = self.root / ("record-" + execution.run_id)
					path.rename(destination)
				elif link == "task_request":
					path = Path(row["tasks"][0]["request_path"])
					destination = self.root / (link + "-" + execution.run_id)
					path.rename(destination)
				else:
					name = {"lock": ".lock", "request": "request.toml", "run_json": "run.json",
						"report": "report.md", "events": "events.jsonl"}[link]
					path = execution.folder / name
					destination = self.root / (link + "-" + execution.run_id)
					if path.exists():
						path.rename(destination)
					else:
						destination.write_text("보존", encoding="utf-8")
				path.symlink_to(destination, target_is_directory=destination.is_dir())
				self.start_mock.reset_mock()
				with self.assertRaises((ValueError, RuntimeError, OSError)):
					runner.run(row["id"])
				self.start_mock.assert_not_called()
				self.assertTrue(destination.exists())
				self.assertTrue(self.ref_exists(execution.branch))
				self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)

	def test_missing_project_branch_is_not_recreated_for_a_waiting_run(self):
		row = self.new()
		execution = self.quota(row)
		self.git("branch", "-D", row["branch"])
		self.assert_retry_blocked(row, execution)
		self.assertFalse(self.ref_exists(row["branch"]))

	def test_project_branch_cannot_be_the_cleanup_branch(self):
		row = self.new()
		execution = self.quota(row)
		self.git("worktree", "remove", "--force", execution.worktree)
		with project._edit(row["id"], "합성 변경") as current:
			current["branch"] = execution.branch
		execution.request["base_branch"] = execution.branch
		text = "[request]\n" + "\n".join(key + "=" + json.dumps(value, ensure_ascii=False)
			for key, value in execution.request.items())
		Path(row["tasks"][0]["request_path"]).write_text(text, encoding="utf-8")
		(execution.folder / "request.toml").write_text(text, encoding="utf-8")
		execution.save()
		records, before = self.records(execution), self.snapshot()
		self.start_mock.reset_mock()
		with self.assertRaises(ValueError):
			runner.run(row["id"])
		self.start_mock.assert_not_called()
		self.assertTrue(self.ref_exists(execution.branch))
		self.assertTrue(self.ref_exists(row["branch"]))
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(self.records(execution), records)
		self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)

	def test_current_repository_cannot_be_the_cleanup_worktree(self):
		primary = self.repo
		run_id = "2020-01-01-123"
		self.repo = self.root / run_id
		checks.git(primary, "worktree", "add", "-q", "-b", "orch/" + run_id, str(self.repo), self.head)
		fixtures.write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": "python3 -m unittest"})
		row = self.new()
		self.git("branch", row["branch"], self.head)
		request_path = row["tasks"][0]["request_path"]
		execution = Run(run_id, orchestration.read_request(request_path)["request"], str(self.repo),
			worktree=str(self.repo), branch="orch/" + run_id)
		execution.folder.mkdir(parents=True)
		shutil.copyfile(request_path, execution.folder / "request.toml")
		execution.save()
		execution.finish("실패", "합성 실패 (한도 소진)")
		(execution.folder / "report.md").write_text("합성 보고서\n", encoding="utf-8")
		project.transition(row["id"], "실행 중")
		project.update_task(row["id"], "t1", run_id=run_id)
		before = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"), self.records(execution))
		original = runner.git
		def prevent_remove(repo, *args, **kwargs):
			if args[:2] == ("worktree", "remove"):
				self.fail("현재 저장소 제거를 시도했습니다")
			return original(repo, *args, **kwargs)
		self.start_mock.reset_mock()
		with mock.patch.object(runner, "worktrees_dir", return_value=self.root), \
				mock.patch.object(runner, "git", side_effect=prevent_remove), self.assertRaises(ValueError):
			runner.run(row["id"])
		self.start_mock.assert_not_called()
		self.assertTrue(self.repo.is_dir())
		self.assertEqual((self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"), self.records(execution)), before)

	def test_git_failure_keeps_run_id_and_retries_remaining_cleanup(self):
		for stage in ("worktree", "branch", "list"):
			with self.subTest(stage=stage):
				row = self.new()
				execution = self.quota(row)
				records = self.records(execution)
				original = runner.git
				def fail_once(repo, *args, **kwargs):
					if args[:2] == {"worktree": ("worktree", "remove"), "branch": ("branch", "-D"),
							"list": ("worktree", "list")}[stage]:
						raise RuntimeError("합성 Git 실패")
					return original(repo, *args, **kwargs)
				self.start_mock.reset_mock()
				with mock.patch.object(runner, "git", side_effect=fail_once), self.assertRaises(RuntimeError):
					runner.run(row["id"])
				self.start_mock.assert_not_called()
				self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)
				self.assertEqual(self.records(execution), records)
				self.assertEqual(self.registered(execution), stage != "branch")
				self.assertTrue(self.ref_exists(execution.branch))
				self.assertEqual(runner.run(row["id"])["status"], "검수")
				self.assert_removed(execution)
				self.assertEqual(self.records(execution), records)

	def test_false_success_or_ref_check_failure_cannot_start_a_new_run(self):
		for stage in ("worktree", "branch", "show_ref"):
			with self.subTest(stage=stage):
				row = self.new()
				execution = self.quota(row)
				original_git, original_run = runner.git, subprocess.run
				def pretend_success(repo, *args, **kwargs):
					if args[:2] == {"worktree": ("worktree", "remove"), "branch": ("branch", "-D")}.get(stage):
						return ""
					return original_git(repo, *args, **kwargs)
				def fail_show_ref(command, *args, **kwargs):
					if stage == "show_ref" and "show-ref" in command:
						return subprocess.CompletedProcess(command, 128)
					return original_run(command, *args, **kwargs)
				self.start_mock.reset_mock()
				with mock.patch.object(runner, "git", side_effect=pretend_success), \
						mock.patch.object(runner.subprocess, "run", side_effect=fail_show_ref), \
						self.assertRaises(RuntimeError):
					runner.run(row["id"])
				self.start_mock.assert_not_called()
				self.assertEqual(project.load(row["id"])["tasks"][0]["run_id"], execution.run_id)
				self.assertTrue(self.ref_exists(execution.branch))
				self.assertEqual(runner.run(row["id"])["status"], "검수")
				self.assert_removed(execution)


if __name__ == "__main__":
	unittest.main()
