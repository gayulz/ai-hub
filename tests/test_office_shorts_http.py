"""소켓 없이 접수 HTTP·정적 파일·상태 연결과 기존 쓰기 경계를 확인한다."""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from office import server, state
from office.shorts_steps import STEPS
import test_office_server as helpers
from test_office_shorts import SID, ShortsCase


class OfficeShortsHTTPTest(ShortsCase):
	raw_request = helpers.OfficeServerTest.raw_request
	request = helpers.OfficeServerTest.request

	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token=server.secrets.token_urlsafe(32))

	def post(self, payload=None, headers=None):
		body = json.dumps(self.payload if payload is None else payload, ensure_ascii=False).encode()
		values = {"Host": "127.0.0.1:8790", "Origin": "http://127.0.0.1:8790", "Content-Type": "application/json",
			"Content-Length": str(len(body)), "X-Office-Token": self.httpd.office_token, **(headers or {})}
		raw = "POST /api/action HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in values.items() if value is not None)
		return self.raw_request(raw.encode() + b"\r\n" + body, "POST")

	def test_steps_without_session_exact_public_shape_and_static_scripts_200(self):
		code, headers, body = self.request("/api/shorts/steps")
		self.assertEqual(code, 200)
		self.assertIn("application/json", headers["Content-Type"])
		data = json.loads(body)
		self.assertEqual(set(data), {"steps", "source_presets", "save_presets"})
		self.assertEqual(data["steps"], STEPS)
		self.assertEqual([s["id"] for s in STEPS], ["purpose", "audience", "message", "facts", "hook", "cuts", "length", "ratio", "style", "sound", "caption", "platform", "link"])
		for step in data["steps"]:
			self.assertEqual(set(step), {"id", "label", "question", "chips", "proposal", "depends_on"})
			for chip in step["chips"]:
				self.assertEqual(set(chip), {"value", "difference"})
				self.assertTrue(chip["difference"])
		for group in (data["source_presets"], data["save_presets"]):
			self.assertTrue(all(set(p) == {"id", "label"} for p in group))
		self.assertNotIn(str(self.user), body.decode())
		self.assertFalse((self.hub / "run").exists())
		for name in ("shorts.js", "shorts_wizard.js"):
			code, headers, body = self.request("/" + name)
			self.assertEqual(code, 200)
			self.assertIn("text/javascript", headers["Content-Type"])
			self.assertTrue(body)
		for path in ("/shorts.py", "/shorts_steps.py", "/api/shorts", "/api/shorts/steps?path=p01"):
			self.assertEqual(self.request(path)[0], 404)

	def test_intake_origin_token_json_host_guards_and_largest_text_payload(self):
		for expected, headers in ((403, {"Origin": None}), (403, {"Origin": "http://acme.invalid"}),
				(403, {"Host": "acme.invalid"}), (403, {"X-Office-Token": None}), (403, {"X-Office-Token": "xyz"}),
				(415, {"Content-Type": "text/plain"}), (400, {"Content-Length": "-1"}),
				(413, {"Content-Length": "9" * 100})):
			self.assertEqual(self.post(headers=headers)[0], expected)
		self.assertFalse((self.hub / "shorts").exists())
		payload = {**self.payload, "story": "가" * 8000,
			"conditions": [{"id": step["id"], "value": "나" * 300} for step in STEPS]}
		code, _, body = self.post(payload)
		self.assertEqual(code, 200, body)
		self.assertEqual(json.loads(body)["draft"]["id"], SID)
		self.assertEqual(self.post(payload)[0], 409)
		self.assertEqual(self.post({"action": "p01", "story": "가" * 6000})[0], 413)

	def test_build_state_includes_only_intake_summary(self):
		self.assertEqual(self.post()[0], 200)
		# 기존 상태 수집기는 모두 대역으로 두어 접수 파일만 읽는다.
		ledger = {"active": {}, "calls": {"status": "확인 불가", "items": []}, "batches": {}, "usage": {},
			"records": {"items": [], "status": "정상"}, "progress": {}, "followups": []}
		runs = {"status": "정상", "items": [], "activity": []}
		patches = [mock.patch.object(state, "read_plans", return_value={"status": "확인 불가", "limit": {}}),
			mock.patch.object(state.targets, "targets", return_value={}),
			mock.patch.object(state, "read_ledger", return_value=ledger),
			mock.patch.object(state.agents, "read_runs", return_value=runs),
			mock.patch.object(state, "read_decisions", return_value={"decisions": {}, "schedule": {"items": []}}),
			mock.patch.object(state, "read_jobs", return_value=[]),
			mock.patch.object(state.agents, "orchestration", return_value=[]),
			mock.patch.object(state.agents, "ollama", return_value={"id": "gemma", "status": "확인 불가"}),
			mock.patch.object(state.agents, "blog", return_value={"id": "blog", "status": "확인 불가"}),
			mock.patch.object(state.agents, "presence", return_value={"status": "확인 불가"}),
			mock.patch.object(state, "dashboard", return_value={}),
			mock.patch.object(state, "read_action_activity", return_value=[]),
			mock.patch.object(state.roles, "build_people", return_value=[]),
			mock.patch.object(state.roles, "build_engines", return_value=[]),
			mock.patch.object(state.roles, "rooms", return_value=[]),
			mock.patch.object(state, "build_schedule", return_value={}),
			mock.patch.object(state.meetings, "read_state", return_value={"questions": {"status": "정상", "items": []}}),
			mock.patch.object(state.chats, "snapshot", return_value=({"chats": {"status": "정상", "items": []}}, [])),
			mock.patch.object(state, "blog_dashboard", return_value="꺼짐"),
			mock.patch.object(state.telegram_bridge, "status", return_value={}),
			mock.patch.object(state, "read_r1", return_value={}),
			mock.patch.object(state.drafts, "read_drafts", return_value={})]
		with ExitStack() as stack:
			for patch in patches:
				stack.enter_context(patch)
			data = state.build_state()
		group = data["shorts"]
		self.assertEqual(group["status"], "정상")
		self.assertEqual(group["items"][0]["id"], SID)
		self.assertEqual(set(group["items"][0]), {"id", "created", "status", "story_preview"})
		self.assertNotIn(str(self.user), json.dumps(group))
