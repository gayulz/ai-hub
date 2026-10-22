"""가상 저장소로 푸시 검사 범위와 사전 누락 시 차단을 확인한다."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import redact_pii
from vaultfilter.__main__ import cmd_filter
from vaultfilter.store import Dictionary
from vaultfilter.verify import _find_leaks, check_tree


class VerifyTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.root / "config")})
		self.env.start()
		redact_pii.host_pattern.cache_clear()
		self.repo = self.root / "repo"
		self.repo.mkdir()
		self.git("init", "-q")
		self.git("config", "user.name", "테스트")
		self.git("config", "user.email", "test@example.test")
		self.git("config", "commit.gpgsign", "false")
		self.dic = Dictionary()
		self.dic.token_for("Acme", "회사명")
		self.dic.save()

	def tearDown(self):
		self.env.stop()
		redact_pii.host_pattern.cache_clear()
		self.tmp.cleanup()

	def git(self, *args):
		return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True).stdout

	def commit(self, name, text):
		(self.repo / name).write_text(text, encoding="utf-8")
		self.git("add", "--", name)
		self.git("commit", "-q", "-m", "합성 자료")

	def check(self, revs="HEAD"):
		output = io.StringIO()
		with mock.patch("vaultfilter.verify._git", side_effect=self.git), contextlib.redirect_stdout(output):
			count = check_tree(revs)
		return count, output.getvalue()

	def test_path_and_non_markdown_content_are_checked_without_printing_values(self):
		self.commit("Acme-notes.md", "무해한 본문")
		self.commit("settings.json", '{"name": "Acme"}')
		count, output = self.check()
		self.assertGreaterEqual(count, 2)
		self.assertNotIn("Acme", output)

	def test_secret_in_ancestor_is_rejected_even_after_deletion(self):
		fake = "ghp_" + "a" * 36
		self.commit("note.md", fake)
		first = self.git("rev-parse", "HEAD").decode().strip()
		self.commit("note.md", "[보안값]")
		self.assertEqual(self.check(first + "..HEAD")[0], 0)
		count, output = self.check()
		self.assertEqual(count, 1)
		self.assertNotIn(fake, output)

	def test_protected_note_is_rejected_without_reading_its_blob(self):
		self.commit("🔐private.md", "읽지 않아야 하는 합성 자료")
		calls = []
		def git(*args):
			calls.append(args)
			return self.git(*args)
		with mock.patch("vaultfilter.verify._git", side_effect=git), contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(check_tree(), 1)
		self.assertFalse(any(args[0] == "cat-file" for args in calls))

	def test_missing_dictionary_blocks_clean_and_verification(self):
		self.dic.path.unlink()
		with self.assertRaises(ValueError):
			cmd_filter("clean")
		with self.assertRaises(ValueError):
			check_tree()

	def test_strong_secrets_are_rejected_and_long_css_data_is_safe_to_scan(self):
		fake = "ghp_" + "b" * 36
		self.assertEqual(_find_leaks(fake, []), ["비밀값"])
		self.assertEqual(_find_leaks('MAIL_KEY = "' + 'c' * 24 + '"', []), ["비밀값"])
		self.assertEqual(_find_leaks("x" * 200_000 + " @media", []), [])
		self.assertEqual(_find_leaks("-" * 200_000 + " @media", []), [])

	def test_author_metadata_is_checked(self):
		self.git("config", "user.name", "Acme")
		self.commit("note.md", "무해한 본문")
		count, output = self.check()
		self.assertEqual(count, 1)
		self.assertNotIn("Acme", output)


if __name__ == "__main__":
	unittest.main()
