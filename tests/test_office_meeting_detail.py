"""회의 GET은 합성 기록만 읽고 응답 칸·마스킹·보안 경계를 유지한다."""

import copy
import json
from types import SimpleNamespace
from unittest import mock

from office import meetings, server
from orchestrator import meeting_record
from orchestrator.meeting import MAX_CALLS, MAX_SECONDS
from test_office_meetings import MID, MeetingCase, meeting_state, write_meeting
import test_office_server as server_tests


class OfficeMeetingDetailTest(MeetingCase):
	raw_request = server_tests.OfficeServerTest.raw_request
	request = server_tests.OfficeServerTest.request

	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token="xyz-" + "session")

	def snapshot(self):
		return {str(p.relative_to(self.hub)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
			for p in self.hub.rglob("*") if p.is_file() and not p.is_symlink()}

	def get(self, identity=MID):
		return self.request("/api/meeting?id=" + identity)

	def test_fields_full_agenda_speech_summaries_limits_and_read_only_permissions(self):
		state = meeting_state("completed")
		state.update(phase="done", agenda="합성 안건 " * 80, active_seconds=12.5,
			calls=[{"raw": "숨길 원응답"}], failure="", canonical_rules="숨길 정본")
		state["rounds"] = {"1": [{"role": "backend", "method": "방법 " + "가" * 400, "risks": ["숨길 위험"],
			"changes": ["숨길 파일 후보"], "raw": "숨길 원응답"}], "2": [{"role": "qa",
			"agree": [{"role": "backend", "text": "동의 요지"}], "disagree": [{"role": "backend", "text": "반대 요지"}],
			"supplement": [{"role": "backend", "text": "보완 요지"}]}]}
		state["summary"] = {key: [{"text": "합성 " + key, "refs": ["숨길 출처"]}]
			for key in ("conclusion", "todos", "open_questions", "decisions")}
		state["absent"] = [{"role": "design", "phase": "round1", "reason": "시간 초과", "detail": "숨길 원응답"}]
		path = write_meeting(self.hub, state)
		path.chmod(0o644)
		before = self.snapshot()
		with mock.patch.object(meeting_record.Store, "locked", side_effect=AssertionError("쓰기 금지")):
			status, headers, body = self.get()
		self.assertEqual(status, 200)
		value = json.loads(body)
		self.assertEqual(set(value), {"id", "status", "phase", "agenda", "target", "participants", "absent", "rounds", "summary",
			"calls", "calls_max", "active_seconds", "seconds_max", "failure", "updated"})
		self.assertEqual(value["agenda"], state["agenda"])
		self.assertEqual(value["participants"][0], {"role": "backend", "name": "백엔드", "reason": "합성 서버 검토"})
		self.assertEqual(value["absent"], [{"role": "design", "name": "디자인", "phase": "round1", "reason": "시간 초과"}])
		self.assertEqual(len(value["rounds"]["1"][0]["text"]), 300)
		self.assertEqual(value["rounds"]["2"][0]["text"], "동의: 동의 요지\n반대: 반대 요지\n보완: 보완 요지")
		self.assertEqual(value["summary"], {key: ["합성 " + source] for key, source in
			(("conclusion", "conclusion"), ("todo", "todos"), ("open_questions", "open_questions"), ("decisions", "decisions"))})
		self.assertEqual((value["calls"], value["calls_max"], value["seconds_max"], value["active_seconds"]), (1, MAX_CALLS, MAX_SECONDS, 12.5))
		self.assertTrue(value["updated"])
		self.assertEqual(value["failure"], "")
		self.assertNotIn("숨길", body.decode())
		self.assertEqual(headers["Cache-Control"], "no-store")
		self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(headers["Content-Security-Policy"], server.CSP)
		self.assertFalse(any(k.lower().startswith("access-control-") for k in headers))
		self.assertEqual(before, self.snapshot())

	def test_every_text_field_is_masked_before_truncation_and_raw_failure_is_hidden(self):
		secret = "ghp_" + "a" * 36
		sensitive = "acme " + secret + " " + "p01" + "@" + "acme.invalid /acme/private ./xyz/file.py C:\\xyz\\private https://acme.invalid/private"
		state = meeting_state("failed")
		state.update(phase="round2", agenda=sensitive, target=sensitive, failure=sensitive + "\n원응답")
		for p in state["participants"]:
			p["reason"] = sensitive
		state["absent"] = [{"role": "qa", "phase": sensitive, "reason": sensitive, "detail": "숨길 오류 원문"}]
		state["rounds"] = {"1": [{"role": "backend", "method": sensitive}], "2": [{"role": "qa",
			"agree": [{"text": sensitive + "\n줄 " + "가" * 350}], "disagree": [], "supplement": []}]}
		state["summary"] = {key: [{"text": sensitive, "refs": [sensitive]}]
			for key in ("conclusion", "todos", "open_questions", "decisions")}
		write_meeting(self.hub, state)
		status, _, body = self.get()
		self.assertEqual(status, 200)
		for raw in ("acme", "xyz", "@", secret, "private", "file.py", "원응답", "숨길"):
			self.assertNotIn(raw, body.decode())
		value = json.loads(body)
		self.assertIn("[경로 생략]", value["rounds"]["1"][0]["text"])
		self.assertTrue(all(len(line) <= 300 for line in value["rounds"]["2"][0]["text"].splitlines()))
		self.assertNotIn("\n", value["failure"])
		self.assertRegex(value["failure"], "[가-힣]")

	def test_invalid_ids_missing_records_and_methods_keep_host_boundary(self):
		before = self.snapshot()
		for path in ("/api/meeting", "/api/meeting?", "/api/meeting?id=", "/api/meeting?id=../xyz", "/api/meeting?id=" + MID.upper(),
				"/api/meeting?id=" + MID + "0", "/api/meeting?id=" + MID + "&id=" + MID, "/api/meeting?id=" + MID + "&text=xyz"):
			with self.subTest(path=path):
				self.assertEqual(self.request(path)[0], 400)
		self.assertEqual(self.get()[0], 404)
		write_meeting(self.hub, meeting_state())
		self.assertEqual(self.get("m-" + "b" * 32)[0], 404)
		self.assertEqual(self.request("/api/meeting?id=" + MID, host="acme.invalid")[0], 403)
		for method in ("POST", "PUT", "HEAD", "DELETE"):
			status, headers, _ = self.request("/api/meeting?id=" + MID, method=method)
			self.assertEqual(status, 405)
			self.assertEqual(headers["Allow"], "GET")
		self.assertTrue(before.items() <= self.snapshot().items())

	def test_unreadable_corrupt_symlink_and_missing_json_are_503_without_writes(self):
		state = meeting_state("running")
		path = write_meeting(self.hub, state)
		for error in (PermissionError, ValueError, TypeError, RecursionError, FileNotFoundError):
			with mock.patch.object(meeting_record.Store, "read", side_effect=error("숨길 원문")):
				status, _, body = self.get()
				self.assertEqual(status, 503)
				self.assertNotIn("숨길", body.decode())
		for content in ("{", "{}", "[" * 1500):
			path.write_text(content)
			before = self.snapshot()
			self.assertEqual(self.get()[0], 503)
			self.assertEqual(before, self.snapshot())
		path.unlink()
		self.assertEqual(self.get()[0], 503)
		path.symlink_to(self.hub / "plans.toml")
		self.assertEqual(self.get()[0], 503)
		path.unlink()
		write_meeting(self.hub, state)
		(self.hub / "meetings" / ("m-" + "c" * 32)).symlink_to(path.parent, target_is_directory=True)
		self.assertEqual(self.get("m-" + "c" * 32)[0], 503)
		for field, bad in (("phase", "xyz"), ("participants", None), ("rounds", {}), ("summary", {})):
			value = copy.deepcopy(state)
			value[field] = bad
			write_meeting(self.hub, value)
			self.assertEqual(self.get()[0], 503)

	def test_unfinished_meeting_returns_null_summary(self):
		write_meeting(self.hub, meeting_state("running"))
		self.assertIsNone(json.loads(self.get()[2])["summary"])
