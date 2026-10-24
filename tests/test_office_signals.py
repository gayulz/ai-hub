"""합성 세션·DB의 시각과 메타데이터만으로 신호와 읽기 경계를 확인한다."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from office import signals


class NaverTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.blog = Path(self.tmp.name) / "acme"
		self.path = self.blog / ".blogops/blogops.db"
		self.path.parent.mkdir(parents=True)
		self.now = datetime(2026, 10, 24, 12, tzinfo=timezone(timedelta(hours=9)))
		with closing(sqlite3.connect(self.path)) as conn, conn:
			conn.executescript("""
				CREATE TABLE job_run(kind TEXT, status TEXT, started_at TEXT, finished_at TEXT, detail TEXT);
				CREATE TABLE post_event(platform TEXT, to_state TEXT, created_at TEXT, detail TEXT);
			""")

	def stamp(self, age):
		return (self.now - timedelta(seconds=age)).isoformat()

	def job(self, kind="STAGE", started=60, finished=None, status="DONE"):
		with closing(sqlite3.connect(self.path)) as conn, conn:
			conn.execute("INSERT INTO job_run VALUES (?,?,?,?,?)", (kind, status, self.stamp(started),
				self.stamp(finished) if isinstance(finished, int) else finished, "읽기 금지 합성 내용"))

	def event(self, age=60, platform="NAVER", state="STAGING"):
		with closing(sqlite3.connect(self.path)) as conn, conn:
			conn.execute("INSERT INTO post_event VALUES (?,?,?,?)", (platform, state, self.stamp(age), "읽기 금지 합성 내용"))

	def signal(self):
		before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
		result = signals.naver(self.blog, self.now)
		self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
		self.assertNotIn("읽기 금지", json.dumps(result, ensure_ascii=False))
		return result

	def test_unfinished_stage_uses_start_within_two_hours_regardless_of_status(self):
		self.job(started=3600, finished="")
		self.assertEqual(self.signal(), {"status": "작업 중", "engine": "로컬", "task": "네이버 편집기 올리기", "updated": self.stamp(3600)})

	def test_recent_finished_rank_uses_finish_instead_of_old_start(self):
		self.job("BLOG_RANK", started=86400, finished=600, status="FAILED")
		self.assertEqual(self.signal(), {"status": "작업 중", "engine": "로컬", "task": "검색 순위 관측", "updated": self.stamp(600)})

	def test_only_old_rows_and_unrelated_kinds_stay_idle(self):
		self.job(started=7201)
		self.job("BLOG_RANK", finished=601)
		self.job("DRAFT")
		self.event(age=601)
		self.event(platform="GLOBAL")
		self.event(state="PUBLISHED")
		self.assertEqual(self.signal(), {"status": "대기"})

	def test_recent_staging_and_staged_events_are_work(self):
		for state in ("STAGING", "STAGED"):
			with self.subTest(state=state):
				self.event(state=state)
				self.assertEqual(self.signal(), {"status": "작업 중", "engine": "로컬", "task": "네이버 글 올리는 중", "updated": self.stamp(60)})

	def test_unfinished_then_completed_then_events_take_priority_and_latest_wins(self):
		self.event(age=0)
		self.job("BLOG_RANK", started=86400, finished=10)
		self.job(started=7200)
		self.assertEqual(self.signal()["task"], "네이버 편집기 올리기")
		self.job("BLOG_RANK", started=3600)
		self.assertEqual(self.signal()["task"], "검색 순위 관측")

	def test_future_and_out_of_window_rows_are_not_active(self):
		for finished, age, expected in ((None, 7200, "작업 중"), (None, 7201, "대기"), (None, -1, "대기"),
				(600, 86400, "작업 중"), (601, 86400, "대기"), (-1, 60, "대기")):
			with self.subTest(finished=finished, age=age):
				with closing(sqlite3.connect(self.path)) as conn, conn:
					conn.execute("DELETE FROM job_run")
				self.job(started=age, finished=finished)
				self.assertEqual(self.signal()["status"], expected)
		for age, expected in ((600, "작업 중"), (601, "대기"), (-1, "대기")):
			with self.subTest(event_age=age):
				with closing(sqlite3.connect(self.path)) as conn, conn:
					conn.execute("DELETE FROM job_run")
					conn.execute("DELETE FROM post_event")
				self.event(age=age)
				self.assertEqual(self.signal()["status"], expected)

	def test_missing_db_is_idle_without_creating_files(self):
		self.path.unlink()
		self.assertEqual(signals.naver(self.blog, self.now), {"status": "대기"})
		self.assertEqual(list(self.path.parent.iterdir()), [])

	def test_missing_tables_or_columns_are_unavailable_even_with_an_active_job(self):
		self.job()
		original = self.path.read_bytes()
		for sql in ("ALTER TABLE job_run RENAME COLUMN status TO xyz", "ALTER TABLE post_event RENAME COLUMN created_at TO xyz",
				"DROP TABLE job_run", "DROP TABLE post_event"):
			with self.subTest(sql=sql):
				self.path.write_bytes(original)
				with closing(sqlite3.connect(self.path)) as conn, conn:
					conn.execute(sql)
				self.assertEqual(self.signal(), {"status": "확인 불가", "reason": "네이버 작업 상태를 읽을 수 없습니다"})

	def test_readonly_uri_short_timeout_metadata_columns_and_closed_connection(self):
		self.job()
		self.event()
		connect, connections, queries, reads = sqlite3.connect, [], [], []
		allowed = {"kind", "status", "started_at", "finished_at", "platform", "to_state", "created_at"}
		def authorize(action, table, column, *unused):
			if action == sqlite3.SQLITE_READ:
				reads.append(column)
				return sqlite3.SQLITE_OK if column in allowed else sqlite3.SQLITE_DENY
			return sqlite3.SQLITE_OK
		def open_db(database, **kwargs):
			self.assertEqual(database, self.path.resolve().as_uri() + "?mode=ro")
			self.assertTrue(kwargs["uri"])
			self.assertGreater(kwargs["timeout"], 0)
			self.assertLessEqual(kwargs["timeout"], 1)
			conn = connect(database, **kwargs)
			with self.assertRaises(sqlite3.OperationalError):
				conn.execute("CREATE TABLE xyz(value)")
			conn.set_authorizer(authorize)
			conn.set_trace_callback(queries.append)
			connections.append(conn)
			return conn
		with mock.patch.object(signals.sqlite3, "connect", side_effect=open_db):
			self.assertEqual(self.signal()["status"], "작업 중")
		self.assertEqual(set(reads), allowed)
		self.assertTrue(all(query.startswith("SELECT ") for query in queries))
		with self.assertRaises(sqlite3.ProgrammingError):
			connections[0].execute("SELECT kind FROM job_run")

	def test_open_failure_locked_invalid_db_and_invalid_timestamps_are_unavailable(self):
		for error in (PermissionError(), sqlite3.OperationalError("합성 실패")):
			with self.subTest(error=type(error)), mock.patch.object(signals.sqlite3, "connect", side_effect=error):
				self.assertEqual(self.signal()["status"], "확인 불가")
		with closing(sqlite3.connect(self.path)) as conn, conn:
			conn.execute("BEGIN EXCLUSIVE")
			self.assertEqual(self.signal()["status"], "확인 불가")
			conn.rollback()
		self.job()
		with closing(sqlite3.connect(self.path)) as conn, conn:
			conn.execute("UPDATE job_run SET started_at='2026-10-24T12:00:00'")
		self.assertEqual(self.signal()["status"], "확인 불가")
		self.path.write_bytes(b"acme")
		self.assertEqual(self.signal()["status"], "확인 불가")


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
