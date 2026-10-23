"""원시 HTTP 요청으로 경로·Host·메서드 제한을 소켓 없이 확인한다."""

import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from office import server
from office.__main__ import main
from test_office import OfficeCase


class OfficeServerTest(OfficeCase):
	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790)

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
		status, headers, body = self.request("/api/state")
		self.assertEqual(status, 200)
		self.assertEqual(len(json.loads(body)["agents"]), 6)
		self.assertNotIn("Access-Control-Allow-Origin", headers)
		self.assertEqual(headers["Cache-Control"], "no-store")
		self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
		for path in ("/index.html", "/api/state?xyz=1", "/api/state/", "//", "//api/state", "///office.js",
				"/../plans.toml", "/%2e%2e/ledger.db", "/favicon.ico", "http://127.0.0.1/"):
			with self.subTest(path=path):
				self.assertEqual(self.request(path)[0], 404)

	def test_localhost_with_exact_port_is_also_allowed(self):
		self.assertEqual(self.request(host=f"localhost:{self.httpd.server_port}")[0], 200)

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
		self.assertIn('disabled', js)
		self.assertNotIn('https://', html + js)
		self.assertNotIn('http://', html + js)
		self.assertNotIn('method:', js)
		for path in [*Path("office").glob("*.py"), *server.STATIC.iterdir()]:
			self.assertLessEqual(len(path.read_text().splitlines()), 500)

	def test_cli_rejects_invalid_ports(self):
		import contextlib
		import io
		for port in ("0", "65536", "-1"):
			with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
				main(["--port", port])
