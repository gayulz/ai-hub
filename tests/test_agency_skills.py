"""스킬 로더를 임시 비공개·저장소 루트에서 검사한다."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency import skills
from collector.config import ConfigError


class SkillsTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub, self.repo = self.root / "hub", self.root / "repo"
		self.hub.mkdir()
		self.repo.mkdir()
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(skills, "REPO", self.repo)):
			patch.start()
			self.addCleanup(patch.stop)

	def write(self, root=None, name="xyz", text=None):
		path = (root or self.hub) / "skills" / name / "SKILL.md"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text if text is not None else
			f"---\nname: {name}\ndescription: 합성 스킬\n---\n합성 본문\n", encoding="utf-8")
		return path

	def test_private_priority_repo_fallback_body_digest_and_prompt(self):
		repo = self.write(self.repo, text="---\nname: xyz\ndescription: 공개 스킬\n---\n공개 본문\n")
		loaded = skills.load("xyz")
		self.assertEqual((loaded.name, loaded.description, loaded.body, loaded.source),
			("xyz", "공개 스킬", "공개 본문", "repo"))
		self.assertEqual(loaded.sha256, hashlib.sha256(repo.read_bytes()).hexdigest())
		private = self.write()
		loaded = skills.load("xyz")
		self.assertEqual((loaded.source, loaded.body), ("private", "합성 본문"))
		self.assertEqual(loaded.sha256, hashlib.sha256(private.read_bytes()).hexdigest())
		self.write(name="abc")
		self.assertEqual(skills.prompt_block([loaded, skills.load("abc")]),
			"## 스킬: xyz\n합성 본문\n\n## 스킬: abc\n합성 본문")
		self.assertEqual(skills.prompt_block([]), "")

	def test_rejects_bad_names_missing_files_and_header_fields(self):
		for name in ("", "../xyz", "/xyz/private", "Upper", "a_1", "a" * 65, None):
			with self.subTest(name_type=type(name).__name__), self.assertRaises(ConfigError):
				skills.load(name)
		with self.assertRaises(ConfigError):
			skills.load("missing")
		for text in ("본문", "\n---\nname: xyz\ndescription: 합성\n---", "---\nname: xyz",
				"---\nname: other\ndescription: 합성\n---", "---\nname: xyz\n---",
				"---\nname: xyz\ndescription: \n---", "---\nname: xyz\ndescription: " + "가" * 1025 + "\n---",
				"---\nname: xyz\nname: xyz\ndescription: 합성\n---",
				"---\nname: xyz\ndescription: >\n  합성\n---"):
			self.write(text=text)
			with self.subTest(length=len(text)), self.assertRaises(ConfigError):
				skills.load("xyz")

	def test_quoted_header_and_other_metadata(self):
		self.write(text='---\nname: "xyz"\ndescription: \'합성: 스킬\'\nlicense: MIT\n---\n## 절차\n본문')
		loaded = skills.load("xyz")
		self.assertEqual(loaded.description, "합성: 스킬")
		self.assertEqual(loaded.body, "## 절차\n본문")

	def test_size_boundary_invalid_utf8_and_nonregular_file(self):
		path = self.write()
		header = b"---\nname: xyz\ndescription: xyz\n---\n"
		path.write_bytes(header + b"a" * (65536 - len(header)))
		self.assertEqual(len(skills.load("xyz").body), 65536 - len(header))
		with path.open("ab") as stream:
			stream.write(b"a")
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		path.write_bytes(b"---\nname: xyz\ndescription: xyz\n---\n\xff")
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		path.unlink()
		path.mkdir()
		with self.assertRaises(ConfigError):
			skills.load("xyz")

	def test_private_links_and_parent_links_cannot_fall_back_to_repo(self):
		self.write(self.repo)
		outside = self.root / "outside.md"
		outside.write_text("합성 자료", encoding="utf-8")
		private = self.hub / "skills" / "xyz"
		private.mkdir(parents=True)
		path = private / "SKILL.md"
		path.symlink_to(outside)
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		path.unlink()
		path.symlink_to(self.root / "missing")
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		path.unlink()
		private.rmdir()
		private.symlink_to(self.repo / "skills" / "xyz", target_is_directory=True)
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		private.unlink()
		private.parent.rmdir()
		private.parent.symlink_to(self.repo / "skills", target_is_directory=True)
		with self.assertRaises(ConfigError):
			skills.load("xyz")

	def test_repo_link_and_invalid_private_file_do_not_load_another_source(self):
		repo = self.write(self.repo)
		outside = self.root / "outside.md"
		outside.write_bytes(repo.read_bytes())
		repo.unlink()
		repo.symlink_to(outside)
		with self.assertRaises(ConfigError):
			skills.load("xyz")
		repo.unlink()
		self.write(self.repo)
		self.write(text="잘못된 비공개 스킬")
		with self.assertRaises(ConfigError) as caught:
			skills.load("xyz")
		self.assertNotIn(str(self.root), str(caught.exception))
