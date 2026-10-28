"""합성 입력으로 개인 저장·버전·마감과 명령 경계를 검사한다."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.config import ConfigError
from jobs import applications, materials, storage


class JobsStorageTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.hub = Path(self.tmp.name).resolve()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)

	def test_file_versions_are_private_and_each_needs_new_permission(self):
		original = self.hub / "input.txt"
		original.write_text("합성 자료 첫 버전", encoding="utf-8")
		with storage.transaction() as root:
			first, created = materials.add(root, *materials.source_file(original))
			self.assertTrue(created)
			self.assertFalse(first["ai_allowed"])
			self.assertEqual(first["source"], "input.txt")
			self.assertEqual(first["sha256"], hashlib.sha256("합성 자료 첫 버전".encode()).hexdigest())
			materials.permission(root, first["id"], True)
			self.assertEqual([row["id"] for row in materials.allowed(root)], [first["id"]])
			same, created = materials.add(root, *materials.source_file(original))
			self.assertFalse(created)
			self.assertEqual(same["id"], first["id"])
			original.write_text("합성 자료 둘째 버전", encoding="utf-8")
			second, created = materials.add(root, *materials.source_file(original))
			self.assertTrue(created)
			self.assertEqual((second["version"], second["previous_id"], second["ai_allowed"]), (2, first["id"], False))
			self.assertEqual(materials.allowed(root), [])
			materials.permission(root, second["id"], True)
			self.assertEqual([row["id"] for row in materials.allowed(root)], [second["id"]])
			materials.permission(root, second["id"], False)
			self.assertEqual(materials.allowed(root), [])
			self.assertEqual(storage.get(root, "materials", first["id"], "m")["content"], "합성 자료 첫 버전")
		self.assertEqual(original.read_text(encoding="utf-8"), "합성 자료 둘째 버전")
		for path in (self.hub / "jobs").rglob("*"):
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
		self.assertEqual(stat.S_IMODE((self.hub / "jobs").stat().st_mode), 0o700)

	def test_direct_input_versions_cannot_branch_from_an_old_version(self):
		with storage.transaction() as root:
			first, _ = materials.add(root, "합성 첫 자료")
			second, _ = materials.add(root, "합성 변경 자료", previous=first["id"])
			self.assertEqual(second["source"], "직접 입력")
			with self.assertRaises(ValueError):
				materials.add(root, "다른 변경 자료", previous=first["id"])

	def test_url_or_body_duplicate_is_returned_without_overwriting(self):
		with storage.transaction() as root:
			first, created = applications.register(root, "https://example.invalid/a", "합성 공고", "합성", "직무", "미확인")
			self.assertTrue(created)
			by_url, created = applications.register(root, "https://EXAMPLE.invalid/a#top", "다른 본문", "변경", "직무", "미확인")
			self.assertFalse(created)
			self.assertEqual(by_url, first)
			by_body, created = applications.register(root, "https://example.invalid/b", "합성 공고", "변경", "직무", "미확인")
			self.assertFalse(created)
			self.assertEqual(by_body, first)
			self.assertEqual(len(storage.records(root, "applications", "a")), 1)

	def test_deadlines_include_unknown_and_sort_in_absolute_time(self):
		with storage.transaction() as root:
			for url, body, due in (("a", "첫 공고", "2026-11-01T09:00:00+09:00"),
					("b", "둘째 공고", "2026-10-31T23:00:00+00:00"), ("c", "셋째 공고", "미확인")):
				applications.register(root, "https://example.invalid/" + url, body, "합성", url, due)
			self.assertEqual([row["role"] for row in applications.deadlines(root)], ["b", "a", "c"])
			for value in ("2026-11-01", "2026-11-01T09:00:00", "잘못된 마감"):
				with self.assertRaises(ValueError):
					applications.register(root, "미확인", "새 공고", "합성", "직무", value)

	def test_safe_paths_lock_and_atomic_failure_preserve_the_existing_record(self):
		with storage.transaction() as root:
			row, _ = materials.add(root, "합성 원본")
			path = root / "materials" / (row["id"] + ".json")
			before = path.read_bytes()
			with self.assertRaises(ConfigError):
				with storage.transaction():
					pass
			with mock.patch("collector.backfill_common.os.replace", side_effect=OSError("실패")):
				with self.assertRaises(OSError):
					materials.permission(root, row["id"], True)
			self.assertEqual(path.read_bytes(), before)
			self.assertEqual(list(path.parent.iterdir()), [path])
			link = self.hub / "linked"
			os.link(path, link)
			with self.assertRaises(ValueError):
				storage.get(root, "materials", row["id"], "m")
			link.unlink()
			path.chmod(0o644)
			with self.assertRaises(ValueError):
				storage.get(root, "materials", row["id"], "m")
			path.chmod(0o600)
		secret = self.hub / ("🔐" + "금지.txt")
		secret.write_text("읽지 않음", encoding="utf-8")
		with self.assertRaises(ValueError):
			materials.source_file(secret)
		link = self.hub / "link.txt"
		link.symlink_to(secret)
		with self.assertRaises(ValueError):
			materials.source_file(link)

	def test_corrupt_application_record_is_not_overwritten(self):
		with storage.transaction() as root:
			row, _ = applications.register(root, "미확인", "합성 공고", "합성", "직무", "미확인")
			row["body"] = "원문 해시와 다른 공고"
			storage.save(root, "applications", row, "a")
			path = root / "applications" / (row["id"] + ".json")
			before = path.read_bytes()
			with self.assertRaises(ValueError):
				applications.get(root, row["id"])
			with self.assertRaises(ValueError):
				applications.register(root, "미확인", "또 다른 공고", "합성", "직무", "미확인")
			self.assertEqual(path.read_bytes(), before)

	def test_cli_register_allow_list_status_and_deadlines(self):
		def run(*args, text=""):
			result = subprocess.run([sys.executable, "-m", "jobs", *args], input=text,
				capture_output=True, text=True, env=os.environ.copy())
			self.assertEqual(result.returncode, 0, result.stderr)
			return json.loads(result.stdout)
		material = run("material", "add", "--stdin", text="합성 이력")["material"]
		self.assertFalse(material["ai_allowed"])
		self.assertTrue(run("material", "allow", material["id"])["ai_allowed"])
		job = run("register", "--company", "합성", "--role", "직무", "--stdin", text="합성 공고")["application"]
		self.assertEqual(job["status"], "접수")
		self.assertEqual(run("status", job["id"], "자료 확인 대기")["status"], "자료 확인 대기")
		self.assertEqual(run("deadlines")[0]["deadline"], "미확인")
		self.assertNotIn("content", run("material", "list")[0])
		self.assertNotIn("body", run("list")[0])


if __name__ == "__main__":
	unittest.main()
