"""R1 테스트 공통 환경. 실제 볼트·설정 대신 임시 합성 자료만 쓴다."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaultreorg import __main__ as cli


class ReorgCase(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.hub, self.vault = self.root / "hub", self.root / "vault"
		self.hub.mkdir()
		self.vault.mkdir()
		self.base = self.hub / "vaultreorg"
		self.base.mkdir()
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub),
			"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
			"GIT_AUTHOR_NAME": "xyz", "GIT_COMMITTER_NAME": "xyz", "GIT_AUTHOR_EMAIL": "p01",
			"GIT_COMMITTER_EMAIL": "p01", "PYTHONDONTWRITEBYTECODE": "1"})
		self.env.start()
		self.addCleanup(self.env.stop)
		(self.hub / "sources.toml").write_text("vault = " + json.dumps(str(self.vault)) + "\n", encoding="utf-8")
		self.dictionary(["acme", "x", "xyz"])

	def write(self, path, data="# 합성 본문\n"):
		path = self.vault / path
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data.encode() if isinstance(data, str) else data)
		return path

	def settings(self, name, text):
		(self.base / name).write_text(text, encoding="utf-8")

	def dictionary(self, values):
		path = self.hub / "redaction/dictionary.json"
		path.parent.mkdir(exist_ok=True)
		path.write_text(json.dumps({"entries": [{"value": value, "kind": "회사명", "token": f"p{index}"}
			for index, value in enumerate(values, 1)]}), encoding="utf-8")

	def dates(self, notes):
		(self.hub / "oldvault-dates-261024.json").write_text(
			json.dumps({"source": "합성", "made": "2026-10-24", "notes": notes}), encoding="utf-8")

	def command(self, *args):
		self.stdout, self.stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
			return cli.main(list(args))

	def plan(self):
		return json.loads((self.base / "r1-plan.json").read_text(encoding="utf-8"))

	def notes(self):
		return {note["path"]: note for note in self.plan()["notes"]}

	def report_text(self):
		return Path(self.plan()["report"]).read_text(encoding="utf-8")

	def run_module(self, *args):
		return subprocess.run([sys.executable, "-B", "-m", "vaultreorg", *args],
			capture_output=True, text=True)

	def git(self, *args):
		return subprocess.run(["git", "-C", str(self.vault), *args], capture_output=True, check=True).stdout

	def init_git(self, *paths):
		self.git("init", "-q", "-b", "main")
		self.git("config", "user.name", "xyz")
		self.git("config", "user.email", "p01")
		self.git("config", "commit.gpgsign", "false")
		self.git("add", "--", *paths)
		self.git("commit", "-q", "-m", "합성 시작")

	def snapshot(self):
		return {path.relative_to(self.vault).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
			for path in self.vault.rglob("*") if path.is_file() and not path.is_symlink()}
