"""공급자 호출 인자를 임시 폴더로 확인한다(실제 AI 호출 없음)."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from collector.config import Plan
from orchestrator import providers


class CodexArgumentsTest(unittest.TestCase):
	def argv(self, folder, writer=False):
		plan = Plan("p01", "codex", "합성", "personal", ("personal",), None, None)
		return providers._codex(plan, writer, Path(folder), Path("schema.json"), Path("last.json"))[0]

	def test_outside_git_skips_trust_check_but_keeps_sandbox(self):
		with tempfile.TemporaryDirectory() as folder:
			argv = self.argv(folder)
		self.assertIn("--skip-git-repo-check", argv)
		self.assertEqual(argv[argv.index("-s") + 1], "read-only")

	def test_inside_git_does_not_add_the_flag(self):
		with tempfile.TemporaryDirectory() as folder:
			subprocess.run(["git", "init", "-q", folder], check=True)
			self.assertNotIn("--skip-git-repo-check", self.argv(folder, writer=True))


class ExitCauseTest(unittest.TestCase):
	def test_all_causes_match_in_either_stream_and_ignore_case(self):
		cases = {
			"한도 소진": ("resource_exhausted", "quota", "rate limit", "usage limit", "too many requests", "429"),
			"로그인 확인 필요": ("unauthorized", "not logged in", "log in required", "please log in", "sign in",
				"authentication", "invalid api key", "401", "403"),
			"입력 길이 초과": ("context length", "context window", "prompt is too long", "too many tokens", "maximum context"),
			"네트워크·서버 오류": ("network", "connection reset", "connection refused", "connection closed", "econnreset",
				"etimedout", "timed out", "dns", "stream disconnected", "failed to connect", "temporarily unavailable",
				"500", "502", "503", "504"),
			"원인 미분류": ("", "SECRETLINE", "잘못된 응답"),
		}
		for cause, phrases in cases.items():
			for phrase in phrases:
				for stderr, stdout in ((phrase.upper(), ""), ("", phrase.upper())):
					with self.subTest(cause=cause, stderr=stderr, stdout=stdout):
						self.assertEqual(providers._exit_cause(stderr, stdout), cause)

	def test_prompt_echo_at_the_start_does_not_decide_the_cause(self):
		# 구현 AI 가 시작할 때 되풀이하는 프롬프트에 quota·429 같은 단어가 있어도, 오래 돈 뒤의 끝부분 오류가 분류를 정한다
		echo = "요청서: Individual quota reached 429 RESOURCE_EXHAUSTED rate limit\n" * 20
		progress = "진행 중인 작업 한 줄\n" * 400
		self.assertEqual(providers._exit_cause(echo + progress + "connection reset by peer", ""), "네트워크·서버 오류")
		self.assertEqual(providers._exit_cause(echo + progress + "something broke", ""), "원인 미분류")
		self.assertEqual(providers._exit_cause("", echo + progress + "Error: 429 quota exceeded"), "한도 소진")

	def test_priority_and_numeric_word_boundaries(self):
		for stderr, stdout, expected in (("network", "429", "한도 소진"),
				("quota", "unauthorized context length network", "한도 소진"),
				("context length network", "401", "로그인 확인 필요"),
				("network", "maximum context", "입력 길이 초과")):
			with self.subTest(stderr=stderr, stdout=stdout):
				self.assertEqual(providers._exit_cause(stderr, stdout), expected)
		for code in ("429", "401", "403", "500", "502", "503", "504"):
			for text in ("1" + code, code + "0", "x" + code, code + "x", "_" + code + "_"):
				with self.subTest(text=text):
					self.assertEqual(providers._exit_cause(text, ""), "원인 미분류")


class ProviderExitTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub, self.user_home = self.root / "hub", self.root / "xyz"
		self.log = self.hub / "logs/provider-errors.jsonl"
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(providers.Path, "home", return_value=self.user_home)):
			patch.start()
			self.addCleanup(patch.stop)

	def fail(self, stderr, stdout="SECRETLINE", tool="agy"):
		plan = Plan("p01", tool, "합성", "personal", ("personal",), None, None)
		def fake(argv, **kwargs):
			return subprocess.CompletedProcess(argv, 0 if argv[0] == "git" else 7, stdout, stderr)
		with mock.patch.object(providers.subprocess, "run", side_effect=fake):
			with self.assertRaises(providers.ProviderError) as caught:
				providers.call(plan, True, "합성 질문", self.root, {}, 60)
		return caught.exception

	def test_classified_exit_messages_never_echo_output_or_create_logs(self):
		for tool in ("agy", "codex"):
			for phrase, cause in (("quota", "한도 소진"), ("401", "로그인 확인 필요"),
					("too many tokens", "입력 길이 초과"), ("503", "네트워크·서버 오류")):
				with self.subTest(tool=tool, cause=cause):
					error = self.fail(phrase + " SECRETLINE", tool=tool)
					self.assertEqual(error.kind, "exit")
					self.assertEqual(str(error), f"p01 종료 코드 7 ({cause})")
					self.assertNotIn("SECRETLINE", str(error))
		self.assertFalse(self.log.exists())

	def test_unclassified_stderr_tail_is_masked_in_private_json_log(self):
		token = "ghp_" + "a" * 36
		other = "aZ0_-/+=" * 5
		suffix = f" {self.user_home}/note {token} {other}\n\t\x00\x7f\x85끝"
		error = self.fail("앞" * 650 + suffix, stdout="버릴 표준 출력")
		self.assertEqual(str(error), "p01 종료 코드 7 (원인 미분류)")
		lines = self.log.read_text(encoding="utf-8").splitlines()
		self.assertEqual(len(lines), 1)
		entry = json.loads(lines[0])
		self.assertEqual(set(entry), {"at", "plan", "tool", "returncode", "tail"})
		self.assertIsNotNone(datetime.fromisoformat(entry["at"]).tzinfo)
		self.assertEqual((entry["plan"], entry["tool"], entry["returncode"]), ("p01", "agy", 7))
		self.assertEqual(entry["tail"], "앞" * (600 - len(suffix)) + " ~/note *** ***     끝")
		for raw in (str(self.user_home), token, other, "버릴 표준 출력", "\n", "\t", "\x00", "\x7f", "\x85"):
			self.assertNotIn(raw, entry["tail"])
		self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

	def test_empty_stderr_uses_stdout_and_append_preserves_existing_rows(self):
		self.fail("첫 실패", stdout="버릴 출력")
		self.log.chmod(0o644)
		self.fail("", stdout="앞" * 650 + "마지막")
		entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
		self.assertEqual([entry["tail"] for entry in entries], ["첫 실패", "앞" * 597 + "마지막"])
		self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

	def test_oversized_log_is_not_appended(self):
		self.log.parent.mkdir(parents=True)
		content = "x" * (2 * 1024 * 1024 + 1)
		self.log.write_text(content, encoding="utf-8")
		self.fail("SECRETLINE")
		self.assertEqual(self.log.read_text(encoding="utf-8"), content)

	def test_log_oserror_does_not_replace_provider_error(self):
		for target in ("orchestrator.providers.Path.mkdir", "orchestrator.providers.os.fchmod"):
			with self.subTest(target=target), mock.patch(target, side_effect=OSError("합성 기록 오류")):
				error = self.fail("SECRETLINE")
				self.assertEqual(error.kind, "exit")
				self.assertEqual(str(error), "p01 종료 코드 7 (원인 미분류)")


if __name__ == "__main__":
	unittest.main()
