"""대화 보관 테스트. 임시 폴더의 합성 자료만 쓴다."""

import contextlib
import gzip
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from collector import __main__ as cli
from collector import archive


class ArchiveTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name)
		self.hub = self.root / "hub"
		self.destination = self.hub / "archive"
		self.roots = {
			"claude": self.root / "original/claude/projects",
			"codex": self.root / "original/codex",
			"gemini": self.root / "original/gemini/tmp",
			"agy": self.root / "original/gemini",
		}
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(archive, "ROOTS", self.roots)):
			patch.start()
			self.addCleanup(patch.stop)

	def write(self, tool, relative, content=b'{"text":"acme"}\n'):
		path = self.roots[tool] / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)
		return path

	def target(self, tool, relative):
		return self.destination / tool / (relative if relative.endswith(".zst") else relative + ".gz")

	def run_archive(self):
		self.output = io.StringIO()
		with contextlib.redirect_stdout(self.output):
			return archive.archive(self.roots)

	def read_database(self, path):
		with tempfile.TemporaryDirectory(dir=self.root) as folder:
			restored = Path(folder) / "restored.db"
			with gzip.open(path, "rb") as incoming, restored.open("wb") as outgoing:
				shutil.copyfileobj(incoming, outgoing)
			connection = sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)
			try:
				return connection.execute("select body from messages order by rowid").fetchall()
			finally:
				connection.close()

	def database(self, relative, wal=False):
		path = self.roots["agy"] / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		connection = sqlite3.connect(path)
		if wal:
			connection.execute("pragma journal_mode=wal")
			connection.execute("pragma wal_autocheckpoint=0")
		connection.execute("create table messages (body text)")
		connection.commit()
		return path, connection

	def test_new_gzip_matches_source_and_directories_are_private(self):
		relative = "acme/main.jsonl"
		source = self.write("claude", relative, b"acme\x00xyz\n" * 100)
		result = self.run_archive()
		target = self.target("claude", relative)
		self.assertEqual(gzip.decompress(target.read_bytes()), source.read_bytes())
		self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
		self.assertEqual(target.stat().st_mode & 0o777, 0o600)
		for path in (self.destination, *(p for p in self.destination.rglob("*") if p.is_dir())):
			self.assertEqual(path.stat().st_mode & 0o777, 0o700)
		self.assertEqual(result, archive.ArchiveStats(new=1, written_bytes=target.stat().st_size))
		self.assertEqual(self.output.getvalue().splitlines(), [result.summary()])
		self.assertFalse(target.read_bytes()[3] & 8)  # gzip 머리말에 파일 경로를 넣지 않는다.

	def test_all_targets_keep_relative_paths_and_exclude_other_files(self):
		included = {
			"claude": ("acme/main.jsonl", "acme/subagents/xyz.jsonl"),
			"codex": ("sessions/2026/10/22/acme.jsonl", "archived_sessions/xyz.jsonl"),
			"gemini": ("acme/chats/main.json", "acme/chats/subagents/xyz.jsonl"),
			"agy": ("antigravity/conversations/acme.pb",),
		}
		excluded = {
			"claude": ("acme/config.json",),
			"codex": ("memories/acme.jsonl", "archived_sessions/nested/acme.jsonl", "sessions/acme.txt"),
			"gemini": ("acme/log.json",),
			"agy": ("antigravity/brain/acme.pb", "antigravity/annotations/acme.pb",
				"antigravity/browser_recordings/acme.pb", "antigravity-cli/conversations/acme.db-wal",
				"antigravity-cli/conversations/acme.db-shm"),
		}
		for paths in (included, excluded):
			for tool, relatives in paths.items():
				for relative in relatives:
					self.write(tool, relative)
		databases = ("antigravity-cli/conversations/xyz.db", "antigravity-cli/conversation_summaries.db",
			"antigravity/conversation_summaries.db")
		for relative in databases:
			_, connection = self.database(relative)
			connection.execute("insert into messages values ('acme')")
			connection.commit()
			connection.close()
		result = self.run_archive()
		expected = {self.target(tool, relative).relative_to(self.destination)
			for tool, relatives in included.items() for relative in relatives}
		expected.update(self.target("agy", relative).relative_to(self.destination) for relative in databases)
		actual = {p.relative_to(self.destination) for p in self.destination.rglob("*") if p.is_file()}
		self.assertEqual(actual, expected)
		self.assertEqual(result.new, len(expected))
		self.assertEqual(result.failed, 0)
		for relative in databases:
			self.assertEqual(self.read_database(self.target("agy", relative)), [("acme",)])

	def test_unchanged_file_is_not_written_again(self):
		relative = "sessions/acme.jsonl"
		self.write("codex", relative)
		self.run_archive()
		before = self.target("codex", relative).stat()
		with mock.patch.object(archive, "_write") as write:
			result = self.run_archive()
		write.assert_not_called()
		self.assertEqual(result, archive.ArchiveStats(unchanged=1))
		self.assertEqual(self.target("codex", relative).stat().st_ino, before.st_ino)

	def test_source_change_updates_existing_copy(self):
		relative = "acme/chats/main.json"
		source = self.write("gemini", relative)
		self.run_archive()
		stamp = source.stat().st_mtime_ns + 1_000_000
		source.write_bytes(b"xyz\n")
		os.utime(source, ns=(stamp, stamp))
		result = self.run_archive()
		target = self.target("gemini", relative)
		self.assertEqual(gzip.decompress(target.read_bytes()), b"xyz\n")
		self.assertEqual(target.stat().st_mtime_ns, stamp)
		self.assertEqual(result, archive.ArchiveStats(updated=1, written_bytes=target.stat().st_size))

	def test_deleted_source_keeps_its_archive(self):
		relative = "acme/main.jsonl"
		source = self.write("claude", relative)
		self.run_archive()
		target = self.target("claude", relative)
		before = target.read_bytes()
		source.unlink()
		self.assertEqual(self.run_archive(), archive.ArchiveStats())
		self.assertEqual(target.read_bytes(), before)

	def test_zstd_file_is_copied_without_gzip(self):
		relative = "archived_sessions/acme.jsonl.zst"
		content = b"\x28\xb5\x2f\xfdacme\x00xyz"
		source = self.write("codex", relative, content)
		result = self.run_archive()
		target = self.target("codex", relative)
		self.assertEqual(target.read_bytes(), content)
		self.assertFalse(target.with_name(target.name + ".gz").exists())
		self.assertEqual(result.written_bytes, len(content))
		self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
		self.assertEqual(self.run_archive(), archive.ArchiveStats(unchanged=1))

	def test_wal_rows_are_backed_up_without_changing_source_files(self):
		relative = "antigravity-cli/conversations/acme.db"
		source, writer = self.database(relative, wal=True)
		self.addCleanup(writer.close)
		writer.execute("pragma wal_checkpoint(truncate)")
		writer.execute("insert into messages values ('acme')")
		writer.commit()
		before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.parent.iterdir()}
		result = self.run_archive()
		after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.parent.iterdir()}
		self.assertEqual(before, after)
		self.assertEqual(result.new, 1)
		self.assertEqual(result.failed, 0)
		target = self.target("agy", relative)
		self.assertEqual(self.read_database(target), [("acme",)])
		self.assertEqual(target.stat().st_mtime_ns,
			max(source.stat().st_mtime_ns, archive._wal(source).stat().st_mtime_ns))
		self.assertEqual([p.name for p in target.parent.iterdir()], ["acme.db.gz"])
		self.assertEqual(self.run_archive(), archive.ArchiveStats(unchanged=1))
		db_stamp = source.stat().st_mtime_ns
		writer.execute("insert into messages values ('xyz')")
		writer.commit()
		wal_stamp = target.stat().st_mtime_ns + 1_000_000
		os.utime(archive._wal(source), ns=(wal_stamp, wal_stamp))
		result = self.run_archive()
		self.assertEqual(source.stat().st_mtime_ns, db_stamp)
		self.assertEqual(result.updated, 1)
		self.assertEqual(result.failed, 0)
		self.assertEqual(self.read_database(target), [("acme",), ("xyz",)])

	def test_closed_wal_database_does_not_create_source_sidecars(self):
		relative = "antigravity/conversation_summaries.db"
		source, writer = self.database(relative, wal=True)
		writer.execute("insert into messages values ('acme')")
		writer.commit()
		writer.close()
		before = {p.name: p.read_bytes() for p in source.parent.iterdir()}
		self.assertEqual(set(before), {source.name})
		self.assertEqual(self.run_archive().new, 1)
		self.assertEqual(before, {p.name: p.read_bytes() for p in source.parent.iterdir()})
		self.assertEqual(self.read_database(self.target("agy", relative)), [("acme",)])

	def test_wal_changing_during_copy_keeps_previous_snapshot(self):
		relative = "antigravity-cli/conversations/acme.db"
		source, writer = self.database(relative, wal=True)
		self.addCleanup(writer.close)
		writer.execute("insert into messages values ('acme')")
		writer.commit()
		self.run_archive()
		target = self.target("agy", relative)
		before = target.read_bytes(), target.stat().st_mtime_ns
		writer.execute("insert into messages values ('xyz')")
		writer.commit()
		stamp = before[1] + 1_000_000
		os.utime(archive._wal(source), ns=(stamp, stamp))
		copy_file = archive._copy

		def copy_while_writing(incoming, outgoing):
			copy_file(incoming, outgoing)
			if incoming == source:
				writer.execute("insert into messages values ('acme xyz')")
				writer.commit()

		with mock.patch.object(archive, "_copy", side_effect=copy_while_writing):
			self.assertEqual(self.run_archive(), archive.ArchiveStats(failed=1))
		self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
		self.assertEqual(list(target.parent.iterdir()), [target])
		self.assertEqual(self.run_archive().updated, 1)
		self.assertEqual(self.read_database(target), [("acme",), ("xyz",), ("acme xyz",)])

	def test_read_failure_does_not_stop_other_files_or_expose_absolute_paths(self):
		bad = self.write("claude", "acme/a.jsonl")
		self.write("claude", "acme/z.jsonl")
		original_open = Path.open

		def open_file(path, *args, **kwargs):
			if path == bad:
				raise PermissionError(str(path))
			return original_open(path, *args, **kwargs)

		with mock.patch.object(Path, "open", new=open_file):
			result = self.run_archive()
		self.assertEqual(result.new, 1)
		self.assertEqual(result.failed, 1)
		self.assertTrue(self.target("claude", "acme/z.jsonl").exists())
		self.assertIn("claude/acme/a.jsonl", self.output.getvalue())
		self.assertNotIn(str(self.root), self.output.getvalue())

	def test_interrupted_write_keeps_previous_copy_and_cleans_only_temporary_file(self):
		relative = "acme/main.jsonl"
		source = self.write("claude", relative)
		self.run_archive()
		target = self.target("claude", relative)
		before = target.read_bytes(), target.stat().st_mtime_ns
		os.utime(source, ns=(before[1] + 1_000_000, before[1] + 1_000_000))

		def interrupt(incoming, outgoing):
			outgoing.write(b"acme")
			raise OSError("쓰기 중단")

		with mock.patch.object(archive.shutil, "copyfileobj", side_effect=interrupt):
			result = self.run_archive()
		self.assertEqual(result, archive.ArchiveStats(failed=1))
		self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
		self.assertEqual(list(target.parent.iterdir()), [target])

	def test_source_disappearing_during_copy_is_skipped(self):
		source = self.write("claude", "acme/main.jsonl")

		def disappear(incoming, outgoing):
			original_copy(incoming, outgoing)
			source.unlink()

		original_copy = shutil.copyfileobj
		with mock.patch.object(archive.shutil, "copyfileobj", side_effect=disappear):
			self.assertEqual(self.run_archive(), archive.ArchiveStats())
		self.assertFalse(self.target("claude", "acme/main.jsonl").exists())

	def test_locked_database_fails_without_stopping_later_files(self):
		_, writer = self.database("antigravity-cli/conversations/acme.db")
		self.addCleanup(writer.close)
		writer.execute("begin exclusive")
		self.write("agy", "antigravity/conversations/xyz.pb")
		result = self.run_archive()
		self.assertEqual(result.failed, 1)
		self.assertEqual(result.new, 1)

	def test_moved_archive_with_changed_mtime_is_safely_rewritten(self):
		relative = "acme/main.jsonl"
		source = self.write("claude", relative)
		self.run_archive()
		target = self.target("claude", relative)
		os.utime(target, ns=(1, 1))
		self.assertEqual(self.run_archive().updated, 1)
		self.assertEqual(gzip.decompress(target.read_bytes()), source.read_bytes())

	def test_cli_archive_needs_no_collection_config(self):
		self.write("claude", "acme/main.jsonl")
		with mock.patch.object(cli.pipeline, "load_all") as load, contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["archive"]), 0)
		load.assert_not_called()
		self.assertTrue(self.target("claude", "acme/main.jsonl").exists())

	def test_cli_run_archives_before_collecting_and_skips_archive_in_dry_run(self):
		for dry_run in (False, True):
			with self.subTest(dry_run=dry_run), mock.patch.object(cli, "pipeline") as pipeline, \
					mock.patch.object(archive, "archive", return_value=archive.ArchiveStats()) as save, \
					contextlib.redirect_stdout(io.StringIO()):
				pipeline.load_all.return_value = (mock.sentinel.sources, mock.sentinel.plans, [])
				pipeline.window.return_value = date(2026, 10, 22)
				pipeline.Pipeline.return_value.counts = {}
				pipeline.Pipeline.return_value.excluded_titles = []
				events = []
				save.side_effect = lambda: events.append("archive") or archive.ArchiveStats()
				pipeline.Pipeline.return_value.run.side_effect = lambda since: events.append("collect")
				args = ["run", "--since", "2026-10-22"] + (["--dry-run"] if dry_run else [])
				self.assertEqual(cli.main(args), 0)
				self.assertEqual(events, ["collect"] if dry_run else ["archive", "collect"])
				pipeline.Pipeline.assert_called_once_with(mock.sentinel.sources, mock.sentinel.plans,
					pipeline.open_ledger.return_value, dry_run)
				pipeline.Pipeline.return_value.run.assert_called_once_with(date(2026, 10, 22))
				pipeline.open_ledger.return_value.close.assert_called_once()
				if dry_run:
					save.assert_not_called()
		self.assertFalse(self.destination.exists())

	def test_cli_run_still_collects_after_unexpected_archive_error(self):
		output = io.StringIO()
		with mock.patch.object(cli, "pipeline") as pipeline, \
				mock.patch.object(archive, "archive", side_effect=OSError(str(self.root))), \
				contextlib.redirect_stdout(output):
			pipeline.load_all.return_value = (mock.sentinel.sources, mock.sentinel.plans, [])
			pipeline.window.return_value = date(2026, 10, 22)
			pipeline.Pipeline.return_value.counts = {}
			self.assertEqual(cli.main(["run"]), 0)
			pipeline.Pipeline.return_value.run.assert_called_once_with(date(2026, 10, 22))
			self.assertEqual(cli.main(["archive"]), 1)
		self.assertIn("실패 1건", output.getvalue())
		self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
	unittest.main()
