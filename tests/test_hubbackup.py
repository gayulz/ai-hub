"""임시 홈·임시 age 키와 합성 자료로만 백업을 확인한다."""

import contextlib
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hubbackup import backup, crypto, restore, state, targets


class HubBackupCase(unittest.TestCase):
	def setUp(self):
		if not shutil.which("age") or not shutil.which("age-keygen"):
			self.skipTest("age·age-keygen 명령이 필요합니다")
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name)
		self.user = self.root / "user"
		self.hub = self.root / "hub"
		patch = mock.patch.dict(os.environ, {"HOME": str(self.user), "AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.roots = targets.roots()
		self.destination = targets.backup_dir()
		self.record = self.hub / "backup-state.json"
		self.key = self.hub / "keys/backup.agekey"
		self.key.parent.mkdir(parents=True)
		result = subprocess.run(["age-keygen", "-o", str(self.key)], capture_output=True)
		self.assertEqual(result.returncode, 0)
		self.restored = self.root / "restored"

	def write(self, tool, relative, content=b"acme synthetic content\n"):
		path = self.roots[tool] / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)
		return path

	def run_backup(self):
		self.output = io.StringIO()
		with contextlib.redirect_stdout(self.output):
			return backup.run()

	def run_restore(self, overwrite=False, key=None):
		self.restore_output = io.StringIO()
		with contextlib.redirect_stdout(self.restore_output):
			return restore.restore(self.key if key is None else key, self.restored, overwrite)

	def index(self):
		with tempfile.TemporaryFile() as output:
			crypto.decrypt(self.destination / "index.json.age", output, self.key)
			return json.load(output)["files"]

	def replace_index(self, entries):
		backup.write_index(entries, self.destination, crypto.recipient(self.key))

	def database(self, wal=True):
		source = self.hub / "ledger.db"
		writer = sqlite3.connect(source)
		if wal:
			writer.execute("pragma journal_mode=wal")
			writer.execute("pragma wal_autocheckpoint=0")
		writer.execute("create table messages (body text)")
		writer.commit()
		if wal:
			writer.execute("pragma wal_checkpoint(truncate)")
		writer.execute("insert into messages values ('acme')")
		writer.commit()
		return source, writer

	def read_database(self):
		reader = sqlite3.connect((self.restored / "ai-hub/ledger.db").as_uri() + "?mode=ro", uri=True)
		try:
			return reader.execute("select body from messages order by rowid").fetchall()
		finally:
			reader.close()

	def command(self, *args):
		return subprocess.run([sys.executable, "-m", "hubbackup", *args], capture_output=True, text=True)


class HubBackupTest(HubBackupCase):
	def test_round_trip_all_targets_and_uncheckpointed_wal(self):
		samples = {
			"ai-hub": ["sources.toml", "plans.toml", "redact_hosts.toml", "redaction/acme.json",
				"runs/acme/run.json", "handoff.md", "tools/acme.sh", "archive/acme.bin", "office-tasks/task-acme.json"],
			"claude": ["CLAUDE.md", "rules/acme.md", "hooks/acme.sh", "agents/acme.md",
				"skills/acme/SKILL.md", "settings.json", "settings.local.json", "statusline-acme.sh",
				"projects/acme/memory/MEMORY.md", "projects/acme/memory/nested/acme.md"],
			"codex": ["AGENTS.md", "config.toml", "hooks.json", "hooks/acme.sh"],
			"gemini": ["GEMINI.md", "settings.json", "rules/acme.md", "skills/acme/SKILL.md",
				"hooks/acme.sh", "projects.json", "trustedFolders.json"],
		}
		expected = {}
		for tool, paths in samples.items():
			for relative in paths:
				content = (tool + "/" + relative + " 합성 자료\n").encode() + bytes(range(256))
				self.write(tool, relative, content)
				expected[tool + "/" + relative] = content
		for relative, content in [("archive/acme.gz", gzip.compress(b"acme xyz\0" * 100)),
				("archive/xyz.zst", b"\x28\xb5\x2f\xfdacme synthetic archive")]:
			self.write("ai-hub", relative, content)
			expected["ai-hub/" + relative] = content
		self.write("claude", "projects/acme/session.jsonl")
		self.write("claude", "projects/acme/not-memory/other.md")
		self.write("codex", "sessions/acme.jsonl")
		source, writer = self.database()
		self.addCleanup(writer.close)
		before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.parent.glob("ledger.db*")}
		result = self.run_backup()
		self.assertEqual((result.new, result.failed), (len(expected) + 1, 0))
		self.assertEqual(self.output.getvalue().splitlines(), [result.summary()])
		self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.parent.glob("ledger.db*")})
		index = self.index()
		self.assertEqual(set(index), {*expected, "ai-hub/ledger.db"})
		for relative in expected:
			self.assertEqual(index[relative]["compressed"], not relative.endswith((".gz", ".zst")))
		self.assertEqual(self.run_restore(), restore.RestoreStats(restored=len(expected) + 1))
		for relative, content in expected.items():
			self.assertEqual((self.restored / relative).read_bytes(), content)
		self.assertEqual(self.read_database(), [("acme",)])

	def test_ciphertext_names_headers_privacy_and_permissions(self):
		content = b"acme-cleartext-unique-synthetic-marker\0" * 30
		self.write("ai-hub", "archive/acme.txt", content)
		self.write("claude", "projects/acme/memory/acme.md", content)
		result = self.run_backup()
		objects = list(self.destination.iterdir())
		self.assertEqual(len(objects), 3)
		for path in objects:
			self.assertTrue(path.name == "index.json.age" or re.fullmatch(r"[0-9a-f]{64}\.age", path.name))
			payload = path.read_bytes()
			self.assertTrue(payload.startswith(b"age-encryption.org/v1\n"))
			for plain in (content, str(self.root).encode(), b"ai-hub/archive/acme.txt", b"projects/acme/memory"):
				self.assertNotIn(plain, payload)
			self.assertEqual(path.stat().st_mode & 0o777, 0o600)
		self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
		self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)
		self.assertEqual(result.written_bytes, sum(path.stat().st_size for path in objects))
		self.assertNotIn(str(self.root), self.record.read_text())
		self.assertNotIn(str(self.root), json.dumps(self.index()))

	def test_incremental_uses_only_local_state_and_updates_one_object(self):
		changed = self.write("ai-hub", "sources.toml", b"acme")
		self.write("codex", "config.toml", b"xyz")
		self.assertEqual(self.run_backup().new, 2)
		before = {path.name: path.read_bytes() for path in self.destination.iterdir()}
		original_open = Path.open
		original_os_open = os.open

		def guarded_open(path, *args, **kwargs):
			if path.is_relative_to(self.destination):
				raise AssertionError("사본을 읽으면 안 됩니다")
			return original_open(path, *args, **kwargs)

		def guarded_os_open(path, flags, *args, **kwargs):
			if not isinstance(path, int) and Path(path).is_relative_to(self.destination):
				self.assertTrue(flags & os.O_WRONLY)
			return original_os_open(path, flags, *args, **kwargs)

		with mock.patch.object(Path, "open", new=guarded_open), mock.patch.object(os, "open", side_effect=guarded_os_open):
			self.assertEqual(self.run_backup(), backup.BackupStats(unchanged=2))
		self.assertEqual(before, {path.name: path.read_bytes() for path in self.destination.iterdir()})
		changed.write_bytes(b"acme changed")
		with mock.patch.object(Path, "open", new=guarded_open), mock.patch.object(os, "open", side_effect=guarded_os_open):
			result = self.run_backup()
		self.assertEqual((result.new, result.updated, result.unchanged, result.failed), (0, 1, 1, 0))
		after = {path.name: path.read_bytes() for path in self.destination.iterdir()}
		changed_names = {name for name in before if before[name] != after[name]}
		self.assertEqual(changed_names, {state.object_name("ai-hub/sources.toml"), "index.json.age"})
		self.assertEqual(result.written_bytes, sum((self.destination / name).stat().st_size for name in changed_names))

	def test_deleted_sources_and_index_entries_survive(self):
		removed = self.write("ai-hub", "archive/acme.txt", b"acme retained")
		self.write("codex", "config.toml")
		self.run_backup()
		before = {path.name: path.read_bytes() for path in self.destination.iterdir()}
		removed.unlink()
		self.assertEqual(self.run_backup(), backup.BackupStats(unchanged=1))
		self.assertEqual(before, {path.name: path.read_bytes() for path in self.destination.iterdir()})
		self.assertIn("ai-hub/archive/acme.txt", self.index())
		self.write("codex", "config.toml", b"xyz changed")
		self.assertEqual(self.run_backup().updated, 1)
		self.assertIn("ai-hub/archive/acme.txt", self.index())
		self.assertEqual(self.run_restore().restored, 2)
		self.assertEqual((self.restored / "ai-hub/archive/acme.txt").read_bytes(), b"acme retained")

	def test_missing_local_state_inherits_index_and_reuploads(self):
		removed = self.write("ai-hub", "archive/acme.txt", b"acme retained")
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		removed.unlink()
		self.record.unlink()
		result = self.run_backup()
		self.assertEqual((result.new, result.updated, result.failed), (0, 1, 0))
		self.assertIn("ai-hub/archive/acme.txt", self.index())

	def test_missing_local_state_refuses_index_from_other_key(self):
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		before = (self.destination / "index.json.age").read_bytes()
		self.record.unlink()
		self.key.unlink()
		subprocess.run(["age-keygen", "-o", str(self.key)], capture_output=True)
		for _ in range(2):
			with self.assertRaises(crypto.BackupError):
				self.run_backup()
		self.assertEqual(before, (self.destination / "index.json.age").read_bytes())

	def test_excluded_credentials_keys_caches_links_and_unselected_files(self):
		for tool, folder in [("ai-hub", "archive"), ("claude", "skills"), ("codex", "hooks"), ("gemini", "rules")]:
			for name in ("auth.json", "oauth_creds.json", "google_accounts.json", "acme-oauth-token.json",
					"keys/acme.txt", "nested/keys/acme.txt", "__pycache__/acme.pyc", ".DS_Store", "🔐acme.md"):
				self.write(tool, folder + "/" + name)
		allowed = self.write("ai-hub", "archive/allowed.txt")
		self.write("ai-hub", "keys/other.agekey")
		self.write("ai-hub", "unselected.txt")
		self.write("ai-hub", "index/segments_1")  # 검색 색인은 다시 만들 수 있고 회사 노트 조각이 들 수 있어 백업하지 않는다
		self.write("ai-hub", "search/eval-candidates.jsonl")
		self.write("codex", "memories/acme.md")
		linked = self.root / "outside"
		linked.mkdir()
		(linked / "acme.txt").write_bytes(b"xyz")
		(allowed.parent / "file-link").symlink_to(linked / "acme.txt")
		(allowed.parent / "directory-link").symlink_to(linked, target_is_directory=True)
		project = self.roots["claude"] / "projects"
		project.mkdir()
		(project / "acme").symlink_to(linked, target_is_directory=True)
		self.assertEqual(self.run_backup().new, 1)
		self.assertEqual(set(self.index()), {"ai-hub/archive/allowed.txt"})

	def test_wal_only_changes_update_and_closed_wal_has_no_new_source_sidecars(self):
		source, writer = self.database()
		try:
			self.run_backup()
			self.assertEqual(self.run_backup(), backup.BackupStats(unchanged=1))
			main_stamp = source.stat().st_mtime_ns
			writer.execute("insert into messages values ('xyz')")
			writer.commit()
			self.assertEqual(source.stat().st_mtime_ns, main_stamp)
			self.assertEqual(self.run_backup().updated, 1)
			self.assertEqual(self.run_restore().restored, 1)
			self.assertEqual(self.read_database(), [("acme",), ("xyz",)])
		finally:
			writer.close()
		before = {path.name: path.read_bytes() for path in self.hub.glob("ledger.db*")}
		self.assertEqual(set(before), {"ledger.db"})
		self.run_backup()
		self.assertEqual(before, {path.name: path.read_bytes() for path in self.hub.glob("ledger.db*")})

	def test_restore_skip_and_overwrite(self):
		self.write("ai-hub", "sources.toml", b"acme original")
		self.run_backup()
		self.assertEqual(self.run_restore(), restore.RestoreStats(restored=1))
		path = self.restored / "ai-hub/sources.toml"
		path.write_bytes(b"xyz existing")
		self.assertEqual(self.run_restore(), restore.RestoreStats(skipped=1))
		self.assertEqual(path.read_bytes(), b"xyz existing")
		self.assertEqual(self.run_restore(overwrite=True), restore.RestoreStats(restored=1))
		self.assertEqual(path.read_bytes(), b"acme original")

	def test_status_and_cli_round_trip_use_temporary_homes(self):
		self.assertIn("마지막 백업: 없음", backup.status())
		self.write("ai-hub", "sources.toml", b"acme cli")
		result = self.command("run")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("새로 1건", result.stdout)
		status = self.command("status")
		self.assertEqual(status.returncode, 0)
		self.assertIn("파일 1개", status.stdout)
		self.assertIn("원본 크기 합 8바이트", status.stdout)
		self.assertIn("마지막 실패 0건", status.stdout)
		self.assertNotIn("마지막 백업: 없음", status.stdout)
		result = self.command("restore", "--key", str(self.key), "--to", str(self.restored))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("복원 1건", result.stdout)
		self.assertEqual((self.restored / "ai-hub/sources.toml").read_bytes(), b"acme cli")

	def test_public_key_alone_can_encrypt(self):
		public = crypto.recipient(self.key)
		self.key.with_suffix(".pub").write_text(public, encoding="ascii")
		self.key.unlink()
		self.write("ai-hub", "sources.toml")
		self.assertEqual(self.run_backup().new, 1)
		self.key.with_suffix(".pub").rename(self.key.with_name(self.key.name + ".pub"))
		self.write("ai-hub", "sources.toml", b"xyz modified")
		self.assertEqual(self.run_backup().updated, 1)


if __name__ == "__main__":
	unittest.main()
