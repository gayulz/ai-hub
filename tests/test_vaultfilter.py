"""마스킹 사전과 누출 탐지 단위 테스트. 합성 이름만 쓴다."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import redact_pii
from vaultfilter.store import Dictionary
from vaultfilter.verify import _find_leaks


class DictionaryTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": self.tmp.name})
		self.env.start()
		redact_pii.host_pattern.cache_clear()
		self.dic = Dictionary(Path(self.tmp.name) / "d.json")

	def tearDown(self):
		self.env.stop()
		redact_pii.host_pattern.cache_clear()
		self.tmp.cleanup()

	def test_verify_catches_case_variants_and_joined_words(self):
		self.assertEqual(_find_leaks("insertXyzUser 와 XYZ", ["xyz"]), ["사전 원문"])
		self.assertEqual(_find_leaks("⟦서비스명-1⟧ 만 있음", ["xyz"]), [])

	def test_same_value_gets_same_token_and_numbers_follow_kind(self):
		first = self.dic.token_for("acme", "회사명")
		self.assertEqual(self.dic.token_for("acme", "회사명"), first)
		self.assertEqual(self.dic.token_for("xyz", "서비스명"), "⟦서비스명-1⟧")
		self.assertEqual(self.dic.token_for("acme2", "회사명"), "⟦회사명-2⟧")
		self.assertEqual(len(self.dic.entries), 3)

	def test_dictionary_saves_and_reloads(self):
		token = self.dic.token_for("x@y.test", "이메일")
		self.dic.save()
		again = Dictionary(self.dic.path)
		self.assertEqual(again.token_for("x@y.test", "이메일"), token)
		self.assertEqual(len(again.entries), 1)

	def test_unknown_kind_is_rejected(self):
		with self.assertRaises(ValueError):
			self.dic.token_for("값", "없는종류")


if __name__ == "__main__":
	unittest.main()
