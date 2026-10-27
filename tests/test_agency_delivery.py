"""납품을 임시 홈·저장소·파일 경로 bare 원격에서만 시험한다."""

import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agency import deliver_cli, delivery, project
from agency.__main__ import main
from agency.departments import Department
from collector.config import ConfigError
from orchestrator import checks
from tests.test_orchestrator_targets import init_repo, write_repos


class DeliveryTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub), "GIT_CONFIG_NOSYSTEM": "1",
			"GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file"})
		patch.start()
		self.addCleanup(patch.stop)
		self.repo = init_repo(self.root / "repo")
		for name, value in {"value.txt": "시작\n", "keep.txt": "유지\n", "시험 파일.py":
				"from pathlib import Path\nassert Path('value.txt').read_text(encoding='utf-8') == '납품\\n'\n"
				"assert Path('keep.txt').read_text(encoding='utf-8') == '유지\\n'\n"}.items():
			(self.repo / name).write_text(value, encoding="utf-8")
		self.git("add", "--", "value.txt", "keep.txt", "시험 파일.py")
		self.git("commit", "-q", "-m", "합성 시작")
		self.head = self.git("rev-parse", "HEAD")
		self.remote = self.root / "remote.git"
		checks.git(self.root, "init", "--bare", "-q", str(self.remote))
		self.git("remote", "add", "origin", str(self.remote))
		self.git("push", "origin", "main")
		self.command = shlex.quote(sys.executable) + " '시험 파일.py'"
		self.configure()
		department = Department("sample", "합성 부서", "합성 자료", ("sample",), (), False,
			"sample", (), "", ("merge", "push", "restart"), (), {"calls": 3, "minutes": 2})
		row = project.new(department, "합성 납품", "합성 요청")
		self.identity, self.branch = row["id"], row["branch"]
		project.update_spec(self.identity, goal="합성 변경", target="sample", out_of_scope="재기동 실행",
			done_criteria=["파일 변경"], budget=department.budget)
		project.confirm_spec(self.identity, department)
		for following in ("계획", "실행 중", "검수", "납품 대기"):
			project.transition(self.identity, following)
		self.git("checkout", "-q", "-b", self.branch)
		(self.repo / "value.txt").write_text("납품\n", encoding="utf-8")
		self.git("add", "--", "value.txt")
		self.git("commit", "-q", "-m", "합성 변경")
		self.tip = self.git("rev-parse", "HEAD")
		self.git("checkout", "-q", "main")
		self.folder = project._folder(self.identity)
		(self.folder / "report.md").write_text("# 검수 보고\n\n기존 근거\n", encoding="utf-8")
		self.bundle = {"version": 1, "project": self.identity, "target": "sample", "repo": str(self.repo),
			"branch": self.branch, "branch_tip": self.tip, "base": self.head, "actions": ["merge", "push", "restart"],
			"restart_notice": "사용자 규칙에 따라 별도로 재기동하세요", "changed": ["value.txt"],
			"criteria": [{"criterion": "파일 변경", "verdict": "pass", "evidence": "합성 시험"}],
			"tests": {"name": "테스트", "ok": True, "detail": "합성 시험\n통과"},
			"reviewer": "sample", "made": "2030-01-02T03:04:05+09:00"}
		self.write_bundle()
		patch = mock.patch.object(delivery, "git", wraps=checks.git)
		self.commands = patch.start()
		self.addCleanup(patch.stop)
		self.addCleanup(self.assert_cleaned)

	def git(self, *args):
		return checks.git(self.repo, *args).strip()

	def configure(self, command=None):
		write_repos(self.hub, {"id": "sample", "path": str(self.repo), "test_command": command or self.command})

	def write_bundle(self):
		payload = {key: value for key, value in self.bundle.items() if key != "hash"}
		self.bundle["hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
		(self.folder / "delivery.json").write_text(json.dumps(self.bundle, ensure_ascii=False), encoding="utf-8")

	def approve(self):
		return delivery.approve(self.identity, self.bundle["hash"])

	def record(self):
		return tuple((self.folder / name).read_bytes() for name in ("project.json", "events.jsonl", "report.md"))

	def files(self):
		return {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*")
			if p.is_file() and ".git" not in p.relative_to(self.repo).parts}

	def report(self):
		return (self.folder / "report.md").read_text(encoding="utf-8")

	def assert_cleaned(self):
		self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
		for call in self.commands.call_args_list:
			if call.args[1:4] == ("worktree", "add", "--detach"):
				self.assertFalse(Path(call.args[4]).exists())

	def assert_blocked(self, result, head, reason):
		self.assertEqual(result["status"], "막힘")
		row = project.load(self.identity)
		self.assertEqual((row["status"], row["blocked_from"]), ("막힘", "납품 중"))
		self.assertIn("납품 실패: " + reason, row["blocked_reason"])
		self.assertIn(reason, self.report())
		self.assertIn("## 납품", self.report())
		self.assertEqual(self.git("rev-parse", "HEAD"), head)

	def cli(self, *argv):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = main(list(argv))
		return code, out.getvalue(), err.getvalue()

	def test_approval_fast_forwards_tested_merge_preserves_dirty_files_and_pushes(self):
		(self.repo / "keep.txt").write_text("사용자 작업\n", encoding="utf-8")
		(self.repo / "local.txt").write_text("추적하지 않는 작업\n", encoding="utf-8")
		result = self.approve()
		merged = result["merge_commit"]
		self.assertEqual(result, {"status": "완료", "merge_commit": merged, "pushed": True,
			"remote_matches": True, "restart_notice": self.bundle["restart_notice"]})
		self.assertEqual(self.git("rev-parse", "HEAD"), merged)
		self.assertEqual(self.git("show", "-s", "--format=%P", merged).split(), [self.head, self.tip])
		self.assertEqual(self.git("show", "-s", "--format=%s", merged), f"사무실 프로젝트 {self.identity}: 합성 납품")
		self.assertEqual(checks.git(self.remote, "rev-parse", "refs/heads/main").strip(), merged)
		self.assertEqual((self.repo / "value.txt").read_text(encoding="utf-8"), "납품\n")
		self.assertEqual((self.repo / "keep.txt").read_text(encoding="utf-8"), "사용자 작업\n")
		self.assertEqual((self.repo / "local.txt").read_text(encoding="utf-8"), "추적하지 않는 작업\n")
		self.assertEqual(project.load(self.identity)["status"], "완료")
		for text in ("기존 근거", "## 납품", "승인 시각:", self.bundle["hash"][:12], merged,
				"빨리 감기: 완료", "push: 완료", "원격 해시 일치: 일치", "재기동 필요: " + result["restart_notice"]):
			self.assertIn(text, self.report())
		self.assertEqual(stat.S_IMODE((self.folder / "report.md").stat().st_mode), 0o600)
		for call in self.commands.call_args_list:
			self.assertNotIn("reset", call.args[1:])
			if call.args[1] == "push":
				self.assertEqual(call.args[1:], ("push", "origin", "main"))

	def test_merge_only_does_not_push_or_claim_restart(self):
		self.bundle["actions"] = ["merge"]
		self.write_bundle()
		self.assertIn("재기동 안내: 없음", self.cli("deliver", self.identity)[1])
		result = self.approve()
		self.assertEqual((result["status"], result["pushed"], result["remote_matches"], result["restart_notice"]),
			("완료", False, False, ""))
		self.assertEqual(checks.git(self.remote, "rev-parse", "refs/heads/main").strip(), self.head)
		self.assertFalse(any(call.args[1] in ("push", "ls-remote") for call in self.commands.call_args_list))

	def test_restart_only_is_a_notice_without_push_or_service_execution(self):
		self.bundle["actions"] = ["merge", "restart"]
		self.write_bundle()
		popen = subprocess.Popen
		with mock.patch.object(delivery.subprocess, "Popen", wraps=popen) as processes:
			result = self.approve()
		self.assertEqual((result["status"], result["pushed"], result["restart_notice"]),
			("완료", False, self.bundle["restart_notice"]))
		self.assertTrue(all(call.args[0][0] in ("git", sys.executable) for call in processes.call_args_list))
		self.assertFalse(any(call.kwargs.get("shell", False) for call in processes.call_args_list))
		self.assertEqual(checks.git(self.remote, "rev-parse", "refs/heads/main").strip(), self.head)

	def test_bundle_change_during_tests_blocks_delivery(self):
		test = delivery._test
		def change(command, folder):
			test(command, folder)
			self.bundle["actions"] = ["merge"]
			self.write_bundle()
		with mock.patch.object(delivery, "_test", side_effect=change):
			self.assert_blocked(self.approve(), self.head, "납품 묶음 또는 프로젝트 가지가 승인 뒤에 바뀌었습니다")

	def test_branch_change_during_tests_blocks_delivery(self):
		test = delivery._test
		def change(command, folder):
			test(command, folder)
			self.git("update-ref", "refs/heads/" + self.branch, self.head)
		with mock.patch.object(delivery, "_test", side_effect=change):
			self.assert_blocked(self.approve(), self.head, "납품 묶음 또는 프로젝트 가지가 승인 뒤에 바뀌었습니다")

	def test_fast_forward_error_after_head_moves_is_recorded_truthfully(self):
		def fail_after_forward(repo, *args, **kwargs):
			out = checks.git(repo, *args, **kwargs)
			if args[:2] == ("merge", "--ff-only"):
				raise RuntimeError("합성 비공개 원문")
			return out
		self.commands.side_effect = fail_after_forward
		result = self.approve()
		self.assert_blocked(result, result["merge_commit"], "본 가지 빨리 감기 실패")
		self.assertIn("빨리 감기: 완료", project.load(self.identity)["blocked_reason"])
		self.assertIn("빨리 감기: 완료", self.report())
		self.assertNotIn("합성 비공개 원문", self.report())

	def test_report_prefix_is_preserved_byte_for_byte(self):
		original = "# 검수 보고\n\n기존 근거  \n\n\n"
		(self.folder / "report.md").write_text(original, encoding="utf-8")
		self.assertEqual(self.approve()["status"], "완료")
		self.assertTrue(self.report().startswith(original))

	def test_report_write_failure_preserves_actual_delivery_facts_and_exit_code(self):
		before = self.report()
		with mock.patch.object(delivery, "private_write", side_effect=OSError("합성 비공개 원문")):
			code, out, err = self.cli("deliver", self.identity, "--approve", self.bundle["hash"])
		self.assertEqual((code, err), (3, ""))
		reason = project.load(self.identity)["blocked_reason"]
		for value in ("납품 실패:", "빨리 감기: 완료", "push: 완료", "보고서 기록 실패"):
			self.assertIn(value, reason)
		self.assertEqual(project.load(self.identity)["status"], "막힘")
		self.assertEqual(self.git("rev-parse", "HEAD"), checks.git(self.remote, "rev-parse", "refs/heads/main").strip())
		self.assertEqual(self.report(), before)
		self.assertNotIn("합성 비공개 원문", out + reason)

	def test_rejection_report_write_failure_remains_blocked(self):
		with mock.patch.object(delivery, "private_write", side_effect=OSError("합성 쓰기 실패")):
			code, out, err = self.cli("deliver", self.identity, "--reject", "다시 확인하세요")
		self.assertEqual((code, err), (3, ""))
		self.assertIn("납품 반려: 다시 확인하세요; 보고서 기록 실패", out)
		self.commands.assert_not_called()

	def test_wrong_approval_hash_leaves_state_and_main_unchanged(self):
		before, files = self.record(), self.files()
		with self.assertRaises(ValueError):
			delivery.approve(self.identity, "0" * 64)
		self.assertEqual((self.record(), self.files(), self.git("rev-parse", "HEAD")), (before, files, self.head))

	def test_branch_tip_change_before_approval_is_refused_without_transition(self):
		self.git("update-ref", "refs/heads/" + self.branch, self.head)
		before = self.record()
		with self.assertRaisesRegex(ValueError, "가지 끝"):
			self.approve()
		self.assertEqual(self.record(), before)
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

	def test_invalid_bundles_are_refused_without_transition(self):
		original, before = dict(self.bundle), self.record()
		cases = [{"extra": 1}, {"version": 2}, {"version": True}, {"project": "p-20300102-999"},
			{"repo": str(self.remote)}, {"branch": "--all"}, {"branch_tip": "HEAD"}, {"base": "--abort"},
			{"actions": []}, {"actions": ["push", "merge"]}, {"actions": ["merge", "publish"]},
			{"actions": ["merge", "merge"]}, {"criteria": [False]}, {"tests": []}]
		for changes in cases:
			with self.subTest(changes=changes):
				self.bundle = {**original, **changes}
				self.write_bundle()
				with self.assertRaises(ValueError):
					self.approve()
				self.assertEqual(self.record(), before)
		self.bundle = dict(original)
		del self.bundle["made"]
		self.write_bundle()
		with self.assertRaises(ValueError):
			self.approve()
		self.bundle = original
		self.write_bundle()
		path = self.folder / "delivery.json"
		path.write_text(path.read_text(encoding="utf-8").replace("합성 시험", "다른 근거"), encoding="utf-8")
		with self.assertRaises(ValueError):
			self.approve()
		self.assertEqual(self.record(), before)

	def test_missing_malformed_and_symlink_bundle_are_refused_without_transition(self):
		path, before = self.folder / "delivery.json", self.record()
		path.unlink()
		for content in (None, "{", "[]", b"\xff"):
			with self.subTest(content=content), self.assertRaises(ValueError):
				if content is not None:
					path.write_bytes(content if isinstance(content, bytes) else content.encode())
				delivery.load_bundle(self.identity)
		self.write_bundle()
		outside = self.root / "bundle.json"
		path.rename(outside)
		path.symlink_to(outside)
		with self.assertRaises(ValueError):
			self.approve()
		self.assertEqual(self.record(), before)

	def test_staged_changes_are_blocked_before_temporary_merge(self):
		(self.repo / "keep.txt").write_text("스테이징 보존\n", encoding="utf-8")
		self.git("add", "--", "keep.txt")
		before = (self.repo / ".git/index").read_bytes()
		self.assert_blocked(self.approve(), self.head, "본 저장소에 스테이징된 변경")
		self.assertEqual((self.repo / ".git/index").read_bytes(), before)
		self.assertFalse(any(call.args[1:3] == ("worktree", "add") for call in self.commands.call_args_list))

	def test_detached_main_is_blocked_without_touching_it(self):
		self.git("checkout", "-q", "--detach")
		self.assert_blocked(self.approve(), self.head, "본 저장소 HEAD가 가지를 가리키지 않습니다")

	def test_conflicting_merge_is_aborted_and_main_preserved(self):
		(self.repo / "value.txt").write_text("본 가지 변경\n", encoding="utf-8")
		self.git("add", "--", "value.txt")
		self.git("commit", "-q", "-m", "합성 충돌")
		head, before = self.git("rev-parse", "HEAD"), self.files()
		self.assert_blocked(self.approve(), head, "병합 충돌")
		self.assertEqual(self.files(), before)
		self.assertTrue(any(call.args[1:] == ("merge", "--abort") for call in self.commands.call_args_list))

	def test_tests_run_on_combined_merge_and_failure_keeps_main(self):
		(self.repo / "keep.txt").write_text("본 가지의 별도 변경\n", encoding="utf-8")
		self.git("add", "--", "keep.txt")
		self.git("commit", "-q", "-m", "합성 시험 조건 변경")
		head, before = self.git("rev-parse", "HEAD"), self.files()
		self.assert_blocked(self.approve(), head, "병합 결과 시험 실패")
		self.assertEqual(self.files(), before)

	def test_dirty_overlap_is_blocked_and_work_is_preserved(self):
		(self.repo / "value.txt").write_text("미커밋 보존\n", encoding="utf-8")
		before = self.files()
		self.assert_blocked(self.approve(), self.head, "작업 중인 파일과 병합 변경이 겹칩니다")
		self.assertEqual(self.files(), before)

	def test_ignored_user_file_is_never_overwritten_by_fast_forward(self):
		(self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
		self.git("add", "--", ".gitignore")
		self.git("commit", "-q", "-m", "합성 무시 규칙")
		head = self.git("rev-parse", "HEAD")
		self.git("checkout", "-q", self.branch)
		(self.repo / "ignored.txt").write_text("납품 파일", encoding="utf-8")
		self.git("add", "--", "ignored.txt")
		self.git("commit", "-q", "-m", "합성 파일 추가")
		self.bundle["branch_tip"] = self.git("rev-parse", "HEAD")
		self.git("checkout", "-q", "main")
		self.write_bundle()
		(self.repo / "ignored.txt").write_text("사용자 무시 파일 보존", encoding="utf-8")
		before = self.files()
		self.assert_blocked(self.approve(), head, "본 가지 빨리 감기 실패")
		self.assertEqual(self.files(), before)

	def test_test_command_start_failure_removes_temporary_worktree(self):
		self.configure(shlex.quote(str(self.root / "missing-command")))
		self.assert_blocked(self.approve(), self.head, "병합 결과 시험 실행 실패")

	def branch_files(self):
		self.git("checkout", "-q", self.branch)
		(self.repo / "nested").mkdir()
		(self.repo / "nested/item.txt").write_text("합성 파일", encoding="utf-8")
		(self.repo / "__pycache__").mkdir()
		(self.repo / "__pycache__/sample.pyc").write_text("합성 캐시", encoding="utf-8")
		self.git("add", "--", "nested/item.txt", "__pycache__/sample.pyc")
		self.git("commit", "-q", "-m", "합성 경로 추가")
		self.bundle["branch_tip"] = self.git("rev-parse", "HEAD")
		self.git("checkout", "-q", "main")
		self.write_bundle()

	def test_directory_overlap_follows_apply_rules(self):
		self.branch_files()
		(self.repo / "nested").write_text("디렉터리 자리에 사용자 파일", encoding="utf-8")
		before = self.files()
		self.assert_blocked(self.approve(), self.head, "작업 중인 파일과 병합 변경이 겹칩니다")
		self.assertEqual(self.files(), before)

	def test_artifact_overlap_is_included(self):
		self.branch_files()
		(self.repo / "__pycache__").mkdir()
		(self.repo / "__pycache__/sample.pyc").write_text("미커밋 캐시 보존", encoding="utf-8")
		self.assert_blocked(self.approve(), self.head, "작업 중인 파일과 병합 변경이 겹칩니다")

	def test_main_moving_after_approval_is_not_overwritten(self):
		new_head = self.git("commit-tree", self.head + "^{tree}", "-p", self.head, "-m", "합성 동시 변경")
		test = delivery._test
		def move(command, folder):
			test(command, folder)
			self.git("update-ref", "refs/heads/main", new_head)
		with mock.patch.object(delivery, "_test", side_effect=move):
			self.assert_blocked(self.approve(), new_head, "본 가지가 승인 뒤에 바뀌었습니다. 다시 검수하세요")
		self.assertFalse(any(call.args[1:3] == ("merge", "--ff-only") for call in self.commands.call_args_list))

	def test_push_hook_rejection_keeps_merge_and_records_partial_delivery(self):
		hook = self.remote / "hooks/pre-receive"
		hook.write_text("#!/bin/sh\nprintf '합성 비공개 원문' >&2\nexit 1\n", encoding="utf-8")
		hook.chmod(0o700)
		result = self.approve()
		self.assert_blocked(result, result["merge_commit"], "푸시 실패")
		self.assertFalse(result["pushed"])
		self.assertEqual(checks.git(self.remote, "rev-parse", "refs/heads/main").strip(), self.head)
		self.assertIn("빨리 감기: 완료", project.load(self.identity)["blocked_reason"])
		self.assertIn("push: 안 됨", self.report())
		self.assertNotIn("합성 비공개 원문", self.report() + project.load(self.identity)["blocked_reason"])

	def test_remote_hash_mismatch_is_blocked_after_push(self):
		def mismatch(repo, *args, **kwargs):
			if args[:1] == ("ls-remote",):
				return "0" * 40 + "\trefs/heads/main\n"
			return checks.git(repo, *args, **kwargs)
		self.commands.side_effect = mismatch
		result = self.approve()
		self.assert_blocked(result, result["merge_commit"], "원격 해시 불일치")
		self.assertTrue(result["pushed"])
		self.assertFalse(result["remote_matches"])
		self.assertIn("push: 완료", self.report())

	def test_test_timeout_removes_temporary_worktree(self):
		process = mock.MagicMock()
		process.__enter__.return_value = process
		process.wait.side_effect = [subprocess.TimeoutExpired("합성 시험", 1200), 0]
		popen_real = subprocess.Popen
		with mock.patch.object(delivery.subprocess, "Popen", return_value=process) as popen, \
				mock.patch.object(delivery.os, "killpg") as kill:
			# Git은 실제로 실행하고 시험 프로세스만 대역으로 바꾼다.
			popen.side_effect = lambda *a, **k: popen_real(*a, **k) if a[0][0] == "git" else process
			self.assert_blocked(self.approve(), self.head, "병합 결과 시험 시간 초과")
			kill.assert_called_once()
		self.assertIn(mock.call(timeout=1200), process.wait.call_args_list)

	def test_reject_only_changes_project_and_appends_report(self):
		before, files = self.git("rev-parse", "HEAD"), self.files()
		row = delivery.reject(self.identity, "기준을 보완하세요")
		self.assertEqual((row["status"], row["blocked_reason"]), ("막힘", "납품 반려: 기준을 보완하세요"))
		self.assertEqual((self.git("rev-parse", "HEAD"), self.files()), (before, files))
		self.assertIn("## 반려", self.report())
		self.assertIn("기존 근거", self.report())
		self.commands.assert_not_called()

	def test_invalid_reject_reason_and_wrong_state_are_refused_without_change(self):
		before = self.record()
		for reason in ("", " " * 3, "가" * 1001, "\x00", None):
			with self.subTest(reason=reason), self.assertRaises(ValueError):
				delivery.reject(self.identity, reason)
			self.assertEqual(self.record(), before)
		project.transition(self.identity, "실행 중")
		before = self.record()
		for operation in (lambda: self.approve(), lambda: delivery.reject(self.identity, "이유")):
			with self.assertRaises(ValueError):
				operation()
			self.assertEqual(self.record(), before)

	def test_cli_shows_bundle_and_full_approval_hash_without_changes(self):
		before = self.record()
		code, out, err = self.cli("deliver", self.identity)
		self.assertEqual((code, err), (0, ""))
		for value in ("가지: " + self.branch, "파일 변경: 통과", "시험 결과: 통과 · 합성 시험 통과", "바뀐 파일: 1개",
				"본 가지 병합", "push 및 원격 해시 확인", "재기동 안내만", "재기동 필요:",
				f"python3 -m agency deliver {self.identity} --approve {self.bundle['hash']}"):
			self.assertIn(value, out)
		self.assertEqual(self.record(), before)

	def test_cli_arguments_errors_and_exception_codes(self):
		for argv in ([], ["other"], ["deliver"], ["deliver", self.identity, "--approve"],
				["deliver", self.identity, "--approve", "hash", "--reject", "이유"], ["deliver", self.identity, "extra"]):
			with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
				self.assertEqual(deliver_cli.main(argv), 2)
		code, out, err = self.cli("deliver", self.identity, "--approve", "wrong")
		self.assertEqual((code, out), (1, ""))
		self.assertTrue(err.startswith("오류: "))
		for exception in (ValueError("합성 오류"), ConfigError("합성 오류"), RuntimeError("합성 오류")):
			with self.subTest(exception=type(exception)), mock.patch.object(delivery, "load_bundle", side_effect=exception):
				code, out, err = self.cli("deliver", self.identity)
				self.assertEqual((code, out, err), (1, "", "오류: 합성 오류\n"))

	def test_cli_approval_failure_exit_code(self):
		(self.repo / "value.txt").write_text("미커밋 작업", encoding="utf-8")
		code, out, err = self.cli("deliver", self.identity, "--approve", self.bundle["hash"])
		self.assertEqual((code, err), (3, ""))
		self.assertIn("상태: 막힘", out)

	def test_cli_approval_success_exit_code(self):
		code, out, err = self.cli("deliver", self.identity, "--approve", self.bundle["hash"])
		self.assertEqual((code, err), (0, ""))
		self.assertIn("상태: 완료", out)
		self.assertIn("재기동 필요: ", out)

	def test_cli_rejection_is_blocked_exit_code(self):
		code, out, err = self.cli("deliver", self.identity, "--reject", "다시 확인하세요")
		self.assertEqual((code, err), (3, ""))
		self.assertIn("납품 반려: 다시 확인하세요", out)

if __name__ == "__main__":
	unittest.main()
