"""127.0.0.1에서 GET만 받는 작은 사무실 서버."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import build_state

STATIC = Path(__file__).with_name("static")
CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:; "
	"font-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
	def log_message(self, format, *args):
		pass

	def respond(self, code: int, content: bytes, content_type: str = "text/plain; charset=utf-8"):
		self.send_response(code)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(content)))
		self.send_header("Content-Security-Policy", CSP)
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Cache-Control", "no-store")
		if code == 405:
			self.send_header("Allow", "GET")
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(content)

	def parse_request(self):
		if not super().parse_request():
			return False
		port = self.server.server_port
		hosts = self.headers.get_all("Host", [])
		if len(hosts) != 1 or hosts[0] not in (f"127.0.0.1:{port}", f"localhost:{port}"):
			self.respond(403, "로컬 Host만 허용합니다".encode())
			return False
		if self.command != "GET":
			self.respond(405, "GET만 허용합니다".encode())
			return False
		return True

	def do_GET(self):
		target = self.requestline.split()[1]
		if target == "/api/state":
			try:
				payload = json.dumps(build_state(), ensure_ascii=False, allow_nan=False).encode("utf-8")
			except Exception:
				self.respond(503, '{"status":"확인 불가","reason":"화면 자료를 만들지 못했습니다"}'.encode(), "application/json; charset=utf-8")
				return
			self.respond(200, payload, "application/json; charset=utf-8")
		elif target in ("/", "/office.js"):
			name, kind = ("index.html", "text/html") if target == "/" else ("office.js", "text/javascript")
			self.respond(200, (STATIC / name).read_bytes(), kind + "; charset=utf-8")
		else:
			self.respond(404, "페이지가 없습니다".encode())


def create_server(port: int = 8790) -> ThreadingHTTPServer:
	return ThreadingHTTPServer(("127.0.0.1", port), Handler)
