"""마스킹 필터 단위 테스트. 합성 이름만 쓴다."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import redact_pii
from vaultfilter.codec import decode, encode
from vaultfilter.store import Dictionary
from vaultfilter.verify import _find_leaks


class CodecTest(unittest.TestCase):
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

	def test_round_trip_restores_original_exactly(self):
		self.dic.entries.append({"value": "ACME", "kind": "회사명", "token": "⟦회사명-1⟧"})
		self.dic.entries.append({"value": "ACME Mobile", "kind": "서비스명", "token": "⟦서비스명-1⟧"})
		text = "ACME Mobile 문서와 ACME 설정, 메일 dev@acme.test 010-1234-5678, 서버 10.1.2.3"
		enc = encode(text, self.dic)
		self.assertNotIn("ACME", enc)
		self.assertNotIn("010-1234-5678", enc)
		self.assertNotIn("10.1.2.3", enc)
		self.assertEqual(decode(enc, self.dic), text)

	def test_case_variants_and_joined_words_are_masked_and_restored(self):
		self.dic.entries.append({"value": "xyz", "kind": "서비스명", "token": "⟦서비스명-1⟧"})
		text = "xyz-app, XYZ 문서, insertXyzUser(), com.xyz.usr, xyzpoc"
		enc = encode(text, self.dic)
		self.assertNotIn("xyz", enc.lower())
		self.assertEqual(decode(enc, self.dic), text)
		tokens = {e["value"]: e["token"] for e in self.dic.entries}
		self.assertEqual(len({tokens["xyz"], tokens["XYZ"], tokens["Xyz"]}), 3)  # 표기마다 토큰이 다르다
		self.assertEqual(encode(text, self.dic), enc)  # 다시 돌려도 같은 결과

	def test_verify_catches_variants_left_unmasked(self):
		self.assertEqual(_find_leaks("insertXyzUser 와 XYZ", ["xyz"]), ["사전 원문"])
		self.assertEqual(_find_leaks("⟦서비스명-1⟧ 만 있음", ["xyz"]), [])

	def test_same_value_gets_same_token_and_dictionary_grows_once(self):
		first = encode("a 010-1234-5678", self.dic)
		second = encode("b 010-1234-5678", self.dic)
		self.assertEqual(len(self.dic.entries), 1)
		self.assertEqual(first.split()[1], second.split()[1])

	def test_dictionary_saves_and_reloads(self):
		encode("x@y.test", self.dic)
		self.dic.save()
		again = Dictionary(self.dic.path)
		self.assertEqual(decode(encode("x@y.test", again), again), "x@y.test")

	def test_unknown_kind_is_rejected(self):
		with self.assertRaises(ValueError):
			self.dic.token_for("값", "없는종류")


if __name__ == "__main__":
	unittest.main()
