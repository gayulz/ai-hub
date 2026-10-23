"""Gemini JSON·JSONL과 gzip 보관함을 합성 자료로 확인한다."""

import json
import os
from datetime import date

from collector import backfill_sessions, config, sessions_other
from collector.sessions import parse_time
from test_backfill_support import BackfillCase


class GeminiJsonlTest(BackfillCase):
	def setUp(self):
		super().setUp()
		self.gemini = sessions_other.GEMINI_HOME
		self.gemini.mkdir(parents=True)
		(self.gemini / "projects.json").write_text(
			json.dumps({"projects": {str(self.repo): "acme"}}), encoding="utf-8")

	def data(self, sid="g01", **fields):
		return {"sessionId": sid, "projectHash": "xyz", "kind": "main",
			"startTime": "2026-08-01T01:00:00Z", "lastUpdated": "2026-08-01T01:10:00Z",
			"messages": [{"type": "user", "content": "합성 질문"},
				{"type": "gemini", "content": "합성 완료 답"}], **fields}

	def jsonl(self, data, extra=()):
		header = {key: value for key, value in data.items() if key != "messages"}
		return "\n".join(json.dumps(row, ensure_ascii=False)
			for row in [header, *data["messages"], *extra])

	def live(self, filename, text):
		path = self.gemini / "tmp/acme/chats" / filename
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
		os.utime(path, (self.clock.timestamp(), self.clock.timestamp()))
		return path

	def test_live_jsonl_keeps_only_message_text_and_project_mapping(self):
		messages = [{"type": "user", "content": "첫 질문"},
			{"type": "gemini", "content": "첫 답", "thoughts": [{"text": "생각 덤프"}],
				"toolCalls": [{"name": "xyz", "result": "도구 덤프"}],
				"tokens": {"input": 123}, "model": "xyz"},
			{"type": "user", "content": [{"text": "둘째 질문 앞"}, {"text": "둘째 질문 뒤"},
				{"image": "이미지 덤프"}, {"text": 123}, "잘못된 조각"]},
			{"type": "gemini", "content": [{"text": "완료 답 앞"}, {"text": "완료 답 뒤"}]},
			{"type": "info", "content": "진행 알림"},
			{"type": "error", "content": "오류 덤프"}]
		self.live("session-g01.jsonl", self.jsonl(self.data(
			cwd=str(self.root / "xyz"), messages=messages)))
		found = sessions_other.read_gemini(self.clock)
		self.assertEqual(len(found), 1)
		session = found[0]
		self.assertEqual((session.tool, session.sid, session.cwd), ("gemini", "g01", str(self.repo)))
		self.assertEqual([(turn.role, turn.text) for turn in session.turns], [
			("user", "첫 질문"), ("assistant", "첫 답"),
			("user", "둘째 질문 앞\n둘째 질문 뒤"), ("assistant", "완료 답 앞\n완료 답 뒤")])

	def test_broken_jsonl_lines_keep_messages_and_last_set_in_file_order(self):
		data = self.data()
		text = self.jsonl(data, [{"$set": {"lastUpdated": "2026-08-01T01:40:00Z"}},
			{"$set": {"lastUpdated": "2026-08-01T01:20:00Z"}}])
		lines = text.splitlines()
		lines.insert(2, "{깨진 중간 줄")
		text = "\n".join(lines) + '\n{"type":'
		self.live("session-g01.jsonl", text)
		path = self.gzip("gemini/acme/chats/session-g01.jsonl.gz", text)
		for session in (sessions_other.read_gemini(self.clock)[0],
				backfill_sessions.read(path, "gemini", {"acme": str(self.repo)})[0]):
			with self.subTest(tool=session.tool, cwds=session.cwds):
				self.assertEqual(session.started, parse_time(data["startTime"]))
				self.assertEqual(session.last, parse_time("2026-08-01T01:20:00Z"))
				self.assertEqual([turn.text for turn in session.turns], ["합성 질문", "합성 완료 답"])

	def test_jsonl_without_last_updated_falls_back_to_start_time(self):
		data = self.data()
		del data["lastUpdated"]
		text = self.jsonl(data)
		self.live("session-g01.jsonl", text)
		path = self.gzip("gemini/acme/chats/session-g01.jsonl.gz", text)
		for session in (sessions_other.read_gemini(self.clock)[0],
				backfill_sessions.read(path, "gemini", {"acme": str(self.repo)})[0]):
			self.assertEqual(session.last, session.started)

	def test_jsonl_excludes_injected_empty_and_subagent_sessions(self):
		cases = {"injected": {"messages": [{"type": "user", "content": [
			{"text": "<system-reminder>자동 안내"}]}, {"type": "gemini", "content": "자동 답"}]},
			"empty": {"messages": [{"type": "user", "content": " \n\t"},
				{"type": "gemini", "content": "답만 있음"}]},
			"subagent": {"kind": "subagent"}}
		for sid, fields in cases.items():
			text = self.jsonl(self.data(sid=sid, **fields))
			self.live("session-" + sid + ".jsonl", text)
			path = self.gzip("gemini/acme/chats/session-" + sid + ".jsonl.gz", text)
			with self.subTest(sid=sid):
				self.assertEqual(backfill_sessions.read(path, "gemini", {"acme": str(self.repo)}), [])
		self.assertEqual(sessions_other.read_gemini(self.clock), [])

	def test_live_deduplicates_both_formats_by_latest_session_update(self):
		for latest in ("json", "jsonl"):
			sid = "shared-" + latest
			for kind in ("json", "jsonl"):
				data = self.data(sid=sid,
					lastUpdated="2026-08-01T01:30:00Z" if kind == latest else "2026-08-01T01:10:00Z",
					messages=[{"type": "user", "content": "합성 질문"},
						{"type": "gemini", "content": latest + " 최신" if kind == latest else "옛 답"}])
				self.live("session-" + sid + "." + kind,
					json.dumps(data) if kind == "json" else self.jsonl(data))
		for kind in ("json", "jsonl"):
			data = self.data(sid="separate-" + kind)
			self.live("session-separate." + kind,
				json.dumps(data) if kind == "json" else self.jsonl(data))
		found = sessions_other.read_gemini(self.clock)
		self.assertEqual(len(found), 4)
		by_sid = {session.sid: session for session in found}
		self.assertEqual(set(by_sid), {"shared-json", "shared-jsonl", "separate-json", "separate-jsonl"})
		for latest in ("json", "jsonl"):
			self.assertEqual(by_sid["shared-" + latest].turns[-1].text, latest + " 최신")

	def test_archive_json_and_jsonl_keep_cwd_precedence_and_fallback_id(self):
		for fields, expected in (({"cwd": str(self.repo), "projectPath": str(self.root / "xyz")}, str(self.repo)),
				({"projectPath": str(self.root / "xyz")}, str(self.root / "xyz")), ({}, str(self.repo))):
			data = self.data(**fields)
			del data["sessionId"]
			found = []
			for kind in ("json", "jsonl"):
				path = self.gzip("gemini/acme/chats/session-g01." + kind + ".gz",
					json.dumps(data) if kind == "json" else self.jsonl(data))
				with self.subTest(fields=fields, kind=kind):
					session = backfill_sessions.read(path, "gemini", {"acme": str(self.repo)})[0]
					self.assertEqual(session.key, "gemini:session-g01")
					self.assertEqual((session.cwd, session.cwds), (expected, [expected]))
					found.append(session)
			self.assertEqual(found[0], found[1])

	def test_archive_jsonl_preserves_incomplete_tool_call_turn(self):
		data = self.data(messages=[{"type": "user", "content": [{"text": "합성 질문"}]},
			{"type": "gemini", "content": "도구 호출 중", "toolCalls": [{"name": "xyz", "result": "도구 덤프"}],
				"thoughts": [{"text": "생각 덤프"}], "tokens": {"input": 123}, "model": "xyz"},
			{"type": "info", "content": "진행 알림"}, {"type": "error", "content": "오류 덤프"},
			{"type": "gemini", "content": [{"text": "완료 답 앞"}, {"text": "완료 답 뒤"}]}])
		path = self.gzip("gemini/acme/chats/session-g01.jsonl.gz", self.jsonl(data))
		session = backfill_sessions.read(path, "gemini", {"acme": str(self.repo)})[0]
		self.assertEqual([(turn.role, turn.text, turn.complete) for turn in session.turns], [
			("user", "합성 질문", True), ("assistant", "도구 호출 중", False),
			("assistant", "완료 답 앞\n완료 답 뒤", True)])

	def test_inventory_creates_jsonl_gzip_task_without_format_hold(self):
		path = self.gzip("gemini/acme/chats/session-g01.jsonl.gz", self.jsonl(self.data()))
		sources = config.load_sources()
		result = backfill_sessions.inventory(sources.repos[0], sources, date(2026, 7, 22), self.clock)
		self.assertEqual((result["warnings"], result["deferred"], result["unclassified"]), ([], [], 0))
		self.assertEqual(len(result["tasks"]), 1)
		task = result["tasks"][0]
		self.assertEqual((task["key"], task["reason"], task["parts"]), ("gemini:g01", "", 1))
		self.assertEqual(task["archive"], path.relative_to(self.hub / "archive").as_posix())
		self.ai.assert_not_called()
