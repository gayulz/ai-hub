"""합성 대화로 끝 신호·작업 기록·하루 중간 기록을 확인한다."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from collector import config, hooks, pipeline, redact_pii, sessions, sessions_other, vault, work_records
from collector.ledger import Ledger
from collector.sessions import Session, Turn


def record(number=1):
	return f"## 작업 기록\n- 결정: {number}번 선택\n- 이유: 자료 보존\n- 실패·막힌 것: 없음\n- 남은 일: 확인"


class CollectorC2Test(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.vault = self.root / "vault"
		self.now = datetime(2026, 10, 23, 12).astimezone()
		patch = mock.patch.dict(os.environ, {"HOME": str(self.root / "acme"), "AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		redact_pii.host_pattern.cache_clear()
		self.addCleanup(redact_pii.host_pattern.cache_clear)
		(self.hub / "sources.toml").write_text('vault = ' + json.dumps(str(self.vault)) + '''
[[classify]]
path = "/w/acme"
class = "company"
name = "acme"
''')
		self.sources = config.load_sources()
		self.ledger = Ledger(self.hub / "ledger.db")
		self.addCleanup(self.ledger.close)
		for folder in vault.landing_dirs(self.vault):
			folder.mkdir(parents=True)
		self.session = Session("claude", "s01", "/w/acme", self.now - timedelta(hours=1),
			self.now, [Turn("user", "합성 질문"), Turn("assistant", record())])

	def runner(self, now=None, dry_run=False, plans=None):
		runner = pipeline.Pipeline(self.sources, plans, self.ledger, dry_run)
		runner.now = now or self.now
		runner.today = runner.now.date()
		return runner

	def signal(self, tool="claude", event="SessionEnd", now=None):
		with mock.patch.object(hooks, "kickstart"):
			hooks.receive(tool, {"session_id": self.session.sid, "cwd": self.session.cwd,
				"hook_event_name": event}, now or self.now)

	def path(self, day=None):
		return vault.daily_path(self.vault, "company", day or self.session.last.date())

	def test_immediate_pending_records_active_session_and_updates_same_block(self):
		self.signal()
		with mock.patch("collector.summarize.call") as call:
			self.runner().handle_session(self.session)
			call.assert_not_called()
		text = self.path().read_text()
		self.assertIn(record(), text)
		self.assertEqual(self.ledger.get(self.session.key)["plan"], work_records.PLAN)
		self.assertEqual(hooks.read_pending(), {})
		self.session.turns.append(Turn("assistant", record(2)))
		self.signal()
		self.runner().handle_session(self.session)
		text = self.path().read_text()
		self.assertEqual(text.count("<!-- ai-hub:claude:s01 -->"), 1)
		self.assertIn(record(2), text)
		self.signal()
		self.runner().handle_session(self.session)
		self.assertFalse(hooks.read_pending())

	def test_delayed_signals_block_normal_fifteen_minute_collection(self):
		for tool, event in (("codex", "Stop"), ("gemini", "AfterAgent")):
			with self.subTest(tool=tool):
				self.session.tool = tool
				self.signal(tool, event)
				self.session.last = self.now
				self.runner(self.now + timedelta(minutes=20)).handle_session(self.session)
				self.assertIsNone(self.ledger.get(self.session.key))
				self.assertTrue(hooks.read_pending())
				self.session.last = self.now + timedelta(minutes=29)
				self.runner(self.now + timedelta(minutes=30)).handle_session(self.session)
				self.assertEqual(self.ledger.get(self.session.key)["status"], "recorded")
				self.assertFalse(hooks.read_pending())

	def test_daily_interim_once_for_changed_active_session_and_next_day(self):
		self.session.started -= timedelta(days=4)
		evening = self.now.replace(hour=23, minute=10)
		self.session.last = evening - timedelta(minutes=1)
		self.runner(evening - timedelta(hours=1)).handle_session(self.session)
		self.assertIsNone(self.ledger.get(self.session.key))
		self.runner(evening).handle_session(self.session)
		first = self.path().read_text()
		self.session.turns.append(Turn("assistant", record(2)))
		self.session.last += timedelta(minutes=2)
		self.runner(evening + timedelta(minutes=2)).handle_session(self.session)
		self.assertEqual(self.path().read_text(), first)
		self.session.last = evening + timedelta(days=1) - timedelta(minutes=1)
		self.runner(evening + timedelta(days=1)).handle_session(self.session)
		self.assertIn(record(2), self.path().read_text())
		self.assertEqual(self.path(evening.date()).read_text(), first)
		self.assertFalse(self.path(self.session.started.date()).exists())

	def test_record_blocks_keep_recent_ten_and_redact_without_rewriting(self):
		self.session.turns = [Turn("user", record(99)), Turn("assistant", "```markdown\n" + record(98) + "\n```")]
		for number in range(12):
			self.session.turns.append(Turn("assistant", "결과\n" + record(number) + "\n## 다른 절\n다른 내용"))
		self.session.turns.append(Turn("assistant", record(97), "commentary"))
		self.session.turns.append(Turn("assistant", record(96), complete=False))
		out = work_records.extract(self.session)
		self.assertEqual(out, "\n\n".join(record(number) for number in range(2, 12)))
		self.session.turns = [Turn("assistant", record() + "\n- 남은 일: api_key=synthetic-value xyz@example.invalid 10.20.30.40")]
		out = work_records.extract(self.session)
		self.assertIn("- 결정: 1번 선택", out)
		for value in ("synthetic-value", "xyz@example.invalid", "10.20.30.40"):
			self.assertNotIn(value, out)
		self.session.turns = [Turn("assistant", record() + "\n```text\n남긴 자료")]
		self.assertEqual(work_records.extract(self.session), self.session.turns[0].text)

	def test_end_signal_does_not_reset_the_daily_interim_limit(self):
		evening = self.now.replace(hour=23, minute=5)
		self.session.last = evening
		self.runner(evening).handle_session(self.session)
		self.session.turns.append(Turn("assistant", record(2)))
		self.signal(now=evening + timedelta(minutes=20))
		self.runner(evening + timedelta(minutes=20)).handle_session(self.session)
		self.session.turns.append(Turn("assistant", record(3)))
		self.session.last = evening + timedelta(minutes=22)
		self.runner(evening + timedelta(minutes=23)).handle_session(self.session)
		self.assertIn(record(2), self.path().read_text())
		self.assertNotIn(record(3), self.path().read_text())

	def test_no_block_uses_only_existing_company_routing(self):
		local = config.Plan("local", "ollama", "", "personal", ("company",), "acme", None)
		remote = config.Plan("remote", "codex", "", "personal", ("company", "personal"), None, None)
		plans = config.Plans({"local": local, "remote": remote}, {"company": ("local",), "personal": ("remote",)})
		self.session.turns[-1] = Turn("assistant", "일반 답변")
		self.signal()
		with mock.patch("collector.summarize.call", return_value="- 기존 요약") as call:
			self.runner(plans=plans).handle_session(self.session)
		self.assertEqual(call.call_args.args[0].id, "local")
		self.assertEqual(self.ledger.get(self.session.key)["plan"], "local")
		self.assertIn("기존 요약", self.path().read_text())

	def test_failed_write_unclassified_and_dry_run_keep_signals(self):
		self.signal()
		self.runner(dry_run=True).handle_session(self.session)
		self.assertIsNone(self.ledger.get(self.session.key))
		self.assertFalse(self.path().exists())
		with mock.patch.object(vault, "upsert_block", side_effect=OSError("합성 실패")):
			with self.assertRaises(OSError):
				self.runner().handle_session(self.session)
		self.session.cwd = "/w/xyz"
		self.runner().handle_session(self.session)
		self.assertEqual(len(hooks.read_pending()), 1)
		self.assertIsNone(self.ledger.get(self.session.key))

	def test_multiple_verbatim_blocks_do_not_split_project_insertion(self):
		self.signal()
		self.runner().handle_session(self.session)
		self.session.sid = "s02"
		self.signal()
		self.runner().handle_session(self.session)
		for sid in ("s01", "s02"):
			self.assertIn(record(), vault.block_body(self.path(), f"claude:{sid}"))

	def test_readers_find_old_pending_sessions_and_changed_long_codex_session(self):
		old = self.now - timedelta(days=40)
		claude = self.root / "claude"
		codex = self.root / "codex"
		gemini = self.root / "gemini"
		claude_path = claude / "acme/s01.jsonl"
		codex_path = codex / old.strftime("%Y/%m/%d") / "rollout-s01.jsonl"
		gemini_path = gemini / "tmp/acme/chats/session-s01.json"
		for path in (claude_path, codex_path, gemini_path):
			path.parent.mkdir(parents=True)
		claude_path.write_text("\n".join(json.dumps({"type": role, "entrypoint": "cli", "sessionId": "s01",
			"cwd": "/w/acme", "timestamp": old.isoformat(), "message": {"content": text}})
			for role, text in (("user", "질문"), ("assistant", record()))))
		meta = {"type": "session_meta", "timestamp": old.isoformat(),
			"payload": {"id": "s01", "cwd": "/w/acme", "timestamp": old.isoformat()}}
		messages = [{"type": "response_item", "timestamp": old.isoformat(),
			"payload": {"type": "message", "role": role, "channel": "final", "content": [{"text": text}]}}
			for role, text in (("user", "질문"), ("assistant", record()))]
		codex_path.write_text("\n".join(json.dumps(row) for row in [meta, *messages]))
		gemini_path.write_text(json.dumps({"sessionId": "s01", "startTime": old.isoformat(),
			"lastUpdated": old.isoformat(), "messages": [{"type": "user", "content": "질문"},
			{"type": "gemini", "content": record()}]}))
		(gemini / "projects.json").write_text(json.dumps({"projects": {"/w/acme": "acme"}}))
		for path in (claude_path, codex_path, gemini_path):
			os.utime(path, (old.timestamp(),) * 2)
		with mock.patch.object(sessions, "CLAUDE_ROOT", claude), mock.patch.object(sessions, "CODEX_ROOT", codex), \
				mock.patch.object(sessions_other, "GEMINI_HOME", gemini), mock.patch.object(pipeline, "read_agy", return_value=([], [])):
			for tool in ("claude", "codex", "gemini"):
				self.session.tool = tool
				self.signal(tool, "SessionEnd" if tool == "claude" else "Stop" if tool == "codex" else "AfterAgent",
					now=self.now - timedelta(hours=1))
			self.runner().run(self.now.date() - timedelta(days=2))
			self.assertFalse(hooks.read_pending())
			for tool in ("claude", "codex", "gemini"):
				self.assertEqual(self.ledger.get(f"{tool}:s01")["plan"], work_records.PLAN)
			messages[-1]["timestamp"] = self.now.isoformat()
			codex_path.write_text("\n".join(json.dumps(row) for row in [meta, *messages]))
			out, _ = pipeline.collect(self.now.date())
			self.assertEqual([session.key for session in out], ["codex:s01"])


if __name__ == "__main__":
	unittest.main()
