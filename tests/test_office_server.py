"""원시 HTTP 요청으로 경로·Host·메서드 제한을 소켓 없이 확인한다."""

import http.client
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from office import server
from office.__main__ import main
from test_office import OfficeCase


class OfficeServerTest(OfficeCase):
	def setUp(self):
		super().setUp()
		patch = mock.patch("socket.create_connection", side_effect=OSError("합성 연결 거부"))
		patch.start()
		self.addCleanup(patch.stop)
		self.httpd = SimpleNamespace(server_port=8790, office_token=server.secrets.token_urlsafe(32))

	def raw_request(self, raw, method="GET"):
		class MemorySocket:
			def __init__(inner, data):
				inner.data, inner.output = data, io.BytesIO()
			def makefile(inner, *args):
				return io.BytesIO(inner.data)
			def sendall(inner, data):
				inner.output.write(data)
		socket = MemorySocket(raw)
		server.Handler(socket, ("127.0.0.1", 1), self.httpd)
		response = http.client.HTTPResponse(MemorySocket(socket.output.getvalue()), method=method)
		response.begin()
		return response.status, dict(response.getheaders()), response.read()

	def request(self, path="/", method="GET", host=None):
		host = f"127.0.0.1:{self.httpd.server_port}" if host is None else host
		return self.raw_request(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode(), method)

	def test_serves_only_screen_script_state_and_binds_only_ipv4_loopback(self):
		with mock.patch.object(server, "ThreadingHTTPServer") as constructor:
			server.create_server(8790)
		constructor.assert_called_once_with(("127.0.0.1", 8790), server.Handler)
		status, headers, body = self.request()
		self.assertEqual(status, 200)
		self.assertIn("text/html", headers["Content-Type"])
		self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
		self.assertIn(b'/office.js', body)
		self.assertEqual(self.request("/office.js")[0], 200)
		for path, kind in (("/pixel-art.js", "text/javascript"), ("/meeting.js", "text/javascript"), ("/office.css", "text/css"), ("/scene.json", "application/json")):
			status, headers, _ = self.request(path)
			self.assertEqual(status, 200)
			self.assertIn(kind, headers["Content-Type"])
		status, headers, body = self.request("/api/state")
		self.assertEqual(status, 200)
		self.assertEqual(len(json.loads(body)["agents"]), 6)
		self.assertEqual(len(json.loads(body)["people"]), 23)
		self.assertEqual(len(json.loads(body)["engines"]), 8)
		self.assertNotIn("Access-Control-Allow-Origin", headers)
		self.assertEqual(headers["Cache-Control"], "no-store")
		self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
		for path in ("/index.html", "/api/state?xyz=1", "/api/state/", "//", "//api/state", "///office.js",
				"/../plans.toml", "/%2e%2e/ledger.db", "/favicon.ico", "http://127.0.0.1/"):
			with self.subTest(path=path):
				self.assertEqual(self.request(path)[0], 404)

	def test_localhost_with_exact_port_is_also_allowed(self):
		self.assertEqual(self.request(host=f"localhost:{self.httpd.server_port}")[0], 200)

	def test_csp_only_adds_two_blog_frame_origins_and_keeps_security_headers(self):
		expected = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
			"font-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
			"frame-src http://127.0.0.1:8765 http://localhost:8765")
		for path, method, host in (("/", "GET", None), ("/office.js", "GET", None),
				("/api/state", "GET", None), ("/xyz", "GET", None), ("/", "POST", None),
				("/api/action", "POST", None), ("/", "GET", "acme.invalid")):
			with self.subTest(path=path, method=method, host=host):
				_, headers, _ = self.request(path, method, host)
				self.assertEqual(headers["Content-Security-Policy"], expected)
				self.assertEqual(headers["Cache-Control"], "no-store")
				self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
				self.assertFalse(any(key.lower().startswith("access-control-") for key in headers))

	def test_missing_duplicate_external_and_wrong_port_hosts_are_forbidden(self):
		for host in ("acme.invalid", "127.0.0.1", "localhost", "127.0.0.1:1", "localhost:1", f"[::1]:{self.httpd.server_port}", ""):
			with self.subTest(host=host):
				self.assertEqual(self.request(host=host)[0], 403)
		for hosts in ([], [f"localhost:{self.httpd.server_port}"]*2):
			headers = ''.join(f'Host: {host}\r\n' for host in hosts)
			self.assertEqual(self.raw_request(f'GET / HTTP/1.1\r\n{headers}\r\n'.encode())[0], 403)

	def test_every_non_get_method_returns_405_without_collecting_or_writing(self):
		before = self.snapshot()
		for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT", "ACME"):
			with self.subTest(method=method):
				status, headers, body = self.request("/api/state", method=method)
				self.assertEqual(status, 405)
				self.assertEqual(headers["Allow"], "GET")
		self.assertEqual(before, self.snapshot())
		self.assertFalse((self.root / "commands.jsonl").exists())

	def test_static_screen_has_no_demo_controls_or_external_assets(self):
		html = (server.STATIC / "index.html").read_text()
		js = (server.STATIC / "office.js").read_text()
		for value in ("scenario", "연출 멈춤", "다시 보기", "이 시안으로 가", "예시 데이터", "실행 006"):
			self.assertNotIn(value, html + js)
		self.assertIn("setInterval(loadState,10000)", js)
		self.assertIn("fetch('/api/state'", js)
		self.assertIn('반영 전에 변경 내용을 확인합니다.', js)
		self.assertNotIn('navigator.clipboard', js)
		self.assertEqual(re.findall(r"https?://[^\s'\"<]+", html + js), ["http://127.0.0.1:8765"])
		self.assertIn("method:'POST'", js)
		self.assertIn("fetch('/api/action'", js)
		self.assertIn("fetch('/api/session'", js)
		for value in ('alert(', 'confirm(', 'localStorage', 'sessionStorage'):
			self.assertNotIn(value, js)
		for path in [*Path("office").glob("*.py"), *server.STATIC.iterdir()]:
			self.assertLessEqual(len(path.read_text().splitlines()), 500)

	def test_cli_rejects_invalid_ports(self):
		import contextlib
		import io
		for port in ("0", "65536", "-1"):
			with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
				main(["--port", port])

	def test_draft_detail_only_returns_pending_validated_card_without_user_text_or_commands(self):
		identity = "d-20261024-100000-abcdef01"
		request = {"role": "backend", "goal": "합성 목표", "done": ["합성 완료 조건"],
			"owned_paths": ["office/*.py"], "forbidden": ["합성 금지 사항"]}
		row = {"id": identity, "created": self.now.isoformat(), "text": "보내면 안 되는 사용자 원문", "summary": {"plan": "p01"}}
		data = {"request": request, "budget": {"calls": 4, "minutes": 30, "fixes": 1}}
		before = self.snapshot()
		with mock.patch.object(server.drafts, "load", return_value=(row, data)) as load:
			status, headers, content = self.request("/api/draft/" + identity)
			self.assertEqual(status, 200)
			self.assertEqual(json.loads(content)["draft"]["budget"], data["budget"])
			self.assertNotIn(row["text"], content.decode())
			self.assertNotIn("Access-Control-Allow-Origin", headers)
			load.assert_called_once_with(self.hub, identity)
		for target in ("/api/draft/../xyz", "/api/draft/", "/api/draft/" + identity, "/api/draft/" + identity + "?xyz"):
			self.assertEqual(self.request(target)[0], 409)
		self.assertEqual(before, self.snapshot())
