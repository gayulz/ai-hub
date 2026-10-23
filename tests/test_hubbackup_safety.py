"""오류 격리·목록 변조·해시·복원 경계를 합성 자료로 확인한다."""

import copy
import fcntl
import json
import os
import subprocess
from unittest import mock

from hubbackup import backup, crypto, restore, state
from test_hubbackup import HubBackupCase


class HubBackupSafetyTest(HubBackupCase):
	def test_unreadable_source_does_not_stop_other_files_and_reports_relative_only(self):
		bad = self.write("ai-hub", "archive/acme.txt")
		self.write("codex", "config.toml", b"xyz")
		original_open = os.open

		def unreadable(path, *args, **kwargs):
			if path == bad:
				raise PermissionError(str(bad))
			return original_open(path, *args, **kwargs)

		with mock.patch.object(os, "open", side_effect=unreadable):
			result = self.run_backup()
		self.assertEqual((result.new, result.failed), (1, 1))
		self.assertIn("백업 실패: ai-hub/archive/acme.txt", self.output.getvalue())
		self.assertNotIn(str(self.root), self.output.getvalue())
		self.assertEqual(set(self.index()), {"codex/config.toml"})
		self.assertIn("마지막 실패 1건", backup.status())
		self.assertEqual(self.run_restore().restored, 1)

	def test_disappearing_source_is_skipped_without_failure(self):
		vanishing = self.write("ai-hub", "archive/acme.txt")
		self.write("codex", "config.toml")
		original_open = os.open

		def disappear(path, *args, **kwargs):
			if path == vanishing:
				vanishing.unlink()
			return original_open(path, *args, **kwargs)

		with mock.patch.object(os, "open", side_effect=disappear):
			result = self.run_backup()
		self.assertEqual((result.new, result.failed), (1, 0))
		self.assertEqual(set(self.index()), {"codex/config.toml"})

	def test_source_change_while_reading_keeps_previous_copy_and_retries(self):
		source = self.write("ai-hub", "archive/acme.txt", b"acme")
		self.run_backup()
		name = state.object_name("ai-hub/archive/acme.txt")
		before = (self.destination / name).read_bytes()
		source.write_bytes(b"xyz before read")
		original_copy = crypto.copy_hash

		def change_while_reading(incoming, outgoing, *args):
			result = original_copy(incoming, outgoing, *args)
			source.write_bytes(b"xyz after read")
			return result

		with mock.patch.object(crypto, "copy_hash", side_effect=change_while_reading):
			result = self.run_backup()
		self.assertEqual((result.updated, result.failed), (0, 1))
		self.assertEqual((self.destination / name).read_bytes(), before)
		self.assertEqual(self.run_backup().updated, 1)
		self.assertEqual(self.run_restore().restored, 1)
		self.assertEqual((self.restored / "ai-hub/archive/acme.txt").read_bytes(), b"xyz after read")

	def test_missing_key_cli_returns_one_with_korean_guidance(self):
		self.key.unlink()
		self.write("ai-hub", "sources.toml")
		result = self.command("run")
		self.assertEqual(result.returncode, 1)
		self.assertIn("백업 키가 없습니다", result.stderr)
		self.assertIn("age-keygen", result.stderr)
		self.assertIn("실패 1건", result.stdout)
		self.assertNotIn(str(self.root), result.stdout + result.stderr)
		self.assertFalse(self.destination.exists())
		self.assertIn("마지막 실패 1건", backup.status())

	def test_missing_age_is_counted_as_failure_instead_of_vanished_source(self):
		self.key.with_suffix(".pub").write_text(crypto.recipient(self.key), encoding="ascii")
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		self.write("ai-hub", "sources.toml", b"xyz updated")
		with mock.patch.object(crypto.subprocess, "run", side_effect=FileNotFoundError("age")):
			result = self.run_backup()
		self.assertEqual((result.updated, result.failed), (0, 1))
		self.assertIn("마지막 실패 1건", backup.status())

	def test_missing_destination_file_is_not_mistaken_for_missing_source(self):
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		self.write("ai-hub", "sources.toml", b"xyz updated")
		with mock.patch.object(crypto, "encrypt", side_effect=FileNotFoundError(str(self.destination))):
			result = self.run_backup()
		self.assertEqual((result.updated, result.failed), (0, 1))
		self.assertNotIn(str(self.destination), self.output.getvalue())

	def test_unreadable_directory_is_counted_and_other_roots_continue(self):
		bad = self.write("claude", "rules/acme.md").parent
		self.write("codex", "config.toml")
		original_scandir = os.scandir

		def unreadable(path):
			if path == bad:
				raise PermissionError(str(path))
			return original_scandir(path)

		with mock.patch.object(os, "scandir", side_effect=unreadable):
			result = self.run_backup()
		self.assertEqual((result.new, result.failed), (1, 1))
		self.assertIn("백업 실패: claude/rules", self.output.getvalue())
		self.assertNotIn(str(self.root), self.output.getvalue())

	def test_index_failure_does_not_commit_local_fingerprints_and_next_run_retries(self):
		source = self.write("ai-hub", "sources.toml", b"acme")
		self.run_backup()
		before = json.loads(self.record.read_text())
		source.write_bytes(b"xyz updated")
		with mock.patch.object(backup, "write_index", side_effect=OSError(str(self.destination))):
			result = self.run_backup()
		after = json.loads(self.record.read_text())
		self.assertEqual((result.updated, result.failed), (1, 1))
		self.assertEqual(before["files"], after["files"])
		self.assertEqual(before["sources"], after["sources"])
		self.assertEqual(after["last_failed"], 1)
		self.assertNotIn(str(self.destination), self.output.getvalue())
		self.assertEqual(self.run_backup().updated, 1)
		self.assertEqual(self.run_restore().restored, 1)
		self.assertEqual((self.restored / "ai-hub/sources.toml").read_bytes(), b"xyz updated")

	def test_crypto_failure_preserves_completed_copy_and_continues(self):
		source = self.write("ai-hub", "sources.toml", b"acme")
		self.run_backup()
		name = state.object_name("ai-hub/sources.toml")
		before = (self.destination / name).read_bytes()
		source.write_bytes(b"xyz modified")
		self.write("codex", "config.toml")
		original_encrypt = crypto.encrypt

		def fail_one(incoming, target, public):
			if target.name == name:
				raise crypto.BackupError("합성 실패")
			return original_encrypt(incoming, target, public)

		with mock.patch.object(crypto, "encrypt", side_effect=fail_one):
			result = self.run_backup()
		self.assertEqual((result.new, result.updated, result.failed), (1, 0, 1))
		self.assertEqual((self.destination / name).read_bytes(), before)
		self.assertEqual(self.run_restore().restored, 2)
		self.assertEqual((self.restored / "ai-hub/sources.toml").read_bytes(), b"acme")

	def test_restore_rejects_traversal_absolute_paths_and_forged_blob_names(self):
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		entries = self.index()
		original = entries["ai-hub/sources.toml"]
		outside = self.root / "outside.txt"
		outside.write_bytes(b"xyz preserved")
		for relative in ("../outside.txt", "ai-hub/../../outside.txt", str(outside), "ai-hub/../outside.txt",
				"ai-hub/./outside.txt", "ai-hub\\..\\outside.txt", "ai-hub//outside.txt"):
			entries[relative] = original.copy()
		entries["codex/config.toml"] = {**original, "name": "../../outside.txt"}
		self.replace_index(entries)
		result = self.run_restore()
		self.assertEqual((result.restored, result.failed), (1, 8))
		self.assertEqual(outside.read_bytes(), b"xyz preserved")
		self.assertNotIn(str(outside), self.restore_output.getvalue())
		self.assertFalse((self.restored / "outside.txt").exists())

	def test_restore_parent_symlink_cannot_escape_even_with_overwrite(self):
		self.write("ai-hub", "archive/acme.txt", b"acme")
		self.run_backup()
		outside = self.root / "outside"
		outside.mkdir()
		(outside / "acme.txt").write_bytes(b"xyz preserved")
		(self.restored / "ai-hub").mkdir(parents=True)
		(self.restored / "ai-hub/archive").symlink_to(outside, target_is_directory=True)
		self.assertEqual(self.run_restore(overwrite=True), restore.RestoreStats(failed=1))
		self.assertEqual((outside / "acme.txt").read_bytes(), b"xyz preserved")

	def test_restore_leaf_symlink_is_skipped_or_replaced_without_following(self):
		self.write("codex", "config.toml", b"acme")
		self.run_backup()
		outside = self.root / "outside.txt"
		outside.write_bytes(b"xyz preserved")
		(self.restored / "codex").mkdir(parents=True)
		leaf = self.restored / "codex/config.toml"
		leaf.symlink_to(outside)
		self.assertEqual(self.run_restore(), restore.RestoreStats(skipped=1))
		self.assertEqual(self.run_restore(overwrite=True), restore.RestoreStats(restored=1))
		self.assertEqual(outside.read_bytes(), b"xyz preserved")
		self.assertFalse(leaf.is_symlink())
		self.assertEqual(leaf.read_bytes(), b"acme")

	def test_restore_checks_hash_before_replacing_existing_file_and_continues(self):
		self.write("ai-hub", "sources.toml", b"acme")
		self.write("codex", "config.toml", b"xyz")
		self.run_backup()
		entries = self.index()
		entries["ai-hub/sources.toml"]["sha256"] = "0" * 64
		self.replace_index(entries)
		(self.restored / "ai-hub").mkdir(parents=True)
		retained = self.restored / "ai-hub/sources.toml"
		retained.write_bytes(b"xyz existing")
		self.assertEqual(self.run_restore(overwrite=True), restore.RestoreStats(restored=1, failed=1))
		self.assertEqual(retained.read_bytes(), b"xyz existing")
		self.assertEqual((self.restored / "codex/config.toml").read_bytes(), b"xyz")
		self.assertEqual(sorted(p.name for p in retained.parent.iterdir()), ["sources.toml"])

	def test_tampered_ciphertext_is_not_published(self):
		self.write("ai-hub", "sources.toml", b"acme")
		self.run_backup()
		path = self.destination / state.object_name("ai-hub/sources.toml")
		payload = bytearray(path.read_bytes())
		payload[-1] ^= 1
		path.write_bytes(payload)
		self.assertEqual(self.run_restore(), restore.RestoreStats(failed=1))
		self.assertFalse((self.restored / "ai-hub/sources.toml").exists())

	def test_invalid_metadata_and_wrong_key_do_not_expose_key_or_absolute_path(self):
		self.write("ai-hub", "sources.toml", b"acme")
		self.run_backup()
		original = self.index()
		for key, value in [("size", -1), ("mtime_ns", "acme"), ("compressed", "yes"), ("sha256", "xyz")]:
			entries = copy.deepcopy(original)
			entries["ai-hub/sources.toml"][key] = value
			self.replace_index(entries)
			self.assertEqual(self.run_restore().failed, 1)
		self.replace_index(original)
		wrong_key = self.root / "xyz.agekey"
		self.assertEqual(subprocess.run(["age-keygen", "-o", str(wrong_key)], capture_output=True).returncode, 0)
		result = self.command("restore", "--key", str(wrong_key), "--to", str(self.restored))
		self.assertEqual(result.returncode, 1)
		self.assertNotIn(str(self.root), result.stderr + result.stdout)
		self.assertNotIn("AGE-SECRET-KEY-", result.stderr + result.stdout)
		self.assertNotIn("Traceback", result.stderr + result.stdout)

	def test_payloads_are_copied_in_bounded_chunks(self):
		content = os.urandom(3 * crypto.CHUNK + 17)
		self.write("ai-hub", "archive/acme.bin", content)
		original_copy = crypto.copy_hash
		reads = []

		class BoundedReader:
			def __init__(inner_self, incoming):
				inner_self.incoming = incoming

			def read(inner_self, size=-1):
				self.assertGreater(size, 0)
				self.assertLessEqual(size, crypto.CHUNK)
				reads.append(size)
				return inner_self.incoming.read(size)

		def bounded_copy(incoming, outgoing, limit=None):
			return original_copy(BoundedReader(incoming), outgoing, limit)

		with mock.patch.object(crypto, "copy_hash", side_effect=bounded_copy):
			self.assertEqual(self.run_backup().new, 1)
			self.assertEqual(self.run_restore().restored, 1)
		self.assertGreater(len(reads), 8)
		self.assertEqual((self.restored / "ai-hub/archive/acme.bin").read_bytes(), content)

	def test_concurrent_backup_is_rejected_before_writing_cloud(self):
		self.write("ai-hub", "sources.toml")
		with self.record.with_suffix(".lock").open("wb") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			with self.assertRaises(crypto.BackupError):
				self.run_backup()
		self.assertFalse(self.destination.exists())

	def test_restore_refuses_plaintext_inside_backup_folder(self):
		self.write("ai-hub", "sources.toml")
		self.run_backup()
		with self.assertRaises(crypto.BackupError):
			restore.restore(self.key, self.destination / "restored")
		self.assertFalse((self.destination / "restored").exists())

	def test_no_sources_still_creates_a_restorable_empty_index(self):
		result = self.run_backup()
		self.assertEqual((result.new, result.failed), (0, 0))
		self.assertGreater(result.written_bytes, 0)
		self.assertEqual(self.index(), {})
		self.assertEqual(self.run_restore(), restore.RestoreStats())
		self.assertEqual(self.run_backup(), backup.BackupStats())
