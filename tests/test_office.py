"""사무실 검증은 합성 홈과 가짜 읽기 전용 명령만 사용한다."""

import fcntl
import io
import json
import os
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from collector.backfill_store import C4_SCHEMA
from collector.ledger import SCHEMA
from office.state import build_state

COMMAND = '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["ACME_OFFICE_COMMANDS"], "a") as out:
	out.write(json.dumps([Path(sys.argv[0]).name, *args]) + "\\n")
if Path(sys.argv[0]).name == "pgrep":
	print("123")
	sys.exit(int(os.environ.get("ACME_BLOG_EXIT", "0")))
if len(args) != 2 or args[0] != "print":
	sys.exit(9)
if os.environ.get("ACME_LAUNCH_EXIT"):
	sys.exit(113)
print("state = waiting")
print("last exit code = " + ("5" if args[1].endswith("-backup") else "0"))
'''


class OfficeCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.user = self.root / "acme"
		self.hub = self.user / ".ai-hub"
		self.hub.mkdir(parents=True)
		self.bin = self.root / "bin"
		self.bin.mkdir()
		for name in ("launchctl", "pgrep"):
			path = self.bin / name
			path.write_text("#!" + sys.executable + "\n" + COMMAND)
			path.chmod(0o755)
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub), "HOME": str(self.user),
			"USER": "acme", "LOGNAME": "acme", "PATH": str(self.bin),
			"ACME_OFFICE_COMMANDS": str(self.root / "commands.jsonl")})
		patch.start()
		self.addCleanup(patch.stop)
		self.now = datetime(2026, 10, 23, 13, 0).astimezone()
		self.opener = mock.Mock()
		self.opener.open.side_effect = URLError("연결 거부")
		patch = mock.patch("office.agents.build_opener", return_value=self.opener)
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "acme-codex"
tool = "codex"
[[plans]]
id = "acme-agy"
tool = "agy"
[[plans]]
id = "acme-claude"
tool = "claude"
[[plans]]
id = "acme-local"
tool = "ollama"
[backfill]
daily_limit_calls = 20
''')
		(self.hub / "decisions.toml").write_text('''[[item]]
id="d01"
kind="decision"
title="xyz 결정"
reply="xyz A"
due=""
doc="읽지 않는 문서"
status="open"
[[item]]
id="d02"
kind="decision"
title="닫힌 결정"
reply="xyz B"
due=""
status="closed"
''')
		(self.hub / "office.toml").write_text('[blog]\ndashboard_url="http://127.0.0.1:9000/dashboard"\n')
		self.conn = sqlite3.connect(self.hub / "ledger.db")
		self.addCleanup(self.conn.close)
		self.conn.executescript(SCHEMA + C4_SCHEMA)
		self.conn.execute("insert into c4_scopes values ('s01','p01','sessions','','','','acme-private-material')")
		self.conn.execute("insert into c4_batches values ('c4-acme','s01','prepared','acme-private-material')")
		for i, status in enumerate(("applied", "prepared", "held")):
			self.conn.execute("insert into c4_items values ('c4-acme',?,'xyz',?,'acme-private-material')", (str(i), status))
		for local, status, key in ((0, "done", "0"), (1, "reserved", "1")):
			self.conn.execute("insert into c4_calls(batch,key,part,plan,day,state,local) values ('c4-acme',?,0,'acme-local',?,?,?)",
				(key, self.now.date().isoformat(), status, local))
		self.conn.execute("insert into calls values (?, 'acme-codex', 3)", (self.now.date().isoformat(),))
		self.conn.commit()
		self.run_record("2026-10-23-005", "승인 필요")
		self.run_record("2026-10-23-006", "작업 중")
		path = self.user / ".gemini/tmp/xyz/chats/acme.json"
		path.parent.mkdir(parents=True)
		path.write_bytes(b"acme-private-material")
		os.utime(path, (self.now.timestamp() - 60,) * 2)
		folder = self.user / "Library/LaunchAgents"
		folder.mkdir(parents=True)
		for name, log in (("collector", "collector.out.log"), ("vaultsync", "vaultsync.log"), ("backup", "hubbackup.log")):
			(folder / f"com.acme.ai-hub-{name}.plist").write_bytes(plistlib.dumps({"StartCalendarInterval": {"Minute": 10}}))
			(self.hub / "logs").mkdir(exist_ok=True)
			(self.hub / "logs" / log).write_text(self.now.isoformat() + " 기록 4건 · 실패 0건 acme-private-material /acme/xyz\n")

	def run_record(self, rid, status, role="implement", plan="acme-codex"):
		folder = self.hub / "runs" / rid
		folder.mkdir(parents=True, exist_ok=True)
		(folder / "run.json").write_text(json.dumps({"status": status, "request": {"goal": "acme-private-material"}, "repo": str(self.user)}))
		(folder / "events.jsonl").write_text(json.dumps({"at": self.now.isoformat(), "event": "call", "role": role,
			"plan": plan, "summary": "acme-private-material"}) + "\n")
		(folder / ".lock").write_bytes(b"acme")
		return folder

	def hold(self, rid="2026-10-23-006"):
		lock = (self.hub / "runs" / rid / ".lock").open("rb")
		fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		self.addCleanup(lock.close)
		return lock

	def loaded_ollama(self):
		self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b'{"models":[{"name":"gemma4:26b"}]}')

	def state(self):
		return build_state(self.now)

	def snapshot(self):
		return {str(p.relative_to(self.user)): (p.read_bytes(), p.stat().st_mtime_ns)
			for p in self.user.rglob("*") if p.is_file() and not p.is_symlink()}

	def people(self, state):
		return {person["id"]: person for person in state["agents"]}
