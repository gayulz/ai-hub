"""브라우저 없이 합성 DOM과 두 HTTP 처리기를 연결하는 시험 도구."""

import http.client
import json
import secrets
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from office import blog_ops, server
from test_office import OfficeCase
from test_office_static import HARNESS

NODE = shutil.which("node")
REMOTE_HOST = "device.synthetic.ts.net"
REMOTE_LOGIN = "synthetic-owner"


class FakeBlog(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def handle_request(self):
		body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
		self.server.received.append((self.command, self.path, self.headers.get("X-Ops-Token"), body))
		mode = self.server.mode
		if mode == "timeout":
			self.server.release.wait(1)
		if mode == "auth" or self.headers.get("X-Ops-Token") != self.server.secret:
			status = 401
		elif mode == "denied":
			status = 409
		elif mode == "error":
			status = 500
		else:
			status = 200
		if status != 200:
			payload = {"ok": False, "job_id": None, "message": self.server.private,
				"details": self.server.secret + self.server.private}
		elif self.command == "GET":
			payload = {"ok": True, "state": self.server.state, "message": self.server.private}
		else:
			payload = {"ok": True, "job_id": 42, "body": self.server.private}
		raw = json.dumps(payload, ensure_ascii=False).encode()
		try:
			self.send_response(status)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(raw)))
			self.end_headers()
			self.wfile.write(raw)
		except OSError:
			pass

	do_GET = handle_request
	do_POST = handle_request


BRIDGE = r"""
const readline=require('readline'),waiting=[];
let sessionToken='';
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>waiting.shift()(JSON.parse(line)));
const bridge=message=>new Promise(resolve=>{waiting.push(resolve);process.stdout.write(JSON.stringify(message)+'\n')});
"""

FETCH = r"""fetch:async(url,options={})=>{
		requests.push(url);
		if(url==='/api/action'){
			actionCount++;actionBody=JSON.parse(options.body);
			assert.equal(options.method,'POST');assert.equal(options.redirect,'error');
			assert.equal(options.headers['X-Office-Token'],sessionToken);
		}
		const response=await bridge({kind:'http',url,method:options.method||'GET',headers:options.headers||{},body:options.body||null});
		if(url==='/api/state')state=response.data;
		if(url==='/api/session')sessionToken=response.data.token;
		return {ok:response.status>=200&&response.status<300,json:async()=>response.data};
	}};"""


class BlogIntegrationCase(OfficeCase):
	def setUp(self):
		super().setUp()
		self.secret, self.private = secrets.token_urlsafe(32), "body-" + secrets.token_hex(16)
		folder = self.user / "workspace" / "naver-blog-agent" / ".blogops"
		folder.mkdir(parents=True, mode=0o700)
		(folder / "ops-token").write_text(self.secret, encoding="ascii")
		(folder / "ops-token").chmod(0o600)
		(self.hub / "sources.toml").write_text("vault=" + json.dumps(str(self.user / "vault"))
			+ "\n[[repo]]\npath=" + json.dumps(str(folder.parent)) + '\nclass="personal"\n')
		(self.hub / "office.toml").write_text(
			f'[remote]\nhosts=["{REMOTE_HOST}"]\nlogins=["{REMOTE_LOGIN}"]\n')
		self.threads = []
		self.fake = self.office = None
		try:
			self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBlog)
			self.office = server.create_server(0)
			self.tcp = True
		except PermissionError:
			if self.fake:
				self.fake.server_close()
			self.tcp = False
			self.fake = SimpleNamespace(server_port=49152)
			self.office = SimpleNamespace(server_port=49153, office_token=secrets.token_urlsafe(32))
			patch = mock.patch.object(http.client.HTTPConnection, "connect", lambda connection: self.connect_pair(connection))
			patch.start()
			self.addCleanup(patch.stop)
		self.fake.received, self.fake.mode = [], "success"
		self.fake.secret, self.fake.private = self.secret, self.private
		self.fake.release = threading.Event()
		self.fake.state = {"running_jobs": [{"id": 7, "kind": "STAGE", "started_at": None,
			"title": self.private}], "recent_failures": [], "circuits": {
			name: {"open": False, "consecutive_failures": 0} for name in ("STAGE", "GENERATE")},
			"post_counts": {name: 0 for name in blog_ops.STATES}, "body": self.private}
		for obj, name, value in ((blog_ops, "PORT", self.fake.server_port), (blog_ops, "TIMEOUT", 0.1)):
			patch = mock.patch.object(obj, name, value)
			patch.start()
			self.addCleanup(patch.stop)
		patch = mock.patch("office.state.blog_dashboard", return_value="꺼짐")
		patch.start()
		self.addCleanup(patch.stop)
		if self.tcp:
			for endpoint in (self.fake, self.office):
				thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
				thread.start()
				self.threads.append(thread)
		self.addCleanup(self.stop_servers)

	def connect_pair(self, connection):
		if connection.host != "127.0.0.1":
			raise OSError("시험 외 연결 금지")
		if connection.port == self.fake.server_port:
			handler, endpoint = FakeBlog, self.fake
		elif connection.port == self.office.server_port:
			handler, endpoint = server.Handler, self.office
		else:
			raise OSError("시험 외 연결 금지")
		client, peer = socket.socketpair()
		client.settimeout(connection.timeout)
		connection.sock = client
		def serve():
			with peer:
				try:
					handler(peer, ("127.0.0.1", 1), endpoint)
				except OSError:
					pass
		thread = threading.Thread(target=serve, daemon=True)
		self.threads.append(thread)
		thread.start()

	def stop_servers(self):
		self.fake.release.set()
		if self.tcp:
			for endpoint in (self.fake, self.office):
				endpoint.shutdown()
				endpoint.server_close()
		for thread in self.threads:
			thread.join(2)

	def http(self, url, method="GET", headers=None, body=None, site="local", host=None, login=None):
		remote = site == "remote"
		actual_host = host or (REMOTE_HOST if remote else f"127.0.0.1:{self.office.server_port}")
		values = {"Host": actual_host, **(headers or {})}
		if remote:
			if login is not None:
				values["Tailscale-User-Login"] = login
			values["Origin"] = "https://" + actual_host
		elif method == "POST":
			values["Origin"] = "http://" + actual_host
		connection = http.client.HTTPConnection("127.0.0.1", self.office.server_port, timeout=5)
		try:
			connection.request(method, url, body, values)
			response = connection.getresponse()
			raw = response.read()
			try:
				data = json.loads(raw)
			except (ValueError, UnicodeError):
				data = None
			if url in ("/api/action", "/api/state"):
				self.assert_safe(raw.decode("utf-8", errors="replace"))
			return {"status": response.status, "data": data}
		finally:
			connection.close()

	def assert_safe(self, text):
		self.assertTrue(all(value not in text for value in (self.secret, self.private, self.office.office_token)),
			"합성 비공개 값이 응답 또는 기록에 포함됨")

	def rows(self):
		log = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assert_safe(log)
		return [json.loads(line) for line in log.splitlines()]

	def dom(self, script, site):
		self.assertIsNotNone(NODE, "합성 DOM 시험에는 Node 실행기가 필요합니다")
		harness = HARNESS.replace("const sandbox={", BRIDGE + "\nconst sandbox={", 1)
		start = harness.index("fetch:async(url,options)=>{")
		end = harness.index("\nvm.createContext(sandbox);", start)
		harness = harness[:start] + FETCH + harness[end:]
		harness = harness.replace("sessionOK?'xyz-session':null", "sessionOK?sessionToken:null", 1)
		marker = "assert.deepEqual(requests,['/scene.json','/api/session','/api/state']);"
		self.assertEqual(harness.count(marker), 1)
		harness = harness.replace(marker, marker + script + "\ninput.close();process.stdin.pause();return;", 1)
		process = subprocess.Popen([NODE, "-e", harness, str(server.STATIC), "integration"],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		timer = threading.Timer(45, process.kill)
		timer.start()
		try:
			for line in process.stdout:
				message = json.loads(line)
				if message["kind"] == "http":
					answer = self.http(message["url"], message["method"], message["headers"],
						message["body"], site, login=REMOTE_LOGIN if site == "remote" else None)
				elif message["kind"] == "count":
					answer = len(self.fake.received)
				elif message["kind"] == "mode":
					self.fake.mode = message["mode"]
					answer = True
				elif message["kind"] == "markers":
					answer = [self.secret, self.private, self.office.office_token]
				else:
					self.fail("합성 DOM의 알 수 없는 요청")
				process.stdin.write(json.dumps(answer, ensure_ascii=False) + "\n")
				process.stdin.flush()
			process.wait(timeout=5)
			self.assertEqual(process.returncode, 0, "합성 DOM 연동 실패")
		finally:
			timer.cancel()
			if process.poll() is None:
				process.kill()
				process.wait()
			for stream in (process.stdin, process.stdout, process.stderr):
				stream.close()
