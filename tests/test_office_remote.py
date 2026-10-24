"""태일스케일 같은 사설망 요청의 허용 규칙을 소켓 없이 원시 HTTP로 확인한다(합성 이름만 쓴다)."""

import http.client
import io
import json
import secrets
from types import SimpleNamespace
from unittest import mock

from office import remote, server
from test_office import OfficeCase

HOST = "box-acme.tail1234.ts.net:8790"
LOGIN = "owner@example.invalid"


class OfficeRemoteTest(OfficeCase):
	def setUp(self):
		super().setUp()
		patch = mock.patch("socket.create_connection", side_effect=OSError("합성 연결 거부"))
		patch.start()
		self.addCleanup(patch.stop)
		self.httpd = SimpleNamespace(server_port=8790, office_token=secrets.token_urlsafe(32))

	def configure(self, text):
		(self.hub / "office.toml").write_text(text, encoding="utf-8")

	def good(self):
		self.configure(f'[remote]\nhosts = ["{HOST}"]\nlogins = ["{LOGIN}"]\n')

	def request(self, *, host=HOST, login=LOGIN, origin=None, post=False, peer="127.0.0.1", token=None):
		headers = {"Host": host}
		if login is not None:
			headers["Tailscale-User-Login"] = login
		body = b'{"action":"c4_apply","id":"x"}'
		if post:
			headers.update({"Origin": origin, "Content-Type": "application/json", "Content-Length": str(len(body)),
				"X-Office-Token": token or "wrong-token"})
		line = "POST /api/action" if post else "GET /api/state"
		raw = f"{line} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v is not None) + "\r\n"

		class MemorySocket:
			def __init__(inner, data):
				inner.data, inner.output = data, io.BytesIO()
			def makefile(inner, *args):
				return io.BytesIO(inner.data)
			def sendall(inner, data):
				inner.output.write(data)
		sock = MemorySocket(raw.encode("latin-1") + (body if post else b""))
		server.Handler(sock, (peer, 1), self.httpd)
		response = http.client.HTTPResponse(MemorySocket(sock.output.getvalue()), method="POST" if post else "GET")
		response.begin()
		return response.status, response.read()

	def test_remote_is_refused_without_configuration_and_with_bad_configuration(self):
		self.assertEqual(self.request()[0], 403)
		for text in ("", '[remote]\nhosts = []\nlogins = []\n', f'[remote]\nhosts = ["{HOST}"]\n',
				f'[remote]\nhosts = ["evil.example.com"]\nlogins = ["{LOGIN}"]\n',
				f'[remote]\nhosts = ["{HOST}"]\nlogins = ["{LOGIN}"]\nextra = 1\n', "[remote\n"):
			with self.subTest(text=text):
				self.configure(text)
				self.assertEqual(self.request()[0], 403)
				self.assertEqual(remote.rules(self.hub), (frozenset(), frozenset()))

	def test_remote_needs_exact_host_login_and_loopback_peer(self):
		self.good()
		self.assertEqual(self.request()[0], 200)
		self.assertEqual(self.request(login=LOGIN.upper())[0], 200)
		for kwargs in ({"host": "other.tail1234.ts.net:8790"}, {"host": "box-acme.tail1234.ts.net"}, {"host": "box-acme.tail1234.ts.net:9999"}, {"host": "box-acme.tail1234.ts.net.evil.com:8790"},
				{"login": None}, {"login": "other@example.invalid"}, {"peer": "10.0.0.5"}):
			with self.subTest(kwargs=kwargs):
				self.assertEqual(self.request(**kwargs)[0], 403)

	def test_local_requests_keep_working_and_ignore_remote_login_header(self):
		self.good()
		status, _ = self.request(host="127.0.0.1:8790", login=None)
		self.assertEqual(status, 200)
		status, _ = self.request(host="127.0.0.1:8790", login="anyone")
		self.assertEqual(status, 200)

	def test_post_origin_must_match_the_remote_host_and_only_for_remote_requests(self):
		self.good()
		status, body = self.request(post=True, origin=f"https://{HOST}")
		self.assertEqual(status, 403)
		self.assertIn("세션 비밀값", json.loads(body)["message"])
		status, body = self.request(post=True, origin=f"http://{HOST}")
		self.assertIn("세션 비밀값", json.loads(body)["message"])
		status, body = self.request(post=True, origin="http://box-acme.tail1234.ts.net")
		self.assertIn("같은 출처", json.loads(body)["message"])
		status, body = self.request(post=True, origin="https://other.tail1234.ts.net")
		self.assertIn("같은 출처", json.loads(body)["message"])
		status, body = self.request(post=True, host="127.0.0.1:8790", login=None, origin=f"https://{HOST}")
		self.assertIn("같은 출처", json.loads(body)["message"])
		status, body = self.request(post=True, host="127.0.0.1:8790", login=None, origin="http://127.0.0.1:8790")
		self.assertIn("세션 비밀값", json.loads(body)["message"])
