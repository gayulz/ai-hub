"""합성 임시 폴더와 가짜 subprocess로 검사한다. 실제 AI·사용자 설정은 읽지 않는다.

회의는 write에서 재시도·불참 없이 failed가 된다. 현재 Flow는 write도 대체 검토자로
넘기므로, 마지막 검토자가 실패하면 실행이 실패하지만 대체 검토자가 성공하면 승인 대기가 된다.
호출부는 이번 수정 범위 밖이며 이 차이를 아래 테스트로 명시한다.
"""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from collector.config import Plan, Plans
from orchestrator import flow, meeting, meeting_record, providers, readonly_guard as guard
from orchestrator.record import Run

REAL_RUN = subprocess.run


class ReadonlyGuardTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		# 분류하지 못한 실패는 홈의 logs/ 에 기록되므로, 시험이 실제 ~/.ai-hub 에 쓰지 않게 홈을 임시 폴더로 바꾼다
		self.hub = self.root / "hub"
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.plain, self.repo = self.root / "plain", self.root / "repo"
		self.enterContext(mock.patch("orchestrator.targets.REPO", self.plain))
		self.plain.mkdir()
		self.repo.mkdir()
		(self.repo / ".git").mkdir()
		(self.repo / "tracked.txt").write_text("원본", encoding="utf-8")
		self.tracked = {"tracked.txt": "원본".encode()}
		self.ignored = set()
		self.effect, self.data = None, {"ok": True}
		self.git_calls, self.tool_calls = [], []
		patch = mock.patch.object(subprocess, "run", side_effect=self.fake_run)
		patch.start()
		self.addCleanup(patch.stop)

	def plan(self, tool="agy"):
		return Plan("p-" + tool, tool, "합성", "personal", ("personal",), None, None)

	def git_files(self):
		found = {}
		for folder, dirs, files in os.walk(self.repo, followlinks=False):
			dirs[:] = [name for name in dirs if name != ".git"]
			for name in files + [name for name in dirs if (Path(folder) / name).is_symlink()]:
				path = Path(folder) / name
				found[path.relative_to(self.repo).as_posix()] = (
					os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
		return found

	def fake_run(self, argv, **kwargs):
		if argv[0] == "git":
			self.git_calls.append(argv)
			if argv[3] == "rev-parse":
				where = Path(argv[2])
				inside = where == self.repo or self.repo in where.parents
				return subprocess.CompletedProcess(argv, 0 if inside else 128, os.fsencode(self.repo) + b"\n")
			current = self.git_files()
			if argv[3] == "status":
				output = b"".join((b"?? " if name not in self.tracked else b" M ")
					+ os.fsencode(name) + b"\0" for name in sorted(current)
					if current[name] != self.tracked.get(name) and name not in self.ignored)
				output += b"".join(b" D " + os.fsencode(name) + b"\0" for name in self.tracked.keys() - current.keys())
			elif argv[3] == "diff":
				output = b"".join(os.fsencode(name) + b"\0" + current.get(name, b"")
					for name in sorted(self.tracked) if current.get(name) != self.tracked[name])
			elif argv[3] == "ls-files" and "--ignored" in argv:
				output = b"".join(os.fsencode(name) + b"\0" for name in sorted(self.ignored & current.keys()))
			elif argv[3] == "ls-files":
				output = b"".join(os.fsencode(name) + b"\0" for name in sorted(self.tracked.keys() | current.keys()))
			else:
				raise AssertionError("예상하지 못한 Git 명령")
			return subprocess.CompletedProcess(argv, 0, output)
		self.tool_calls.append(argv)
		if self.effect is not None:
			result = self.effect()
			if isinstance(result, subprocess.CompletedProcess):
				return result
		if argv[0] == "codex":
			Path(argv[argv.index("-o") + 1]).write_text(json.dumps(self.data), encoding="utf-8")
		return subprocess.CompletedProcess(argv, 0, json.dumps({"structured_output": self.data}))

	def call(self, cwd=None, tool="agy", writer=False):
		return providers.call(self.plan(tool), writer, "합성 질문", cwd or self.plain, {}, 60)

	def assert_write(self, callback):
		with self.assertRaises(providers.ProviderError) as caught:
			callback()
		self.assertEqual(caught.exception.kind, "write")
		self.assertNotIn(str(self.root), str(caught.exception))
		return str(caught.exception)

	def test_new_file_is_reported_and_removed_in_git_and_plain_folders(self):
		for cwd in (self.repo, self.plain):
			with self.subTest(git=cwd == self.repo):
				new = cwd / "probe.txt"
				self.effect = lambda: new.write_text("합성 파일")
				with mock.patch.object(providers, "_unwrap") as unwrap:
					message = self.assert_write(lambda: self.call(cwd))
				unwrap.assert_not_called()
				self.assertEqual(message, "읽기 전용 호출이 파일을 바꿨습니다: probe.txt")
				self.assertFalse(new.exists())

	def test_empty_git_directory_in_a_parent_is_not_mistaken_for_a_repository(self):
		# 홈 폴더처럼 내용이 빈 .git 이 상위에 있어도 회의 기록 폴더는 일반 폴더로 검사해야 한다(실제 git 사용).
		bogus = self.root / "bogus"
		folder = bogus / "meetings" / "m-sample"
		folder.mkdir(parents=True)
		(bogus / ".git").mkdir()
		(folder / "meeting.json").write_text("{}", encoding="utf-8")
		with mock.patch.object(subprocess, "run", side_effect=REAL_RUN):
			before = guard.snapshot(folder)
			self.assertIsNotNone(before)
			self.assertIsNone(before.git)
			self.assertIn("meeting.json", before.files)
			(folder / "stray.txt").write_text("새 파일", encoding="utf-8")
			self.assert_write(lambda: guard.verify(folder, before, "agy"))
		self.assertFalse((folder / "stray.txt").exists())

	def test_real_git_repository_is_checked_by_git_when_a_parent_folder_is_a_symlink(self):
		# macOS 임시 경로(/var → /private/var)처럼 상위 폴더가 링크여도 git 이 돌려주는 실제 경로와 맞춰 검사해야 한다.
		parent = self.root / "parent-real"
		repo = parent / "repo"
		repo.mkdir(parents=True)
		(self.root / "parent-link").symlink_to(parent, target_is_directory=True)
		link = self.root / "parent-link" / "repo"
		with mock.patch.object(subprocess, "run", side_effect=REAL_RUN):
			subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
			(repo / "tracked.txt").write_text("원본", encoding="utf-8")
			subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, capture_output=True)
			subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
				"commit", "-q", "-m", "합성"], check=True, capture_output=True)
			before = guard.snapshot(link)
			self.assertIsNotNone(before)
			self.assertIsNotNone(before.git)
			self.assertIn("tracked.txt", before.files)
			(repo / "added.txt").write_text("새 파일", encoding="utf-8")
			self.assert_write(lambda: guard.verify(link, before, "agy"))
		self.assertFalse((repo / "added.txt").exists())

	def test_existing_tracked_file_is_reported_and_preserved(self):
		path = self.repo / "tracked.txt"
		self.effect = lambda: path.write_text("바뀐 내용은 보존한다")
		self.assertIn("tracked.txt", self.assert_write(lambda: self.call(self.repo)))
		self.assertEqual(path.read_text(), "바뀐 내용은 보존한다")

	def test_existing_dirty_and_untracked_files_are_preserved(self):
		for cwd, name in ((self.repo, "tracked.txt"), (self.repo, "existing.txt"), (self.plain, "existing.txt")):
			with self.subTest(cwd=cwd.name, name=name):
				path = cwd / name
				path.write_text("호출 전에 있던 변경")
				self.effect = lambda: path.write_text("호출 중에 바뀐 내용은 남긴다")
				self.assertIn(name, self.assert_write(lambda: self.call(cwd)))
				self.assertEqual(path.read_text(), "호출 중에 바뀐 내용은 남긴다")

	def test_existing_ignored_file_is_not_deleted_when_it_becomes_untracked(self):
		path = self.repo / "existing.txt"
		path.write_text("호출 전부터 있던 파일")
		self.ignored.add("existing.txt")
		self.effect = self.ignored.clear
		self.assert_write(lambda: self.call(self.repo))
		self.assertEqual(path.read_text(), "호출 전부터 있던 파일")

	def test_changes_only_in_gitignored_files_are_not_a_write_but_other_new_files_are(self):
		(self.repo / "cache").mkdir()
		old = self.repo / "cache" / "old.pyc"
		old.write_text("호출 전부터 있던 캐시")
		self.ignored.update({"cache/old.pyc", "cache/new.pyc"})
		def effect():
			old.write_text("캐시가 다시 만들어졌다")
			(self.repo / "cache" / "new.pyc").write_text("새 캐시")
		self.effect = effect
		self.assertEqual(self.call(self.repo).data, self.data)
		self.assertEqual((self.repo / "cache" / "new.pyc").read_text(), "새 캐시")  # 무시 대상은 지우지 않는다
		self.effect = lambda: (self.repo / "probe.txt").write_text("무시 대상이 아닌 새 파일")
		self.assertIn("probe.txt", self.assert_write(lambda: self.call(self.repo)))
		self.assertFalse((self.repo / "probe.txt").exists())

	def test_codex_and_claude_changes_are_logged_not_failed_and_files_are_kept(self):
		hub = self.root / "hub"
		for tool in ("codex", "claude"):
			for cwd in (self.repo, self.plain):
				with self.subTest(tool=tool, cwd=cwd.name), mock.patch.dict(os.environ, {"AI_HUB_HOME": str(hub)}):
					other = cwd / f"concurrent-{tool}.txt"
					self.effect = lambda other=other: other.write_text("호출 중 다른 곳에서 만든 파일")
					self.assertEqual(self.call(cwd, tool).data, self.data)
					self.assertTrue(other.exists())  # 새 파일을 지우지 않는다
		lines = (hub / "logs/readonly-guard.log").read_text(encoding="utf-8").splitlines()
		self.assertEqual(len(lines), 4)
		self.assertTrue(all("동시 편집일 수 있음" in line for line in lines))
		self.assertEqual(stat.S_IMODE((hub / "logs/readonly-guard.log").stat().st_mode), 0o600)

	def test_agy_still_fails_and_removes_new_files_while_codex_in_the_same_folder_does_not(self):
		with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.root / "hub")}):
			probe = self.repo / "agy-probe.txt"
			self.effect = lambda: probe.write_text("AGY가 만든 파일")
			self.assertIn("agy-probe.txt", self.assert_write(lambda: self.call(self.repo, "agy")))
			self.assertFalse(probe.exists())

	def test_unchanged_call_succeeds_for_all_tools_and_folders(self):
		for cwd in (self.repo, self.plain):
			for tool in ("agy", "codex", "claude"):
				with self.subTest(cwd=cwd.name, tool=tool):
					self.assertEqual(self.call(cwd, tool).data, self.data)

	def test_writer_does_not_snapshot_or_add_git_commands(self):
		for tool in ("agy", "codex", "claude"):
			with self.subTest(tool=tool), mock.patch.object(guard, "snapshot") as snapshot:
				self.git_calls.clear()
				self.assertEqual(self.call(tool=tool, writer=True).data, self.data)
				snapshot.assert_not_called()
				self.assertEqual(len(self.git_calls), int(tool == "codex"))
				if self.git_calls:
					self.assertEqual(self.git_calls[0][3:], ["rev-parse", "--is-inside-work-tree"])

	def test_write_takes_precedence_over_timeout_exit_missing_and_bad_json(self):
		for cwd in (self.repo, self.plain):
			for kind in ("timeout", "exit", "missing", "json"):
				with self.subTest(cwd=cwd.name, kind=kind):
					new = cwd / "probe.txt"
					def fail():
						new.write_text("시간 초과에도 남는 파일")
						if kind == "timeout":
							raise subprocess.TimeoutExpired("합성", 60)
						if kind == "missing":
							raise OSError("합성 실행 오류")
						return subprocess.CompletedProcess([], int(kind == "exit"), "잘못된 응답")
					self.effect = fail
					self.assert_write(lambda: self.call(cwd))
					self.assertFalse(new.exists())

	def test_unchanged_errors_keep_their_original_kinds(self):
		for kind in ("timeout", "exit", "missing", "json"):
			with self.subTest(kind=kind):
				def fail():
					if kind == "timeout":
						raise subprocess.TimeoutExpired("합성", 60)
					if kind == "missing":
						raise OSError("합성")
					return subprocess.CompletedProcess([], int(kind == "exit"), "잘못된 응답")
				self.effect = fail
				with self.assertRaises(providers.ProviderError) as caught:
					self.call()
				self.assertEqual(caught.exception.kind, kind)

	def test_entry_limit_blocks_agy_before_execution_but_allows_codex_without_verification(self):
		for name in ("a.txt", "b.txt"):
			(self.plain / name).write_text("합성")
		with mock.patch.object(guard, "MAX_ENTRIES", 1):
			self.assertIsNone(guard.snapshot(self.plain))
			message = self.assert_write(self.call)
			self.assertEqual(message, "읽기 전용을 검사할 수 없는 폴더입니다")
			self.assertEqual(self.tool_calls, [])
			# 저장소인지 묻는 rev-parse 만 부르고 상태·차이는 읽지 않는다.
			self.assertTrue(all(call[3] == "rev-parse" for call in self.git_calls))
			with mock.patch.object(guard, "snapshot", wraps=guard.snapshot) as snapshot:
				self.assertEqual(self.call(tool="codex").data, self.data)
				snapshot.assert_called_once_with(self.plain)
		self.assertEqual(len(self.tool_calls), 1)

	def test_failed_git_command_is_uncheckable_and_blocks_agy(self):
		original = self.fake_run
		for command in ("status", "diff", "ls-files"):
			with self.subTest(command=command):
				def fail(argv, **kwargs):
					if argv[0] == "git" and argv[3] == command:
						return subprocess.CompletedProcess(argv, 1, b"", "합성 실패".encode())
					return original(argv, **kwargs)
				with mock.patch.object(subprocess, "run", side_effect=fail):
					self.assertIsNone(guard.snapshot(self.repo))
					self.assert_write(lambda: self.call(self.repo))
		self.assertEqual(self.tool_calls, [])

	def test_git_records_hashes_of_exact_status_and_binary_diff(self):
		before = guard.snapshot(self.repo)
		self.assertEqual(before.git, (hashlib.sha256(b"").hexdigest(),) * 2)
		self.assertIn(["git", "-C", str(self.repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"], self.git_calls)
		self.assertIn(["git", "-C", str(self.repo), "diff", "HEAD", "--binary"], self.git_calls)

	def test_error_lists_at_most_five_relative_names_and_remaining_count(self):
		for cwd in (self.repo, self.plain):
			with self.subTest(git=cwd == self.repo):
				def create():
					for index in range(7):
						(cwd / f"new-{index}.txt").write_text("합성")
				self.effect = create
				message = self.assert_write(lambda: self.call(cwd))
				self.assertEqual(message, "읽기 전용 호출이 파일을 바꿨습니다: "
					+ ", ".join(f"new-{index}.txt" for index in range(5)) + " 외 2개")
				self.assertNotIn("~/", message)

	def test_git_subdirectory_names_are_relative_to_cwd(self):
		cwd = self.repo / "nested"
		cwd.mkdir()
		self.effect = lambda: (cwd / "probe.txt").write_text("합성")
		self.assertEqual(self.assert_write(lambda: self.call(cwd)), "읽기 전용 호출이 파일을 바꿨습니다: probe.txt")
		self.assertFalse((cwd / "probe.txt").exists())

	def test_tree_records_metadata_and_stops_at_depth_and_entry_limits(self):
		path = self.plain / "a.txt"
		path.write_text("합성")
		info = path.lstat()
		self.assertEqual(guard.snapshot(self.plain).files["a.txt"], (stat.S_IFREG, info.st_size, info.st_mtime_ns))
		with mock.patch.object(guard, "MAX_ENTRIES", 1):
			self.assertIsNotNone(guard.snapshot(self.plain))
		for _ in range(guard.MAX_DEPTH):
			path = path.parent / "nested" if path.is_file() else path / "nested"
			path.mkdir()
		self.assertIsNotNone(guard.snapshot(self.plain))
		(path / "too-deep.txt").write_text("합성")
		self.assertIsNone(guard.snapshot(self.plain))

	def test_symlinks_are_recorded_without_following_targets_or_cycles(self):
		outside = self.root / "outside"
		outside.mkdir()
		victim = outside / "keep.txt"
		victim.write_text("보존")
		for cwd in (self.repo, self.plain):
			with self.subTest(git=cwd == self.repo):
				(cwd / "link").symlink_to(outside, target_is_directory=True)
				(cwd / "cycle").symlink_to(cwd, target_is_directory=True)
				before = guard.snapshot(cwd)
				self.assertIsNotNone(before)
				self.assertNotIn("link/keep.txt", before.files)
				victim.write_text("링크 밖 변경은 검사하지 않는다")
				self.assertEqual(guard.snapshot(cwd), before)
				guard._remove_new(cwd, {"link/keep.txt", "link", "cycle"})
				self.assertTrue(victim.exists())
				self.assertTrue((cwd / "link").is_symlink())

	def test_new_links_and_directories_are_preserved_but_new_regular_files_are_removed(self):
		for cwd in (self.repo, self.plain):
			with self.subTest(git=cwd == self.repo):
				def create():
					(cwd / "folder").mkdir()
					(cwd / "folder/new.txt").write_text("합성")
					(cwd / "link").symlink_to(self.root / "missing")
				self.effect = create
				self.assert_write(lambda: self.call(cwd))
				self.assertTrue((cwd / "folder").is_dir())
				self.assertTrue((cwd / "link").is_symlink())
				self.assertFalse((cwd / "folder/new.txt").exists())

	def test_uncheckable_after_call_is_a_write_failure(self):
		before = guard.snapshot(self.plain)
		with mock.patch.object(guard, "snapshot", side_effect=[before, None]):
			self.assert_write(self.call)

	def test_meeting_write_fails_without_retry_or_absence_in_every_phase(self):
		for phase in ("analysis", "round1", "round2", "summary"):
			with self.subTest(phase=phase):
				state = meeting_record.new("합성 안건", "synthetic")
				state["participants"] = [{"role": "backend"}, {"role": "qa"}]
				state["rounds"]["1"] = [{"role": "backend"}, {"role": "qa"}]
				record = SimpleNamespace(folder=self.plain, save=mock.Mock())
				session = meeting.Session(record, state, None)
				session.prepared = (None, {role: self.plan() for role in ("moderator", "backend", "qa")}, "합성 규칙")
				self.effect = lambda: (self.plain / "probe.txt").write_text("합성")
				self.tool_calls.clear()
				with mock.patch.object(meeting.prompts, "schema", return_value={}), \
						mock.patch.object(meeting.prompts, "prompt", return_value="합성 질문"), \
						mock.patch.object(meeting.cards, "text"):
					self.assertIsNone(session.ask(phase, "backend" if phase.startswith("round") else None))
				self.assertEqual(state["status"], "failed")
				self.assertEqual(state["absent"], [])
				self.assertEqual(len(state["calls"]), 1)
				self.assertEqual(len(self.tool_calls), 1)

	def review(self, fallback):
		self.data = {"verdict": "pass", "summary": "합성 검토", "findings": []}
		run = Run("synthetic-run", {"goal": "합성 분석", "done": ["합성 검토"], "owned_paths": []},
			str(self.plain), kind="analysis", worktree=str(self.plain),
			step="review", document="합성 분석", checks=[{"name": "문서", "ok": True}])
		plans = Plans({p.id: p for p in (self.plan(), self.plan("codex"))}, {})
		config = {"reviewer": [self.plan().id] + ([self.plan("codex").id] if fallback else [])}
		def write_once():
			if len(self.tool_calls) == 1:
				(self.plain / "probe.txt").write_text("합성")
		self.effect = write_once
		with mock.patch("orchestrator.record.runs_dir", return_value=self.root / "runs"):
			run.save()
			return flow.Flow(run, plans, config).run_all()

	def test_review_write_failure_ends_run_when_no_fallback_is_configured(self):
		run = self.review(False)
		self.assertEqual(run.status, "실패")
		self.assertEqual(len(run.calls), 1)
		self.assertTrue(run.calls[0]["error"].startswith("write:"))

	def test_review_currently_falls_back_after_write_and_can_approve(self):
		"""현재 호출부의 한계: write도 다음 검토자로 넘긴다. 호출부 수정이 필요하다."""
		run = self.review(True)
		self.assertEqual(run.status, "승인 필요")
		self.assertEqual([c["tool"] for c in run.calls], ["agy", "codex"])
		self.assertTrue(run.calls[0]["error"].startswith("write:"))


if __name__ == "__main__":
	unittest.main()
