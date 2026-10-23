"""비교 테스트용 HTTP 서버와 구독 CLI. 모든 입출력은 임시 합성 자료다."""

import json
import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest import mock

from test_backfill_support import BackfillCase


class Handler(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def respond(self, status, body):
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.end_headers()
		try:
			self.wfile.write(json.dumps(body, ensure_ascii=False).encode())
		except (BrokenPipeError, ConnectionResetError):
			pass

	def do_GET(self):
		self.server.gets.append(self.path)
		self.respond(200, {"models": [{"name": name} for name in self.server.models]})

	def do_POST(self):
		data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		self.server.posts.append((self.path, data))
		if self.server.delay:
			time.sleep(self.server.delay)
		self.respond(self.server.status, {"response": self.server.answer,
			"thinking": "합성 비공개 생각 과정", "model": data["model"]})


class CompareCase(BackfillCase):
	def setUp(self):
		super().setUp()
		self.ai_patch.stop()
		clock = mock.patch("collector.backfill_compare_report.now", side_effect=lambda: self.clock)
		clock.start()
		self.addCleanup(clock.stop)
		try:
			self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
			self.tcp = True
		except PermissionError:
			self.tcp = False
			self.server = SimpleNamespace(server_port=49152)
			patch = mock.patch("collector.ollama.urlopen", side_effect=self.memory_request)
			patch.start()
			self.addCleanup(patch.stop)
		self.server.models, self.server.posts, self.server.gets = ["xyz"], [], []
		self.server.answer = "- 로컬: 2026-08-01까지 18건을 확인하기로 결정. 남은 일은 검토."
		self.server.delay, self.server.status = 0, 200
		if self.tcp:
			self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
			self.thread.start()
		self.running = True
		self.addCleanup(self.stop_server)
		self.url = f"http://127.0.0.1:{self.server.server_port}"
		self.bin = self.root / "bin"
		self.bin.mkdir()
		self.log = self.root / "calls.jsonl"
		code = '''import json, os, sys
args = sys.argv[1:]
flag = '-m' if '-m' in args else '--model'
model = args[args.index(flag) + 1] if flag in args else None
tool = os.path.basename(sys.argv[0])
prompt = args[args.index('-p') + 1] if tool == 'agy' else sys.stdin.read()
model = model or 'xyz-default'
with open(os.environ['ACME_CALLS'], 'a', encoding='utf-8') as out:
	out.write(json.dumps({'model': model, 'prompt': prompt}, ensure_ascii=False) + '\\n')
if model in json.loads(os.environ.get('ACME_FAILURE_MODELS', '[]')):
	sys.exit(1)
answer = os.environ.get('ACME_ANSWER', '- 구독: 2026-08-01까지 18건을 확인하기로 결정. 남은 일은 검토.')
if '--output-format' in args:
	print(json.dumps({'result': answer, 'modelUsage': {model: {}}}, ensure_ascii=False))
else:
	print(answer)
	if tool == 'codex':
		print('model: ' + model, file=sys.stderr)
'''
		for name in ("codex", "claude", "agy"):
			path = self.bin / name
			path.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
			path.chmod(0o700)
		patch = mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath,
			"ACME_CALLS": str(self.log), "ACME_FAILURE_MODELS": "[]", "NO_PROXY": "127.0.0.1,localhost"})
		patch.start()
		self.addCleanup(patch.stop)
		self.configure()

	def stop_server(self):
		if self.running:
			if self.tcp:
				self.server.shutdown()
				self.server.server_close()
				self.thread.join()
			self.running = False

	def memory_request(self, request, timeout):
		"""포트 바인딩이 금지된 환경에서도 같은 HTTP 핸들러를 검사한다."""
		if not self.running or self.server.delay > timeout:
			raise URLError("합성 연결 실패 또는 시간 초과")
		handler = Handler.__new__(Handler)
		handler.server, handler.request_version = self.server, "HTTP/1.1"
		handler.path = request.full_url.removeprefix(self.url)
		handler.requestline = request.get_method() + " " + handler.path + " HTTP/1.1"
		handler.command = request.get_method()
		handler.rfile = io.BytesIO(request.data or b"")
		handler.wfile = io.BytesIO()
		handler.headers = {"Content-Length": str(len(request.data or b""))}
		(handler.do_POST if request.data is not None else handler.do_GET)()
		head, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
		status = int(head.split(b" ", 2)[1])
		if status >= 400:
			raise HTTPError(request.full_url, status, "합성 실패", {}, io.BytesIO(body))
		return io.BytesIO(body)

	def configure(self, *, limit=20, routing=None, override=None, local_allow=None,
			url=None, timeout=300, per_plan=None):
		routing = ["p01", "p02", "p03"] if routing is None else routing
		allow = ["personal", "study", "career"] if local_allow is None else local_allow
		text = ("[[plans]]\nid='p01'\ntool='codex'\nowner='personal'\n"
			"allow=['personal','study','career']\nmodel='xyz-sub'\n"
			+ (f"daily_limit_calls={per_plan}\n" if per_plan else "")
			+ "[[plans]]\nid='p02'\ntype='ollama'\nowner='personal'\nmodel='xyz'\n"
			f"allow={json.dumps(allow)}\nurl={json.dumps(url or self.url)}\ntimeout={timeout}\n"
			"[[plans]]\nid='p03'\ntool='claude'\nowner='personal'\n"
			"allow=['personal','study','career']\nmodel='xyz-next'\n[routing]\n")
		for cls in ("personal", "study", "career"):
			text += f"{cls}={json.dumps(routing)}\n"
		text += f"[backfill]\ndaily_limit_calls={limit}\n"
		if override is not None:
			text += "[backfill.routing]\n"
			for cls in ("personal", "study", "career"):
				text += f"{cls}={json.dumps(override)}\n"
		(self.hub / "plans.toml").write_text(text, encoding="utf-8")

	def subscription_calls(self):
		return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

	def compare(self, count=20):
		return self.command("--compare", "p01", "--count", str(count))
