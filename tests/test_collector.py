"""수집기 단위 테스트. 모든 자료는 합성이다(실제 대화·회사 이름을 쓰지 않는다)."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from collector import config, redact, redact_pii, render, sessions, summarize, vault
from collector.ledger import Ledger
from collector.sessions import Session, Turn


def _session(cwd="/w/a", turns=None, tool="claude", sid="s1"):
	when = datetime(2026, 10, 22, 9, 0).astimezone()
	return Session(tool, sid, cwd, when, when, turns=turns or [Turn("user", "질문입니다")])


class RedactTest(unittest.TestCase):
	def test_key_value_and_quoted_values_are_masked(self):
		text = 'password=hunter2 api_key: "abc123" token = xyz'
		out = redact.redact(text)
		self.assertNotIn("hunter2", out)
		self.assertNotIn("abc123", out)
		self.assertNotIn("xyz", out)
		self.assertIn("[보안값]", out)

	def test_url_credentials_and_headers(self):
		out = redact.redact("db https://user:pw@example.com/x and Authorization: Bearer abc.def")
		self.assertNotIn("pw@", out)
		self.assertNotIn("Bearer abc", out)

	def test_plain_text_is_unchanged(self):
		text = "회사 이름 NEWPOC 와 메모리 설계 결정은 남긴다"
		self.assertEqual(redact.redact(text), text)

	def test_token_prefixes(self):
		self.assertNotIn("sk-", redact.redact("key sk-abcdefghijklmnopqrstu"))


class ClassifyTest(unittest.TestCase):
	rules = (
		config.Rule("/w", "personal", "w", False),
		config.Rule("/w/corp", "company", "corp", False),
		config.Rule("/w/*-app", "study", "apps", True),
		config.Rule("/w/corp/skip", "exclude", "", False),
	)

	def test_longest_prefix_wins(self):
		self.assertEqual(config.classify("/w/corp/x", self.rules).cls, "company")
		self.assertEqual(config.classify("/w/other", self.rules).cls, "personal")

	def test_prefix_must_match_whole_segment(self):
		self.assertIsNone(config.classify("/w2/x", self.rules))

	def test_glob_and_exclude(self):
		self.assertEqual(config.classify("/w/my-app", self.rules).cls, "study")
		self.assertEqual(config.classify("/w/corp/skip/y", self.rules).cls, "exclude")
		self.assertIsNone(config.classify(None, self.rules))


class VaultTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.vault = Path(self.tmp.name)
		(self.vault / "📥 Inbox/AI").mkdir(parents=True)
		self.day = date(2026, 10, 22)
		self.path = vault.daily_path(self.vault, "personal", self.day)

	def tearDown(self):
		self.tmp.cleanup()

	def test_upsert_is_idempotent_and_keeps_human_text(self):
		self.path.write_text("---\ntype: daily\n---\n\n사람이 쓴 메모\n\n## ai-hub\n\n- 손글씨\n", encoding="utf-8")
		self.assertTrue(vault.upsert_block(self.path, "k1", "- 첫 줄", "ai-hub", self.day, "personal"))
		self.assertFalse(vault.upsert_block(self.path, "k1", "- 첫 줄", "ai-hub", self.day, "personal"))
		self.assertTrue(vault.upsert_block(self.path, "k1", "- 바뀐 줄", "ai-hub", self.day, "personal"))
		text = self.path.read_text(encoding="utf-8")
		self.assertEqual(text.count("<!-- ai-hub:k1 -->"), 1)
		self.assertIn("- 바뀐 줄", text)
		self.assertNotIn("- 첫 줄", text)
		self.assertIn("사람이 쓴 메모", text)
		self.assertIn("- 손글씨", text)

	def test_new_heading_is_appended_and_new_file_gets_frontmatter(self):
		vault.upsert_block(self.path, "k1", "- a", "projA", self.day, "personal")
		vault.upsert_block(self.path, "k2", "- b", "projB", self.day, "personal")
		text = self.path.read_text(encoding="utf-8")
		self.assertTrue(text.startswith("---\ntype: daily\nsensitivity: personal"))
		self.assertLess(text.index("## projA"), text.index("## projB"))

	def test_missing_landing_folder_is_an_error(self):
		bad = vault.daily_path(self.vault, "company", self.day)
		with self.assertRaises(FileNotFoundError):
			vault.upsert_block(bad, "k", "- x", "h", self.day, "company")


class SummarizeTest(unittest.TestCase):
	def test_pick_plan_respects_allow_and_daily_limit(self):
		plans = config.Plans(
			{
				"a": config.Plan("a", "claude", "", "personal", ("personal",), None, 1),
				"b": config.Plan("b", "codex", "", "personal", ("personal",), None, None),
			},
			{"personal": ("a", "b"), "company": ()},
		)
		with tempfile.TemporaryDirectory() as tmp:
			ledger = Ledger(Path(tmp) / "l.db")
			self.assertEqual(summarize.pick_plan("personal", plans, ledger, "d").id, "a")
			ledger.bump_call("a", "d")
			self.assertEqual(summarize.pick_plan("personal", plans, ledger, "d").id, "b")
			self.assertIsNone(summarize.pick_plan("company", plans, ledger, "d"))
			self.assertIsNone(summarize.pick_plan("personal", None, ledger, "d"))
			ledger.close()

	def test_prompt_is_truncated_and_bounded(self):
		turns = [Turn("user" if i % 2 == 0 else "assistant", "가" * 2000) for i in range(40)]
		prompt = summarize.build_prompt(_session(turns=turns))
		self.assertLessEqual(len(prompt), summarize.MAX_PROMPT_CHARS)
		self.assertIn("--- 대화 끝 ---", prompt)


class RenderTest(unittest.TestCase):
	def test_minimal_body_has_no_answer_text(self):
		body = render.session_body(_session(turns=[Turn("user", "비밀 질문\n둘째 줄")]), None, None, "호출 실패")
		self.assertIn("요약 없음", body)
		self.assertIn("첫 질문: 비밀 질문", body)
		self.assertNotIn("둘째 줄", body)


class SessionReadTest(unittest.TestCase):
	def test_claude_reader_keeps_cli_user_turns_only(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "x.jsonl"
			rows = [
				{"type": "user", "entrypoint": "cli", "sessionId": "c1", "cwd": "/w/a",
					"timestamp": "2026-10-22T01:00:00Z", "message": {"content": "질문 하나"}},
				{"type": "assistant", "entrypoint": "cli", "sessionId": "c1", "cwd": "/w/a",
					"timestamp": "2026-10-22T01:00:05Z", "message": {"content": [
						{"type": "thinking", "thinking": "숨김"}, {"type": "text", "text": "답변"}]}},
				{"type": "user", "entrypoint": "sdk-cli", "sessionId": "c2", "cwd": "/w/b",
					"timestamp": "2026-10-22T01:00:00Z", "message": {"content": "자동 호출"}},
				{"type": "user", "entrypoint": "cli", "sessionId": "c1", "cwd": "/w/a",
					"timestamp": "2026-10-22T01:01:00Z", "message": {"content": [
						{"type": "tool_result", "content": "출력"}]}},
			]
			path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
			out = sessions._claude_file(path, 0)
		self.assertEqual([s.sid for s in out], ["c1"])
		self.assertEqual([(t.role, t.text) for t in out[0].turns], [("user", "질문 하나"), ("assistant", "답변")])

	def test_codex_subagent_thread_is_skipped(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "r.jsonl"
			meta = {"type": "session_meta", "timestamp": "2026-10-22T01:00:00Z",
				"payload": {"id": "t1", "cwd": "/w/a", "thread_source": "subagent", "timestamp": "2026-10-22T01:00:00Z"}}
			msg = {"type": "response_item", "timestamp": "2026-10-22T01:00:01Z",
				"payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "질문"}]}}
			path.write_text("\n".join(json.dumps(r) for r in (meta, msg)), encoding="utf-8")
			self.assertIsNone(sessions._codex_file(path))

	def test_codex_user_text_uses_input_text_items(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "r.jsonl"
			meta = {"type": "session_meta", "timestamp": "2026-10-22T01:00:00Z",
				"payload": {"id": "t2", "cwd": "/w/a", "thread_source": "user", "timestamp": "2026-10-22T01:00:00Z"}}
			msg = {"type": "response_item", "timestamp": "2026-10-22T01:00:01Z",
				"payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "질문"}]}}
			ans = {"type": "response_item", "timestamp": "2026-10-22T01:00:02Z",
				"payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "답"}]}}
			path.write_text("\n".join(json.dumps(r) for r in (meta, msg, ans)), encoding="utf-8")
			out = sessions._codex_file(path)
		self.assertEqual(out.user_count, 1)
		self.assertEqual(out.turns[-1].text, "답")


class ConfigFileTest(unittest.TestCase):
	def test_bad_routing_is_rejected(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "plans.toml"
			path.write_text('[routing]\npersonal = ["ghost"]\n', encoding="utf-8")
			with self.assertRaises(config.ConfigError):
				config.load_plans(path)

	def test_missing_file_is_config_error(self):
		with self.assertRaises(config.ConfigError):
			config.load_sources(Path("/nonexistent/sources.toml"))


class PiiRedactTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": self.tmp.name})
		self.env.start()
		redact_pii.host_pattern.cache_clear()

	def tearDown(self):
		self.env.stop()
		redact_pii.host_pattern.cache_clear()
		self.tmp.cleanup()

	def write_hosts(self, domains):
		Path(self.tmp.name, "redact_hosts.toml").write_text(
			"domains = [" + ", ".join(f'"{d}"' for d in domains) + "]\n", encoding="utf-8")
		redact_pii.host_pattern.cache_clear()

	def test_email_phone_and_ip_are_masked(self):
		out = redact.redact("메일 a.b@example.co.kr 전화 010-1234-5678 또는 +82 10 2345 6789 서버 10.20.30.40:8080")
		self.assertNotIn("example.co.kr", out)
		self.assertNotIn("1234-5678", out)
		self.assertNotIn("2345 6789", out)
		self.assertNotIn("10.20.30.40", out)
		self.assertIn(":8080", out)

	def test_versions_dates_and_amounts_are_kept(self):
		text = "버전 3.14.6 날짜 2026-10-22 금액 1,234,000 원 번호 1.2.3.4.5"
		self.assertEqual(redact.redact(text), text)

	def test_internal_hosts_only_when_configured(self):
		self.assertIn("wiki.corp.test", redact.redact("주소 wiki.corp.test:8443"))
		self.write_hosts(["corp.test"])
		out = redact.redact("주소 wiki.corp.test:8443 그리고 notcorp.test")
		self.assertNotIn("wiki.corp.test", out)
		self.assertIn("notcorp.test", out)  # 접미사가 같아도 다른 도메인은 가리지 않는다

	def test_count_reports_kinds_without_content(self):
		self.write_hosts(["corp.test"])
		counts = redact_pii.count_pii("a@b.co 010-1234-5678 10.1.1.1 host.corp.test")
		self.assertEqual(counts, {"email": 1, "phone": 1, "ipv4": 1, "host": 1})


if __name__ == "__main__":
	unittest.main()
