"""자문 명령이 글을 검사한 뒤 빈 폴더에서 읽기 전용으로 부르고 해시만 기록하는지 확인한다."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.config import ConfigError
from orchestrator import consult, providers

PLANS = ('[[plans]]\nid="agy-argon"\ntool="agy"\nowner="personal"\nallow=["personal"]\n'
	'[[plans]]\nid="c01"\ntool="agy"\nowner="company"\nallow=["company"]\n')


class ConsultTest(unittest.TestCase):
	def setUp(self):
		temp = tempfile.TemporaryDirectory()
		self.addCleanup(temp.cleanup)
		self.hub = Path(temp.name).resolve()
		(self.hub / "redaction").mkdir()
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "company01"}]}), encoding="utf-8")
		(self.hub / "plans.toml").write_text(PLANS, encoding="utf-8")
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch("vaultfilter.public._allowed", return_value=set())):
			patch.start()
			self.addCleanup(patch.stop)

	def test_calls_read_only_in_an_empty_folder_and_logs_only_a_hash(self):
		seen = {}

		def call(plan, writer, prompt, cwd, schema, timeout):
			seen.update(plan=plan.id, writer=writer, prompt=prompt, files=list(cwd.iterdir()))
			return providers.Reply({"verdict": "alternative", "recommendation": "더 작은 안", "reasons": ["근거"], "risks": []}, 2.04, {})

		with mock.patch.object(providers, "call", side_effect=call):
			result = consult.consult("합성 문제와 지금 안")
		self.assertEqual((seen["plan"], seen["writer"], seen["files"]), ("agy-argon", False, []))
		self.assertIn("합성 문제와 지금 안", seen["prompt"])
		self.assertEqual((result["verdict"], result["seconds"]), ("alternative", 2.0))
		line = (self.hub / "logs/consult.jsonl").read_text(encoding="utf-8")
		self.assertNotIn("합성 문제", line)
		self.assertEqual(json.loads(line)["verdict"], "alternative")

	def test_rejects_unsafe_text_bad_plans_and_malformed_replies(self):
		with mock.patch.object(providers, "call", side_effect=AssertionError("호출 금지")):
			for text in ("acme 저장소 문제", "경로 ~/.ai-hub/x", "연락 a@b.co"):
				with self.subTest(text=text), self.assertRaises(ValueError):
					consult.consult(text)
			for identity in ("c01", "없는-요금제"):
				with self.subTest(plan=identity), self.assertRaises(ConfigError):
					consult.consult("합성 문제", identity)
		malformed = providers.Reply({"verdict": "좋음", "recommendation": "", "reasons": [], "risks": []}, 1.0, {})
		with mock.patch.object(providers, "call", return_value=malformed), self.assertRaises(ValueError):
			consult.consult("합성 문제")


if __name__ == "__main__":
	unittest.main()
