"""대화 조회·쓰기 요청의 HTTP 보안 경계를 소켓 없이 검사한다."""

import json
from types import SimpleNamespace
from unittest import mock

from office import actions, chats, server
from orchestrator import chat_record
import test_office_actions_http as action_http
import test_office_server as server_http
from test_office_chats import CARD, CHAT, ChatCase, chat_state, write_chat


class OfficeChatsHTTPTest(ChatCase):
	raw_request = server_http.OfficeServerTest.raw_request
	request = server_http.OfficeServerTest.request
	post = action_http.OfficeActionHTTPTest.request

	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token="synthetic-" + "session")

	def get(self, identity=CHAT):
		return self.request("/api/chat?id=" + identity)

	def test_detail_public_fields_full_turns_and_read_only_mode(self):
		state = chat_state(card="answered")
		state.update(calls=3, seconds=1.5, dev_draft="합성 초안 " + "가" * 1990, needs_user="합성 확인 필요")
		state["turns"] = [{"at": state["created"], "role": role, "text": text, "source": "office", "kind": kind}
			for role, text, kind in (("assistant", "가" * 6000, "message"), ("user", "확인한 합성 요약", "answer_summary"))]
		path = write_chat(self.hub, state)
		path.chmod(0o644)
		before = self.snapshot()
		status, headers, body = self.get()
		self.assertEqual(status, 200)
		value = json.loads(body)
		self.assertEqual(set(value), {"id", "status", "target", "plan", "tool", "turns", "card", "dev_draft", "needs_user", "calls", "seconds", "failure", "failure_reason", "updated"})
		self.assertEqual(value["turns"], state["turns"])
		self.assertEqual(set(value["card"]), {"id", "turn", "status", "questions", "summary"})
		self.assertEqual(value["card"]["id"], CARD)
		self.assertEqual((value["calls"], value["seconds"]), (3, 1.5))
		self.assertLessEqual(len(value["dev_draft"]), 2000)
		self.assertNotIn(state["session"], body.decode())
		self.assertNotIn('"session"', body.decode())
		self.assertNotIn('"answers"', body.decode())
		self.assertEqual(headers["Cache-Control"], "no-store")
		self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(headers["Content-Security-Policy"], server.CSP)
		self.assertFalse(any(k.lower().startswith("access-control-") for k in headers))
		self.assertEqual(before, self.snapshot())

	def test_all_detail_text_masked_and_paths_removed_before_truncation_failure_is_fixed(self):
		secret = "ghp_" + "a" * 36
		sensitive = "acme " + secret + " " + "p01" + "@" + "acme.invalid /tmp/private ./lib/file.py office/other.py C:\\data\\private https://other.invalid/private"
		state = chat_state(card="answered")
		state.update(target="xyz", plan="acme", dev_draft=sensitive, needs_user=sensitive, failure="숨길 오류 원문")
		state["turns"] = [{"at": state["created"], "role": "assistant", "text": sensitive, "source": "office", "kind": "message"}]
		q = state["cards"][0]["questions"][0]
		q.update(text=sensitive, reason=sensitive)
		for option in q["options"]:
			for key in ("label", "difference", "reply"):
				option[key] = sensitive
		state["cards"][0]["summary"] = sensitive
		write_chat(self.hub, state)
		status, _, body = self.get()
		self.assertEqual(status, 200)
		for raw in ("acme", "xyz", "@", secret, "private", "원문", "hidden-session"):
			self.assertNotIn(raw, body.decode())
		self.assertIn("[경로 생략]", body.decode())
		self.assertEqual(json.loads(body)["failure"], "대화를 마치지 못했습니다. 상태를 확인해 주세요.")
		self.assertEqual(json.loads(body)["failure_reason"], "")  # 엔진이 정한 분류 문구가 아니면 이유를 보내지 않는다

	def test_bad_query_ids_missing_records_host_and_methods(self):
		for path in ("/api/chat", "/api/chat?", "/api/chat?id=", "/api/chat?id=../other", "/api/chat?id=" + CHAT.upper(),
				"/api/chat?id=" + CHAT + "0", "/api/chat?id=" + CHAT + "&id=" + CHAT, "/api/chat?id=" + CHAT + "&text=other"):
			self.assertEqual(self.request(path)[0], 400)
		self.assertEqual(self.get()[0], 404)
		write_chat(self.hub, chat_state())
		self.assertEqual(self.get("c-" + "c" * 32)[0], 404)
		self.assertEqual(self.request("/api/chat?id=" + CHAT, host="other.invalid")[0], 403)
		for method in ("POST", "PUT", "HEAD", "DELETE"):
			status, headers, _ = self.request("/api/chat?id=" + CHAT, method=method)
			self.assertEqual((status, headers["Allow"]), (405, "GET"))
		self.assertIsNone(json.loads(self.get()[2])["card"])

	def test_corruption_read_failures_links_and_raw_response_fields_return_fixed_503(self):
		path = write_chat(self.hub, chat_state())
		for error in (PermissionError, ValueError, TypeError, RecursionError, FileNotFoundError):
			with mock.patch.object(chat_record.Store, "read", side_effect=error("숨길 원문")):
				status, _, body = self.get()
				self.assertEqual(status, 503)
				self.assertEqual(json.loads(body), {"message": "대화 기록을 읽지 못했습니다. 잠시 뒤 다시 확인해 주세요."})
		for content in ("{", "{}", "[" * 1500, json.dumps({**chat_state(), "raw_response": "숨길 원응답"})):
			path.write_text(content)
			before = self.snapshot()
			self.assertEqual(self.get()[0], 503)
			self.assertEqual(before, self.snapshot())
		path.unlink()
		self.assertEqual(self.get()[0], 503)
		path.symlink_to(self.hub / "plans.toml")
		self.assertEqual(self.get()[0], 503)
		(self.hub / "chats" / ("c-" + "c" * 32)).symlink_to(path.parent, target_is_directory=True)
		self.assertEqual(self.get("c-" + "c" * 32)[0], 503)

	def test_all_five_actions_share_existing_local_and_remote_security_checks(self):
		host = "box.tail1234.ts.net:8790"
		(self.hub / "office.toml").write_text('[remote]\nhosts=["' + host + '"]\nlogins=["p01"]\n')
		payloads = (self.new, self.send, self.answer, self.confirm, self.close)
		with mock.patch.object(chats, "execute", return_value=(200, actions.result(True, "합성 대화 요청"))) as execute:
			for headers in ({}, {"Host": host, "Tailscale-User-Login": "p01", "Origin": "https://" + host}):
				for payload in payloads:
					status, _, _ = self.post(payload, headers=headers)
					self.assertEqual(status, 200)
					self.assertEqual(execute.call_args.args[1], payload)
					before = execute.call_count
					for expected, change in ((403, {"Origin": "https://other.invalid"}), (403, {"X-Office-Token": "wrong"}),
							(403, {"Host": "other.invalid"}), (415, {"Content-Type": "text/plain"})):
						self.assertEqual(self.post(payload, headers={**headers, **change})[0], expected)
					self.assertEqual(self.post({**payload, "extra": "other"}, headers=headers)[0], 400)
					self.assertEqual(self.post(payload, body=b" " * 16385, headers=headers)[0], 413)
					self.assertEqual(self.post(payload, body=b"{", headers=headers)[0], 400)
					self.assertEqual(execute.call_count, before)
			for login in (None, "other"):
				self.assertEqual(self.post(self.send, headers={"Host": host, "Tailscale-User-Login": login, "Origin": "https://" + host})[0], 403)
			for key, value, expected in (("Origin", "http://127.0.0.1:8790", 403), ("X-Office-Token", self.httpd.office_token, 403),
					("Content-Type", "application/json", 415), ("Content-Length", "1", 400)):
				self.assertEqual(self.post(self.send, extra=[(key, value)])[0], expected)

	def test_allowed_remote_get_uses_the_same_read_only_projection(self):
		host = "box.tail1234.ts.net:8790"
		(self.hub / "office.toml").write_text('[remote]\nhosts=["' + host + '"]\nlogins=["p01"]\n')
		write_chat(self.hub, chat_state())
		raw = f'GET /api/chat?id={CHAT} HTTP/1.1\r\nHost: {host}\r\nTailscale-User-Login: p01\r\n\r\n'.encode()
		status, _, body = self.raw_request(raw)
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body)["id"], CHAT)
		self.assertNotIn("hidden-session", body.decode())
