"""127.0.0.1에서 화면 조회와 허용된 결재 반영만 받는다."""

from __future__ import annotations

import json
import hmac
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import actions
from .state import build_state

STATIC = Path(__file__).with_name("static")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
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
			self.send_header("Allow", "GET, POST" if self.target() == "/api/action" else "GET")
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(content)

	def send_error(self, code, message=None, explain=None):
		self.close_connection = True
		self.respond(code, "HTTP 요청을 처리할 수 없습니다".encode())

	def target(self):
		parts = self.requestline.split()
		return parts[1] if len(parts) > 1 else ""

	def respond_json(self, code: int, payload: dict):
		self.respond(code, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
			"application/json; charset=utf-8")

	def parse_request(self):
		if not super().parse_request():
			return False
		port = self.server.server_port
		hosts = self.headers.get_all("Host", [])
		if len(hosts) != 1 or hosts[0] not in (f"127.0.0.1:{port}", f"localhost:{port}"):
			self.respond(403, "로컬 Host만 허용합니다".encode())
			return False
		if self.command != "GET" and not (self.command == "POST" and self.target() == "/api/action"):
			self.respond(405, "허용하지 않는 메서드입니다".encode())
			return False
		return True

	def do_POST(self):
		def reject(code, message):
			self.respond_json(code, actions.result(False, message))
		port = self.server.server_port
		origins = self.headers.get_all("Origin", [])
		if len(origins) != 1 or origins[0] not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
			reject(403, "같은 출처의 화면에서만 반영할 수 있습니다.")
			return
		kinds = self.headers.get_all("Content-Type", [])
		if len(kinds) != 1 or self.headers.get_content_type() != "application/json":
			reject(415, "application/json 요청만 허용합니다.")
			return
		lengths = self.headers.get_all("Content-Length", [])
		if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
			reject(400, "올바른 본문 길이가 필요합니다.")
			return
		length_text = lengths[0].lstrip("0") or "0"
		if len(length_text) > 4 or int(length_text) > 4096:
			reject(413, "본문은 4096바이트 이하여야 합니다.")
			return
		length = int(length_text)
		tokens = self.headers.get_all("X-Office-Token", [])
		if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), self.server.office_token.encode("ascii")):
			reject(403, "화면의 세션 비밀값을 확인할 수 없습니다. 화면을 다시 열어 주세요.")
			return
		try:
			body = self.rfile.read(length)
			if len(body) != length:
				raise ValueError
			payload = json.loads(body.decode("utf-8"))
		except (ValueError, UnicodeError, RecursionError):
			reject(400, "올바른 JSON 본문이 필요합니다.")
			return
		code, payload = actions.apply(payload)
		self.respond_json(code, payload)

	def do_GET(self):
		target = self.target()
		if target == "/api/state":
			try:
				payload = json.dumps(build_state(), ensure_ascii=False, allow_nan=False).encode("utf-8")
			except Exception:
				self.respond(503, '{"status":"확인 불가","reason":"화면 자료를 만들지 못했습니다"}'.encode(), "application/json; charset=utf-8")
				return
			self.respond(200, payload, "application/json; charset=utf-8")
		elif target == "/api/session":
			self.respond_json(200, {"token": self.server.office_token})
		elif target in ("/", "/office.js", "/pixel-art.js", "/office.css", "/scene.json"):
			name, kind = {"/": ("index.html", "text/html"), "/office.js": ("office.js", "text/javascript"),
				"/pixel-art.js": ("pixel-art.js", "text/javascript"), "/office.css": ("office.css", "text/css"),
				"/scene.json": ("scene.json", "application/json")}[target]
			self.respond(200, (STATIC / name).read_bytes(), kind + "; charset=utf-8")
		else:
			self.respond(404, "페이지가 없습니다".encode())


def create_server(port: int = 8790) -> ThreadingHTTPServer:
	server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
	server.office_token = secrets.token_urlsafe(32)
	return server
