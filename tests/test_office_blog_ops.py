"""임시 홈과 합성 블로그 서버로 운영 창구의 경계를 확인한다."""

import json
import fcntl
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from office import actions, blog_ops, ledger
from test_office import OfficeCase
import test_office_actions_http as action_http


class FakeBlog(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def handle_request(self):
		length = int(self.headers.get("Content-Length", "0"))
		body = self.rfile.read(length)
		self.server.received.append((self.command, self.path, dict(self.headers), body))
		self.server.started.set()
		if self.server.mode == "disconnect":
			self.connection.shutdown(socket.SHUT_RDWR)
			self.close_connection = True
			return
		if self.server.mode == "delay":
			self.server.release.wait(2)
		status, payload = self.server.replies[self.command]
		body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
		try:
			self.send_response(status)
			if status == 302:
				self.send_header("Location", "/other")
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			self.wfile.write(body)
		except OSError:
			pass

	do_GET = handle_request
	do_POST = handle_request


class OfficeBlogOpsTest(OfficeCase):
	request = action_http.OfficeActionHTTPTest.request

	def setUp(self):
		super().setUp()
		self.bid = "unused"
		self.httpd = SimpleNamespace(server_port=8790, office_token=secrets.token_urlsafe(32))
		self.slug = "post-" + secrets.token_hex(8)
		self.private = "private-" + secrets.token_hex(12)
		self.secret = secrets.token_urlsafe(32)
		self.blog = self.user / "workspace/naver-blog-agent"
		self.folder = self.blog / ".blogops"
		self.folder.mkdir(parents=True, mode=0o700)
		self.file = self.folder / "ops-token"
		self.write_token()
		(self.hub / "sources.toml").write_text("vault=" + json.dumps(str(self.user / "vault"))
			+ "\n[[repo]]\npath=" + json.dumps(str(self.blog)) + '\nclass="personal"\n')
		self.blog_state = {"running_jobs": [{"id": 4, "kind": "STAGE", "started_at": "2026-10-28T10:00:00",
			"title": self.private}], "recent_failures": [{"kind": self.private, "finished_at": "2026-10-28T09:00:00"}],
			"circuits": {"STAGE": {"open": True, "consecutive_failures": 2, "reason": self.private},
				"GENERATE": {"open": False, "consecutive_failures": 0}},
			"post_counts": {name: 0 for name in ("PLANNED", "AWAITING_EXPERIENCE", "DRAFTING", "GATE_CHECK",
				"POLISHING", "REVIEWING", "RENDERED", "STAGING", "STAGED", "PUBLISHED", "ARCHIVED", "FAILED")},
			"private": self.private}
		try:
			self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBlog)
			self.tcp = True
		except PermissionError:
			self.tcp = False
			self.fake = SimpleNamespace(server_port=49152)
			patch = mock.patch.object(blog_ops.http.client.HTTPConnection, "connect", lambda connection: self.connect_socketpair(connection))
			patch.start()
			self.addCleanup(patch.stop)
		self.fake.received, self.fake.mode = [], "normal"
		self.fake.started, self.fake.release = threading.Event(), threading.Event()
		self.fake.replies = {"GET": (200, {"ok": True, "message": self.private, "job_id": None, "state": self.blog_state}),
			"POST": (200, {"ok": True, "message": self.private, "job_id": 42, "body": self.private})}
		self.threads = []
		if self.tcp:
			self.thread = threading.Thread(target=self.fake.serve_forever, daemon=True)
			self.thread.start()
		self.addCleanup(self.stop_server)
		patch = mock.patch.object(blog_ops, "PORT", self.fake.server_port)
		patch.start()
		self.addCleanup(patch.stop)

	def stop_server(self):
		self.fake.release.set()
		if self.tcp:
			self.fake.shutdown()
			self.fake.server_close()
			self.thread.join(2)
		for thread in self.threads:
			thread.join(2)

	def connect_socketpair(self, connection):
		if connection.host != "127.0.0.1" or connection.port != self.fake.server_port:
			raise OSError("시험 외 연결 금지")
		client, server = socket.socketpair()
		client.settimeout(connection.timeout)
		connection.sock = client
		def respond():
			with server:
				FakeBlog(server, ("127.0.0.1", 1), self.fake)
		thread = threading.Thread(target=respond, daemon=True)
		self.threads.append(thread)
		thread.start()

	def write_token(self):
		self.file.write_text(self.secret, encoding="ascii")
		self.file.chmod(0o600)

	def post(self, action, identity, **kwargs):
		status, _, raw = self.request({"action": action, "id": identity}, **kwargs)
		return status, json.loads(raw)

	def assert_private_absent(self, value):
		content = json.dumps(value, ensure_ascii=False)
		self.assertTrue(all(marker not in content for marker in (self.slug, self.private, self.secret, self.httpd.office_token)))

	def test_four_actions_send_exact_contract_once_and_log_only_safe_results(self):
		payloads = (("blog_state", "state", "조회 완료"), ("blog_stage_retry", self.slug, "접수"),
			("blog_redraft", self.slug, "접수"), ("blog_clip_scan", "followers", "접수"))
		with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid", "NO_PROXY": ""}):
			for action, identity, outcome in payloads:
				status, value = self.post(action, identity, headers={"Sec-Fetch-Site": "same-origin"})
				self.assertEqual((status, value["result"]), (200, outcome))
				self.assert_private_absent(value)
				if action == "blog_state":
					state = value["state"]
					self.assertEqual(set(state), {"running_jobs", "recent_failures", "circuits", "post_counts"})
					self.assertEqual(state["running_jobs"], [{"id": 4, "kind": "STAGE", "started_at": "2026-10-28T10:00:00"}])
					self.assertEqual(state["recent_failures"][0]["kind"], "UNKNOWN")
				else:
					self.assertEqual(value["job_id"], 42)
					self.assertIn("완료 여부", value["message"])
		self.assertEqual(len(self.fake.received), 4)
		for index, (method, path, headers, body) in enumerate(self.fake.received):
			self.assertEqual((method, path), ("GET", "/api/ops/state") if index == 0 else ("POST", "/api/ops"))
			self.assertTrue(headers.get("X-Ops-Token") == self.secret)
			self.assertNotIn("Origin", headers)
			self.assertNotIn("Sec-Fetch-Site", headers)
			if index:
				expected = ({"action": "stage_retry", "slug": self.slug}, {"action": "redraft", "slug": self.slug},
					{"action": "clip_scan", "kind": "followers"})[index - 1]
				self.assertTrue(json.loads(body) == expected)
			else:
				self.assertEqual(body, b"")
		log = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assert_private_absent(log)
		rows = [json.loads(line) for line in log.splitlines()]
		self.assertEqual([row["action"] for row in rows], [row[0] for row in payloads])
		self.assertEqual([row["result"] for row in rows], [row[2] for row in payloads])
		self.assertTrue(all(set(row) == {"at", "action", "result"} for row in rows))
		activity = ledger.read_action_activity(self.hub)
		self.assertEqual(len(activity), 4)
		self.assert_private_absent(activity)
		self.assertTrue(all(row["source"] == "office-action" for row in activity))
		with mock.patch("socket.create_connection", side_effect=OSError("시험 외 연결 금지")):
			visible = [row for row in self.state()["activity"] if row["source"] == "office-action"]
		self.assertEqual(len(visible), 4)
		self.assert_private_absent(visible)

	def test_invalid_ids_extra_fields_and_other_actions_never_reach_blog(self):
		bad = [{"action": "blog_state", "id": "other"}, {"action": "blog_state", "id": "state", "extra": 1},
			{"action": "blog_clip_scan", "id": "other"}, {"action": "blog_publish", "id": self.slug},
			{"action": "stage_circuit_reset", "id": "state"}, {"action": "blog_comment", "id": self.slug},
			{"action": "blog_neighbor_visit", "id": self.slug}, {"action": "blog_circuit_reset", "id": "state"}]
		for action in ("blog_stage_retry", "blog_redraft"):
			bad.extend({"action": action, "id": value} for value in ("", ".", "..", "../x", "x/y", "x\\y", "x\n", "가", 1, "x" * 256))
		for payload in bad:
			self.assertEqual(self.request(payload)[0], 400)
		self.assertEqual(len(self.fake.received), 0)

	def test_office_http_guards_and_remote_login_stop_requests_before_blog(self):
		payload = {"action": "blog_redraft", "id": self.slug}
		for headers, status in (({"Host": "invalid"}, 403), ({"Origin": None}, 403),
				({"Origin": "http://invalid"}, 403), ({"X-Office-Token": None}, 403),
				({"X-Office-Token": "invalid"}, 403), ({"Content-Type": "text/plain"}, 415)):
			self.assertEqual(self.request(payload, headers=headers)[0], status)
		for body in (b"{", b"null", b"\xff"):
			self.assertEqual(self.request(payload, body=body)[0], 400)
		self.assertEqual(self.request(payload, body=b" " * 16385)[0], 413)
		for key, value, status in (("Host", "127.0.0.1:8790", 403), ("Origin", "http://127.0.0.1:8790", 403),
				("X-Office-Token", self.httpd.office_token, 403), ("Content-Type", "application/json", 415)):
			self.assertEqual(self.request(payload, extra=[(key, value)])[0], status)
		host = "desk.example.ts.net:8790"
		(self.hub / "office.toml").write_text(f'[remote]\nhosts=["{host}"]\nlogins=["owner"]\n')
		for login in (None, "other"):
			self.assertEqual(self.request(payload, headers={"Host": host, "Origin": f"https://{host}",
				"Tailscale-User-Login": login})[0], 403)
		self.assertEqual(len(self.fake.received), 0)
		self.assertEqual(self.request(payload, headers={"Host": host, "Origin": f"https://{host}",
			"Tailscale-User-Login": "owner"})[0], 200)
		self.assertEqual(len(self.fake.received), 1)

	def test_token_file_and_parent_must_be_safe_and_are_never_created_by_adapter(self):
		self.file.unlink()
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.assertFalse(self.file.exists())
		self.write_token()
		self.file.chmod(0o644)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.file.chmod(0o600)
		other = self.folder / "other"
		os.link(self.file, other)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		other.unlink()
		self.file.rename(other)
		self.file.symlink_to(other.name)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.file.unlink()
		other.rename(self.file)
		self.folder.chmod(0o777)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.folder.chmod(0o700)
		self.file.write_text("invalid")
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.file.unlink()
		os.mkfifo(self.file)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.file.unlink()
		self.write_token()
		other_folder = self.blog / "other"
		self.folder.rename(other_folder)
		self.folder.symlink_to(other_folder.name, target_is_directory=True)
		self.assertEqual(self.post("blog_state", "state")[0], 503)
		self.folder.unlink()
		other_folder.rename(self.folder)
		self.assertEqual(len(self.fake.received), 0)

	def test_lock_and_misclassified_source_block_delivery(self):
		with actions.private_file(self.hub, "run", "office-action.lock") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			self.assertEqual(self.post("blog_stage_retry", self.slug)[0], 409)
		config = self.hub / "sources.toml"
		config.write_text("vault=" + json.dumps(str(self.user / "vault"))
			+ "\n[[repo]]\npath=" + json.dumps(str(self.blog)) + '\nclass="company"\n')
		self.assertEqual(self.post("blog_stage_retry", self.slug)[0], 503)
		self.assertEqual(len(self.fake.received), 0)

	def test_source_config_and_default_path_discovery(self):
		alternate = self.user / "alternate" / "naver-blog-agent"
		alternate.parent.mkdir()
		self.blog.rename(alternate)
		config = self.hub / "sources.toml"
		config.write_text("vault=" + json.dumps(str(self.user / "vault"))
			+ "\n[[repo]]\npath=" + json.dumps(str(alternate)) + '\nclass="personal"\n')
		self.assertEqual(self.post("blog_state", "state")[0], 200)
		alternate.rename(self.blog)
		config.unlink()
		self.assertEqual(self.post("blog_state", "state")[0], 200)
		self.assertEqual(len(self.fake.received), 2)

	def test_failures_and_timeouts_do_not_retry_and_return_uncertain(self):
		for mode in ("disconnect", "delay"):
			self.fake.mode = mode
			self.fake.release.clear()
			before = len(self.fake.received)
			with mock.patch.object(blog_ops, "TIMEOUT", 0.05):
				status, value = self.post("blog_stage_retry", self.slug)
			self.fake.release.set()
			self.assertEqual((status, value["result"]), (502, "결과 불명"))
			self.assertEqual(len(self.fake.received), before + 1)
			self.assert_private_absent(value)
		self.assert_private_absent((self.hub / "logs/office-actions.jsonl").read_text())

	def test_redirect_and_invalid_response_never_forward_or_claim_success(self):
		for status, payload in ((302, {"ok": False, "job_id": None, "message": self.private}),
				(200, {"ok": True, "job_id": None, "message": self.private}),
				(503, {"ok": False, "job_id": 42, "message": self.private}),
				(409, {"ok": False, "message": self.private}),
				(200, b'{"ok":true,"ok":false,"job_id":42}')):
			self.fake.replies["POST"] = status, payload
			before = len(self.fake.received)
			code, value = self.post("blog_redraft", self.slug)
			self.assertEqual((code, value["result"]), (502, "결과 불명"))
			self.assertEqual(len(self.fake.received), before + 1)
			self.assert_private_absent(value)
		self.fake.replies["POST"] = 409, {"ok": False, "job_id": None, "message": self.private}
		code, value = self.post("blog_clip_scan", "comments")
		self.assertEqual((code, value["result"]), (409, "거부"))
		self.assert_private_absent(value)

	def test_audit_write_failure_after_delivery_does_not_retry(self):
		audit = mock.Mock()
		audit.write.side_effect = OSError("합성 오류")
		status, value = blog_ops.execute(self.hub, "blog_redraft", self.slug, audit)
		self.assertEqual((status, value["result"]), (500, "결과 불명"))
		self.assertEqual(len(self.fake.received), 1)
		self.assert_private_absent(value)

	def test_untrusted_state_and_bad_activity_fields_cannot_leak_text(self):
		self.blog_state["running_jobs"][0]["started_at"] = self.private
		status, value = self.post("blog_state", "state")
		self.assertEqual((status, value["result"]), (502, "실패"))
		self.assert_private_absent(value)
		log = self.hub / "logs/office-actions.jsonl"
		with log.open("a") as output:
			output.write(json.dumps({"at": "2026-10-28T10:00:00", "action": "blog_redraft",
				"result": self.private, "message": self.private, "id": self.slug}) + "\n")
		activity = ledger.read_action_activity(self.hub)
		self.assertEqual(len(activity), 1)
		self.assert_private_absent(activity)
