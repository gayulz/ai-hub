"""소켓·브라우저 없이 원시 HTTP로 쓰기 창구의 모든 차단 조건을 확인한다."""

import hmac
import http.client
import io
import json
import secrets
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from office import actions, server
from test_office_actions import ActionCase


class OfficeActionHTTPTest(ActionCase):
	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token=secrets.token_urlsafe(32))

	def request(self, payload=None, *, headers=None, body=None, method="POST", path="/api/action", extra=()):
		body = json.dumps(payload if payload is not None else {"action": "c4_apply", "id": self.bid}).encode() if body is None else body
		values = {"Host": "127.0.0.1:8790", "Origin": "http://127.0.0.1:8790", "Content-Type": "application/json",
			"Content-Length": str(len(body)), "X-Office-Token": self.httpd.office_token, **(headers or {})}
		raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in [*values.items(), *extra] if value is not None)
		class MemorySocket:
			def __init__(inner, data):
				inner.data, inner.output = data, io.BytesIO()
			def makefile(inner, *args):
				return io.BytesIO(inner.data)
			def sendall(inner, data):
				inner.output.write(data)
		socket = MemorySocket(raw.encode("latin-1") + b"\r\n" + body)
		server.Handler(socket, ("127.0.0.1", 1), self.httpd)
		response = http.client.HTTPResponse(MemorySocket(socket.output.getvalue()), method=method)
		response.begin()
		status, response_headers, content = response.status, dict(response.getheaders()), response.read()
		self.assertEqual(response_headers["Cache-Control"], "no-store")
		self.assertEqual(response_headers["X-Content-Type-Options"], "nosniff")
		self.assertIn("default-src 'none'", response_headers["Content-Security-Policy"])
		self.assertFalse(any(key.lower().startswith("access-control-") for key in response_headers))
		return status, response_headers, content

	def test_session_token_is_generated_once_per_server_and_only_returned_uncached(self):
		before = self.snapshot()
		with mock.patch.object(server, "ThreadingHTTPServer") as constructor, \
				mock.patch.object(server.secrets, "token_urlsafe", return_value=self.httpd.office_token) as generate:
			instance = server.create_server(8790)
			self.assertEqual(instance.office_token, self.httpd.office_token)
			constructor.assert_called_once_with(("127.0.0.1", 8790), server.Handler)
			generate.assert_called_once_with(32)
		status, _, body = self.request(method="GET", path="/api/session", headers={"Origin": None})
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body), {"token": self.httpd.office_token})
		self.assertEqual(before, self.snapshot())

	def test_origin_token_json_size_and_host_guards_never_run_commands(self):
		cases = [(403, {"Origin": None}), (403, {"Origin": "http://acme.invalid"}), (403, {"Origin": "null"}),
			(403, {"Origin": "https://127.0.0.1:8790"}), (403, {"Origin": "http://localhost:8791"}),
			(403, {"Origin": "http://127.0.0.1:8790/"}), (403, {"X-Office-Token": None}),
			(403, {"X-Office-Token": "xyz"}), (403, {"X-Office-Token": "\xff"}),
			(415, {"Content-Type": None}), (415, {"Content-Type": "text/plain"}),
			(415, {"Content-Type": "application/x-www-form-urlencoded"}), (413, {"Content-Length": "16385"}),
			(413, {"Content-Length": "9" * 5000}), (400, {"Content-Length": "-1"}),
			(400, {"Content-Length": None}), (400, {"Transfer-Encoding": "chunked"}), (403, {"Host": "acme.invalid"})]
		with mock.patch.object(actions.subprocess, "run") as run:
			for expected, headers in cases:
				with self.subTest(headers=str(headers)[:100]):
					self.assertEqual(self.request(headers=headers)[0], expected)
			self.assertEqual(self.request(body=b" " * 16385)[0], 413)
			run.assert_not_called()
		self.assertFalse((self.hub / "logs/office-actions.jsonl").exists())

	def test_duplicate_security_headers_are_rejected(self):
		for name, value, expected in (("Host", "127.0.0.1:8790", 403), ("Origin", "http://127.0.0.1:8790", 403),
				("X-Office-Token", self.httpd.office_token, 403), ("Content-Type", "application/json", 415), ("Content-Length", "1", 400)):
			with self.subTest(name=name), mock.patch.object(actions.subprocess, "run") as run:
				self.assertEqual(self.request(extra=[(name, value)])[0], expected)
				run.assert_not_called()

	def test_unknown_action_invalid_id_absent_item_and_malformed_json_never_run_commands(self):
		cases = [(400, {"action": "push", "id": "xyz"}), (400, {"action": "dev_draft", "id": "xyz"}),
			(400, {"action": "c4_apply", "id": "../xyz"}), (400, {"action": "run_apply", "id": "xyz"}),
			(400, {"action": "r1_apply", "id": "xyz/abc"}), (400, []), (400, {"action": "c4_apply"}),
			(409, {"action": "c4_apply", "id": "c4-" + "b" * 16}),
			(409, {"action": "run_apply", "id": "2026-10-24-999"}), (409, {"action": "r1_apply", "id": "xyz"})]
		with mock.patch.object(actions.subprocess, "run") as run:
			for expected, payload in cases:
				with self.subTest(payload=payload):
					self.assertEqual(self.request(payload)[0], expected)
			for body in (b"{", b"\xff", b"[" * 1500, b"null"):
				self.assertEqual(self.request(body=body)[0], 400)
			run.assert_not_called()

	def test_other_post_paths_and_methods_are_405_with_no_cors(self):
		with mock.patch.object(actions.subprocess, "run") as run:
			for path in ("/", "/api/state", "/api/session", "/api/action?xyz", "//api/action", "/api/action/", "http://127.0.0.1:8790/api/action"):
				with self.subTest(path=path):
					status, headers, _ = self.request(path=path)
					self.assertEqual((status, headers["Allow"]), (405, "GET"))
			for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "HEAD", "ACME"):
				for path, allow in (("/api/action", "GET, POST"), ("/api/state", "GET")):
					with self.subTest(method=method, path=path):
						status, headers, _ = self.request(method=method, path=path)
						self.assertEqual((status, headers["Allow"]), (405, allow))
			self.assertEqual(self.request(method="OPTIONS", headers={"Host": "acme.invalid"})[0], 403)
			run.assert_not_called()

	def test_exact_local_origins_size_boundary_and_constant_time_token_check(self):
		body = json.dumps({"action": "c4_apply", "id": self.bid}).encode()
		body += b" " * (16384 - len(body))
		for origin in ("http://127.0.0.1:8790", "http://localhost:8790"):
			with self.subTest(origin=origin), mock.patch.object(actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "xyz")) as run, \
					mock.patch.object(server.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
				status, _, output = self.request(body=body, headers={"Origin": origin, "Content-Type": "application/json; charset=utf-8"})
				self.assertEqual(status, 200)
				self.assertEqual(set(json.loads(output)), {"ok", "message", "output", "restart_needed"})
				run.assert_called_once()
				compare.assert_called_once()
		log = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assertNotIn(self.httpd.office_token, log)
		self.assertNotIn('"X-Office-Token"', log)
		self.assertNotIn(body.decode().strip(), log)

	def test_concurrent_posts_execute_once_and_second_returns_conflict(self):
		started, release = threading.Event(), threading.Event()
		def wait(*args, **kwargs):
			started.set()
			if not release.wait(5):
				raise RuntimeError("가짜 명령 대기 실패")
			return subprocess.CompletedProcess([], 0, "xyz")
		with mock.patch.object(actions.subprocess, "run", side_effect=wait) as run, ThreadPoolExecutor() as pool:
			first = pool.submit(self.request)
			try:
				self.assertTrue(started.wait(5))
				status, _, body = self.request()
				self.assertEqual((status, json.loads(body)["message"]), (409, "이미 실행 중"))
			finally:
				release.set()
			self.assertEqual(first.result(timeout=5)[0], 200)
			run.assert_called_once()

	def test_dev_text_payload_accepts_two_thousand_korean_characters_and_only_exact_keys(self):
		with mock.patch.object(actions.drafts, "execute", return_value=(200, actions.result(True, "합성 초안"))) as execute:
			for action, identity in (("dev_draft", "backend"), ("dev_redraft", "d-20261024-123456-abcdef01")):
				payload = {"action": action, "id": identity, "text": "가" * 2000}
				if action == "dev_draft":
					payload["target"] = "ai-hub"
					payload["origin"] = 11
				self.assertEqual(self.request(payload)[0], 200)
				self.assertEqual(execute.call_args.args[3], payload["text"])
				self.assertEqual(self.request({**payload, "text": "가" * 2001})[0], 400)
				self.assertEqual(self.request({**payload, "extra": "xyz"})[0], 400)
				if action == "dev_draft":
					for origin in (None, "11", True, 999):
						self.assertEqual(self.request({**payload, "origin": origin})[0], 400)
			self.assertEqual(execute.call_count, 2)
