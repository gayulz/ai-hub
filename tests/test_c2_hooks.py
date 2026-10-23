"""실제 홈·서비스에 닿지 않는 훅의 시간·보안·호출 제한 검사."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from collector import hooks
from collector.backfill_common import locked


class HookCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.user = self.root / "acme"
		self.hub = self.user / ".ai-hub"
		self.hub.mkdir(parents=True)
		self.bin = self.root / "bin"
		self.bin.mkdir()
		self.log = self.root / "launch.jsonl"
		launchctl = self.bin / "launchctl"
		launchctl.write_text("#!/bin/sh\n" + '[ "$1" = warm ] && exit 0\n' + 'printf "%s\\n" "$*" >> "$ACME_LAUNCH_LOG"\n')
		launchctl.chmod(0o755)
		# macOS의 새 실행 파일 최초 검사는 훅의 실행 시간에 포함하지 않는다.
		subprocess.run([str(launchctl), "warm"], check=True, capture_output=True)
		patch = mock.patch.dict(os.environ, {"HOME": str(self.user), "AI_HUB_HOME": str(self.hub),
			"PATH": str(self.bin), "ACME_LAUNCH_LOG": str(self.log),
			"AI_HUB_COLLECTOR_LABEL": "com.acme.ai-hub-collector", "PYTHONDONTWRITEBYTECODE": "1"})
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "sources.toml").write_text('''vault = "~/vault"
[[classify]]
path = "/w/acme"
class = "company"
[[repo]]
id = "p01"
path = "/w/acme"
class = "company"
''')
		self.now = datetime(2026, 10, 23, 12).astimezone()

	def data(self, event, sid="s01"):
		return {"session_id": sid, "cwd": "/w/acme/src", "hook_event_name": event,
			"prompt": "대화 원문 합성 자료", "transcript_path": "/no-reading/acme.jsonl"}

	def command(self, tool, payload):
		return subprocess.run([sys.executable, "-m", "collector", "hook", tool], input=payload,
			capture_output=True, text=True, timeout=2)

	def test_events_store_only_safe_metadata_and_end_deadlines(self):
		for tool, events in hooks.EVENTS.items():
			for event, (status, delay) in events.items():
				with self.subTest(tool=tool, event=event):
					hooks.receive(tool, self.data(event), self.now)
					path = self.hub / f"run/presence/{tool}.json"
					data = json.loads(path.read_text())
					self.assertEqual(data, {"status": status, "at": self.now.isoformat(),
						"since": self.now.isoformat(), "session_hash": hooks.session_hash(f"{tool}:s01"), "cls": "company", "project_id": "p01"})
					self.assertEqual(path.stat().st_mode & 0o777, 0o600)
					markers = hooks.read_pending()
					marker = markers.get(hooks.session_hash(f"{tool}:s01"))
					if delay is not None:
						self.assertEqual(marker.due, self.now + timedelta(minutes=delay))
					else:
						self.assertIsNone(marker)
		for path in (self.hub / "run").rglob("*.json"):
			text = path.read_text()
			for value in ("s01", "/w/", "대화 원문", "transcript", "prompt"):
				self.assertNotIn(value, text)
		self.assertEqual(len(self.log.read_text().splitlines()), 1)

	def test_active_signal_cancels_only_its_own_delayed_marker(self):
		hooks.receive("codex", self.data("Stop", "a"), self.now)
		hooks.receive("codex", self.data("Stop", "b"), self.now)
		hooks.receive("codex", self.data("PreToolUse", "a"), self.now + timedelta(minutes=3))
		self.assertEqual(set(hooks.read_pending()), {hooks.session_hash("codex:b")})
		self.assertFalse(self.log.exists())

	def test_duration_continues_only_while_same_status_project_is_recent(self):
		path = self.hub / "run/presence/claude.json"
		hooks.receive("claude", self.data("UserPromptSubmit"), self.now)
		hooks.receive("claude", self.data("UserPromptSubmit"), self.now + timedelta(minutes=3))
		self.assertEqual(json.loads(path.read_text())["since"], self.now.isoformat())
		hooks.receive("claude", self.data("UserPromptSubmit"), self.now + timedelta(minutes=14))
		self.assertEqual(json.loads(path.read_text())["since"], (self.now + timedelta(minutes=14)).isoformat())
		hooks.receive("claude", self.data("Stop"), self.now + timedelta(minutes=15))
		self.assertEqual(json.loads(path.read_text())["since"], (self.now + timedelta(minutes=15)).isoformat())

	def test_kickstart_is_throttled_and_uses_gui_domain_without_restart(self):
		for minute in (0, 9, 10):
			hooks.receive("claude", self.data("SessionEnd", str(minute)), self.now + timedelta(minutes=minute))
		# kickstart 는 기다리지 않고 띄우므로 가짜 launchctl 기록이 끝날 때까지 잠깐 기다린다.
		deadline = time.monotonic() + 3
		while time.monotonic() < deadline and len(self.log.read_text().splitlines() if self.log.exists() else []) < 2:
			time.sleep(0.05)
		time.sleep(0.2)
		calls = self.log.read_text().splitlines()
		self.assertEqual(calls, [f"kickstart gui/{os.getuid()}/com.acme.ai-hub-collector"] * 2)
		self.assertEqual(len(hooks.read_pending()), 3)

	def test_kick_lock_contention_preserves_pending_without_waiting(self):
		with locked("hook-kick"):
			result = self.command("claude", json.dumps(self.data("SessionEnd")))
		self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
		self.assertEqual(len(hooks.read_pending()), 1)
		self.assertFalse(self.log.exists())

	def test_malformed_and_unknown_inputs_are_silent_and_fast(self):
		for payload in ("{", "[]", "null", "{}", '"acme"', "x" * 65537,
				json.dumps(self.data("PostToolUse")), json.dumps({**self.data("Stop"), "session_id": []})):
			with self.subTest(payload=payload[:30]):
				started = time.monotonic()
				result = self.command("codex", payload)
				self.assertLess(time.monotonic() - started, 0.2)
				self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
		self.assertFalse((self.hub / "run").exists())

	def test_cli_success_is_silent_fast_and_writes_metadata(self):
		for tool, event in (("claude", "UserPromptSubmit"), ("claude", "SessionEnd"),
				("codex", "Stop"), ("gemini", "AfterAgent")):
			with self.subTest(tool=tool, event=event):
				started = time.monotonic()
				result = self.command(tool, json.dumps(self.data(event)))
				self.assertLess(time.monotonic() - started, 0.2)
				self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
				self.assertEqual(json.loads((self.hub / f"run/presence/{tool}.json").read_text())["cls"], "company")
		self.assertEqual(len(hooks.read_pending()), 3)

	def test_write_failure_and_slow_writer_do_not_block_the_tool(self):
		for failure in (OSError("합성 쓰기 실패"), None):
			output, errors = io.StringIO(), io.StringIO()
			def write(*args):
				if failure:
					raise failure
				time.sleep(1)
			with mock.patch("collector.backfill_common.private_write", side_effect=write), \
					mock.patch("sys.stdin", io.StringIO(json.dumps(self.data("Stop")))), \
					contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
				started = time.monotonic()
				self.assertEqual(hooks.main(["codex"]), 0)
				self.assertLess(time.monotonic() - started, 0.2)
			self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))

	def test_stdin_deadline_is_armed_before_read_and_restored(self):
		previous = signal.getsignal(signal.SIGALRM)
		observed = []
		def read(_limit):
			observed.append(signal.getitimer(signal.ITIMER_REAL)[0])
			if signal.getsignal(signal.SIGALRM) is hooks._timeout:
				signal.raise_signal(signal.SIGALRM)
			return ""
		with mock.patch("sys.stdin") as stream:
			stream.read.side_effect = read
			self.assertEqual(hooks.main(["codex"]), 0)
		self.assertEqual(len(observed), 1)
		self.assertGreater(observed[0], 0)
		self.assertLess(observed[0], 0.2)
		self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
		self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

	def test_slow_or_unclosed_stdin_and_slow_launchctl_have_deadlines(self):
		# 입력 전의 0.2초 미만 타이머는 별도로 검사하고, CLI에는 Python 시작 여유를 준다.
		with subprocess.Popen([sys.executable, "-m", "collector", "hook", "codex"],
				stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
			try:
				self.assertEqual(proc.wait(timeout=2), 0)
			finally:
				if proc.poll() is None:
					proc.kill()
				output = proc.communicate()
			self.assertEqual(output, (b"", b""))
		# 느린 launchctl은 실행 시간 대신 자식 종료를 기다리는 호출이 없는지 검사한다.
		with mock.patch("collector.hooks.subprocess.Popen") as launch:
			hooks.receive("claude", self.data("SessionEnd"), self.now)
		launch.assert_called_once()
		launch.return_value.wait.assert_not_called()
		launch.return_value.communicate.assert_not_called()
		launch.return_value.__enter__.assert_not_called()
		self.assertEqual(len(hooks.read_pending()), 1)

	def test_sources_missing_and_symlink_targets_never_expose_input(self):
		(self.hub / "sources.toml").unlink()
		hooks.receive("gemini", self.data("BeforeTool"), self.now)
		path = self.hub / "run/presence/gemini.json"
		self.assertIsNone(json.loads(path.read_text())["cls"])
		outside = self.root / "outside.json"
		outside.write_text("합성 원본")
		path.unlink()
		path.symlink_to(outside)
		result = self.command("gemini", json.dumps(self.data("BeforeTool")))
		self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
		self.assertEqual(outside.read_text(), "합성 원본")

	def test_acknowledgement_keeps_new_signals_and_nonmarker_files(self):
		hooks.receive("codex", self.data("Stop"), self.now)
		marker = next(iter(hooks.read_pending().values()))
		other = marker.path.parent / "acme.txt"
		other.write_text("보존")
		hooks.receive("codex", self.data("Stop"), self.now + timedelta(minutes=1))
		hooks.acknowledge(marker)
		self.assertTrue(marker.path.exists())
		hooks.acknowledge(next(iter(hooks.read_pending().values())))
		self.assertFalse(marker.path.exists())
		self.assertEqual(other.read_text(), "보존")


if __name__ == "__main__":
	unittest.main()
