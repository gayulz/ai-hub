"""산출물의 새 파일 계약과 정렬된 식별값을 검사한다."""

import hashlib
import os

from shorts import Conflict, StoreError, artifacts
from test_shorts_support import SID, ShortsCase


class ArtifactTest(ShortsCase):
	def test_bytes_metadata_digest_and_no_overwrite(self):
		content = "합성 내용".encode("utf-8")
		run = self.add("script-v1.txt", "script", content)
		row = run["artifacts"][0]
		self.assertEqual((row["name"], row["kind"], row["sha256"], row["size"]),
			("script-v1.txt", "script", hashlib.sha256(content).hexdigest(), len(content)))
		path = self.round_path() / "artifacts/script-v1.txt"
		before = path.read_bytes()
		with self.assertRaises(Conflict):
			self.add("script-v1.txt", "script", b"replacement")
		with self.assertRaises(Conflict):
			self.add("SCRIPT-v1.txt", "script", b"replacement")
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual(self.read(), run)

	def test_digest_ignores_registration_order_and_report_is_outside_review_scope(self):
		self.add("b.txt", "script", b"b")
		run = self.add("a.txt", "voice", b"a")
		self.assertEqual(artifacts.manifest_digest(run["artifacts"]), artifacts.manifest_digest(list(reversed(run["artifacts"]))))
		first = artifacts.manifest_digest(run["artifacts"], {"script", "voice"})
		run = self.add("report.txt", "report")
		self.assertEqual(first, artifacts.manifest_digest(run["artifacts"], {"script", "voice"}))
		second = self.store.new(SID)
		self.number = second["round"]
		self.add("a.txt", "voice", b"a")
		run = self.add("b.txt", "script", b"b")
		self.assertEqual(first, artifacts.manifest_digest(run["artifacts"]))

	def test_names_kinds_and_non_bytes_are_rejected_before_creation(self):
		for name in ("../escape", "/absolute", "a/b", "a\\b", ".hidden", "a..b", "", "a" * 129,
				"한글.txt", "a\x00", "a\n"):
			with self.subTest(name=name), self.assertRaises(StoreError):
				self.add(name, "script")
		for kind, content in (("other", b"a"), ([], b"a"), ("script", "text"), ("script", bytearray(b"a"))):
			with self.assertRaises(StoreError):
				self.add("blocked.txt", kind, content)
		self.assertEqual(list((self.round_path() / "artifacts").iterdir()), [])
		self.assertEqual(self.read()["artifacts"], [])

	def test_existing_unregistered_file_symlink_and_hardlink_never_overwrite_targets(self):
		folder = self.round_path() / "artifacts"
		target = self.root / "target"
		target.write_bytes(b"original")
		target.chmod(0o600)
		(folder / "existing.txt").write_bytes(b"existing")
		(folder / "link.txt").symlink_to(target)
		os.link(target, folder / "hard.txt")
		for name in ("existing.txt", "link.txt", "hard.txt"):
			with self.subTest(name=name), self.assertRaises(Conflict):
				self.add(name, "report", b"replacement")
		self.assertEqual(target.read_bytes(), b"original")
		self.assertEqual((folder / "existing.txt").read_bytes(), b"existing")
		self.assertEqual(self.read()["artifacts"], [])
