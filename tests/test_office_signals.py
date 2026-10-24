"""임시 세션의 시각·메타데이터만으로 숏폼 신호와 읽기 경계를 확인한다."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from office import signals


class ShortsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.user = Path(self.tmp.name).resolve() / "p01"
		self.root = self.user / ".codex/sessions"
		self.root.mkdir(parents=True)
		self.projects = self.user / ".codex/.chatgpt-projects"
		self.now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

	def session(self, name, source="cli", age=60, cwd=None):
		path = self.root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"type": "session_meta", "payload": {
			"cwd": str(cwd or self.projects / "acme"), "source": source}}) + "\n노출 금지 합성 본문", encoding="utf-8")
		os.utime(path, (self.now.timestamp() - age,) * 2)
		return path

	def test_only_recent_jsonl_files_are_opened_and_root_is_checked_once(self):
		for index in range(40):
			self.session(f"2026/10/23/old-{index}.jsonl", age=601 + index)
		self.session("2026/10/23/future.jsonl", age=-1)
		self.session("2026/10/23/xyz.txt")
		path = self.session("2026/10/23/acme.jsonl")
		os.utime(path.parent, (self.now.timestamp() - 86400,) * 2)
		with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened, \
				mock.patch.object(signals, "safe", wraps=signals.safe) as safe, \
				mock.patch.object(signals, "regular", side_effect=AssertionError("파일별 부모 검사 금지")):
			result = signals.shorts(self.user, self.now)
		self.assertEqual(result, {"status": "정상", "main": (self.now - timedelta(seconds=60)).isoformat(), "auxiliary": None})
		opened.assert_called_once_with(path, encoding="utf-8")
		safe.assert_called_once_with(self.root)

	def test_symlinks_and_protected_names_are_neither_opened_nor_traversed(self):
		path = self.session("acme/main.jsonl")
		self.session("🔐acme/xyz.jsonl")
		self.session("🔐xyz.jsonl")
		outside = self.user / "xyz"
		outside.mkdir()
		target = outside / "p01.jsonl"
		target.write_text("읽기 금지 합성 자료", encoding="utf-8")
		(self.root / "linked.jsonl").symlink_to(target)
		(self.root / "linked").symlink_to(outside, target_is_directory=True)
		(self.root / "loop").symlink_to(self.root, target_is_directory=True)
		(self.root / "missing.jsonl").symlink_to(outside / "missing.jsonl")
		with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened, \
				mock.patch.object(signals.os, "scandir", wraps=os.scandir) as scanned:
			result = signals.shorts(self.user, self.now)
		self.assertEqual(result["status"], "정상")
		opened.assert_called_once_with(path, encoding="utf-8")
		self.assertEqual({Path(call.args[0]) for call in scanned.call_args_list}, {self.root, path.parent})

	def test_recent_main_and_child_threads_are_classified_without_parent_sessions(self):
		for source, cwd, key in (("cli", self.projects / "acme", "main"),
				({"subagent": {}}, self.projects / "xyz", "auxiliary"),
				({}, self.projects / "xyz", "auxiliary"),
				(None, self.projects / "acme", None), (12, self.projects / "acme", None),
				("cli", self.user / "work/acme", None),
				({}, self.user / ".codex/.chatgpt-projects-xyz/acme", None),
				({}, self.projects / "🔐xyz/acme", None)):
			with self.subTest(source=source, cwd=cwd):
				self.session("acme.jsonl", source=source, cwd=cwd)
				result = signals.shorts(self.user, self.now)
				expected = {"status": "정상", "main": None, "auxiliary": None}
				if key:
					expected[key] = (self.now - timedelta(seconds=60)).isoformat()
				self.assertEqual(result, expected)

	def test_ten_minute_boundary_and_latest_signal_are_preserved(self):
		for age, active in ((600, True), (601, False), (-1, False)):
			with self.subTest(age=age):
				self.session("acme.jsonl", age=age)
				result = signals.shorts(self.user, self.now)
				self.assertEqual(result, {"status": "정상", "main":
					(self.now - timedelta(seconds=age)).isoformat() if active else None, "auxiliary": None})
		self.session("acme.jsonl", age=600)
		self.session("xyz.jsonl", age=30)
		self.session("child-old.jsonl", source={}, age=300)
		self.session("child-new.jsonl", source={}, age=60)
		self.assertEqual(signals.shorts(self.user, self.now), {"status": "정상",
			"main": (self.now - timedelta(seconds=30)).isoformat(),
			"auxiliary": (self.now - timedelta(seconds=60)).isoformat()})

	def test_only_one_bounded_metadata_line_is_read(self):
		self.session("acme.jsonl")
		line = json.dumps({"type": "session_meta", "payload": {"cwd": str(self.projects / "acme"), "source": "cli"}})
		for size, status in ((65536, "정상"), (65537, "확인 불가")):
			with self.subTest(size=size), mock.patch.object(Path, "open") as opened:
				source = opened.return_value.__enter__.return_value
				source.readline.return_value = line + " " * (size - len(line))
				result = signals.shorts(self.user, self.now)
				self.assertEqual(result["status"], status)
				self.assertEqual(source.mock_calls, [mock.call.readline(65537)])

	def test_recent_malformed_or_unreadable_metadata_is_unavailable(self):
		path = self.session("acme.jsonl")
		for line in ("{", "[]", '{"payload": []}', '{"payload": {"cwd": []}}'):
			with self.subTest(line=line):
				path.write_text(line, encoding="utf-8")
				os.utime(path, (self.now.timestamp(),) * 2)
				self.assertEqual(signals.shorts(self.user, self.now)["status"], "확인 불가")
		with mock.patch.object(Path, "open", side_effect=PermissionError("합성 실패")):
			self.assertEqual(signals.shorts(self.user, self.now)["status"], "확인 불가")
		with mock.patch.object(signals.os, "scandir", side_effect=PermissionError("합성 실패")):
			self.assertEqual(signals.shorts(self.user, self.now)["status"], "확인 불가")

	def test_missing_root_is_idle_and_unsafe_root_is_unavailable_without_scanning(self):
		self.root.rmdir()
		self.assertEqual(signals.shorts(self.user, self.now), {"status": "정상", "main": None, "auxiliary": None})
		outside = self.user / "xyz"
		outside.mkdir()
		self.root.symlink_to(outside, target_is_directory=True)
		with mock.patch.object(signals.os, "scandir") as scanned:
			self.assertEqual(signals.shorts(self.user, self.now)["status"], "확인 불가")
			self.assertEqual(signals.shorts(self.user / "🔐acme", self.now)["status"], "확인 불가")
		scanned.assert_not_called()
