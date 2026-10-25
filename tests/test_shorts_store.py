"""경로·권한·원자적 기록·회차 번호와 엄격한 JSON 경계를 검사한다."""

import copy
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from shorts import Conflict, StoreError, approvals, artifacts, files, schema
from test_shorts_support import SID, ShortsCase


class StoreTest(ShortsCase):
	def test_directories_and_files_have_private_permissions(self):
		self.add("script.txt", "script")
		for folder, folders, names in os.walk(self.hub / "shorts"):
			self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
			for name in names:
				self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode), 0o600)
		self.assertTrue((self.round_path() / "logs").is_dir())

	def test_next_round_is_concurrent_monotonic_and_old_round_is_read_only(self):
		self.add("first.txt", "script")
		old = self.read()
		before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.round_path().rglob("*") if p.is_file()}
		gate = threading.Barrier(2)
		def create():
			gate.wait(timeout=5)
			return self.store.new(SID)["round"]
		with ThreadPoolExecutor(max_workers=2) as pool:
			self.assertCountEqual(list(pool.map(lambda _: create(), range(2))), (2, 3))
		self.assertEqual(self.read(), old)
		for function, args in ((artifacts.register, ("second.txt", "script", b"second")),
				(approvals.start_stage, ())):
			with self.assertRaises(StoreError):
				self.call(function, *args)
		with self.store.locked(SID, 1, writable=False) as record:
			with self.assertRaises(StoreError):
				record.save()
		self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

	def test_interrupted_round_number_is_reserved_and_never_reused(self):
		with mock.patch("shorts.store.files.write_atomic", side_effect=OSError("합성 저장 중단")):
			with self.assertRaises(StoreError):
				self.store.new(SID)
		self.assertTrue(self.round_path(2).is_dir())
		self.assertEqual(self.store.new(SID)["round"], 3)

	def test_atomic_save_fsyncs_content_before_rename_and_preserves_old_on_failure(self):
		path = self.round_path() / "run.json"
		before, events = path.read_bytes(), []
		real_sync, real_rename = os.fsync, os.rename
		def sync(fd):
			events.append("folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
			return real_sync(fd)
		def rename(src, dst, **kwargs):
			self.assertEqual(path.read_bytes(), before)
			self.assertIn("file", events)
			events.append("rename")
			return real_rename(src, dst, **kwargs)
		with mock.patch.object(files.os, "fsync", side_effect=sync), mock.patch.object(files.os, "rename", side_effect=rename):
			self.call(approvals.start_stage)
		self.assertEqual(events[-2:], ["rename", "folder"])
		before = path.read_bytes()
		with mock.patch.object(files.os, "rename", side_effect=OSError("합성 저장 중단")):
			with self.assertRaises(StoreError):
				self.call(approvals.complete_stage)
		self.assertEqual(path.read_bytes(), before)
		self.assertFalse(any(p.name.startswith(".tmp-") for p in self.round_path().iterdir()))

	def test_intake_owner_and_group_or_world_write_are_rejected_without_chmod(self):
		folder = self.hub / "shorts" / SID
		for mode in (0o720, 0o702):
			folder.chmod(mode)
			try:
				with self.assertRaises(StoreError):
					self.read()
				self.assertEqual(stat.S_IMODE(folder.stat().st_mode), mode)
			finally:
				folder.chmod(0o700)
		inode = folder.stat().st_ino
		real_fstat = os.fstat
		def foreign(fd):
			info = real_fstat(fd)
			if info.st_ino == inode:
				values = list(info)
				values[4] = os.getuid() + 1
				return os.stat_result(values)
			return info
		with mock.patch.object(files.os, "fstat", side_effect=foreign), self.assertRaises(StoreError):
			self.read()

	def test_bad_intake_identity_status_and_version_are_read_only(self):
		path = self.hub / "shorts" / SID / "intake.json"
		original = path.read_bytes()
		row = json.loads(original)
		try:
			for change in ({"id": "s-" + "b" * 32}, {"status": "waiting"}, {"version": True},
					{"confirmed_by": "automatic"}, {"created": "2026-10-25"}):
				path.write_text(json.dumps({**row, **change}), encoding="utf-8")
				before = path.read_bytes()
				with self.assertRaises(StoreError):
					self.store.new(SID)
				self.assertEqual(path.read_bytes(), before)
		finally:
			path.write_bytes(original)

	def test_ancestor_symlink_and_parent_traversal_in_home_are_rejected(self):
		alias = self.root / "alias"
		alias.symlink_to(self.hub, target_is_directory=True)
		for value in (alias, self.hub / ".." / "hub"):
			with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(value)}), self.assertRaises(StoreError):
				self.read()

	def test_symlink_at_every_managed_directory_is_rejected(self):
		for path, writing in ((self.hub / "shorts", False), (self.hub / "shorts" / SID, False),
				(self.round_path().parent, False), (self.round_path(), False),
				(self.round_path() / "artifacts", True)):
			with self.subTest(path=path.name):
				backup = path.with_name(path.name + "-real")
				path.rename(backup)
				path.symlink_to(backup, target_is_directory=True)
				try:
					with self.assertRaises(StoreError):
						self.add("blocked.txt", "report") if writing else self.read()
				finally:
					path.unlink()
					backup.rename(path)

	def test_record_intake_and_lock_symlinks_or_hardlinks_are_rejected(self):
		for path in (self.round_path() / "run.json", self.hub / "shorts" / SID / "intake.json",
				self.round_path() / ".lock", self.round_path().parent.parent / ".rounds.lock"):
			for linked in (True, False):
				with self.subTest(file=path.name, symlink=linked):
					backup = path.with_name(path.name + ".original")
					path.rename(backup)
					if linked:
						path.symlink_to(backup)
					else:
						os.link(backup, path)
					try:
						with self.assertRaises(StoreError):
							self.read()
					finally:
						path.unlink()
						backup.rename(path)

	def test_strict_schema_rejects_unknown_keys_wrong_types_and_large_strings_on_read_and_write(self):
		bad = [{**self.run, "extra": 1}, {**self.run, "version": True}, {**self.run, "round": False},
			{**self.run, "revision": 0}, {**self.run, "revision": True}, {**self.run, "updated": None},
			{**self.run, "status": []}, {**self.run, "stage": "other"},
			{**self.run, "created": "2026-10-25"}, {**self.run, "blocked_reason": "x" * 10000},
			{**self.run, "approvals": {}}, {**self.run, "review_items": {"a": 2}},
			{**self.run, "artifacts": [{"name": "a", "kind": "script", "sha256": "a" * 64,
				"size": True, "registered_at": schema.now()}]},
			{**self.run, "attempts": [{"extra": "원문"}]}]
		bad.extend({key: value for key, value in self.run.items() if key != removed} for removed in self.run)
		path = self.round_path() / "run.json"
		original = path.read_bytes()
		for run in bad:
			with self.subTest(keys=list(run)):
				path.write_text(json.dumps(run), encoding="utf-8")
				try:
					with self.assertRaises(StoreError):
						self.read()
				finally:
					path.write_bytes(original)
				with self.store.locked(SID, self.number) as record:
					record.run = copy.deepcopy(run)
					with self.assertRaises(StoreError):
						record.save()
				self.assertEqual(path.read_bytes(), original)

	def test_duplicate_json_keys_constants_and_oversized_files_are_rejected(self):
		path = self.round_path() / "run.json"
		original = path.read_bytes()
		for raw in (b'{"version":1,"version":1}', b'{"version":NaN}',
				b"[" * 2000, b" " * (schema.MAX_RECORD_BYTES + 1)):
			path.write_bytes(raw)
			try:
				with self.assertRaises(StoreError):
					self.read()
			finally:
				path.write_bytes(original)
