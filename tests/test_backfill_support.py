"""C4 테스트 공통 합성 원천. 실제 설정·대화·AI CLI에 접근하지 않는다."""

import contextlib
import gzip
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from collector import __main__ as cli
from collector import archive, redact_pii, session_summaries, sessions_other, summarize, vault
from collector.backfill_store import Store


class BackfillCase(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.hub, self.repo, self.vault = (self.root / n for n in ("hub", "acme", "vault"))
		self.hub.mkdir()
		self.repo.mkdir()
		for folder in vault.landing_dirs(self.vault):
			folder.mkdir(parents=True)
		self.clock = datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
		self.patches = [mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub),
			"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
			mock.patch.object(session_summaries, "SUMMARY_ROOT", self.root / "summaries"),
			mock.patch.object(sessions_other, "GEMINI_HOME", self.root / "original/gemini"),
			mock.patch.object(archive, "ROOTS", {t: self.root / "original" / t
				for t in ("claude", "codex", "gemini", "agy")})]
		for module in ("backfill", "backfill_common", "backfill_store", "backfill_output"):
			self.patches.append(mock.patch("collector." + module + ".now", side_effect=lambda: self.clock))
		for patch in self.patches:
			patch.start()
			self.addCleanup(patch.stop)
		self.ai_patch = mock.patch.object(summarize, "call", return_value="- 합성 작업 결론\n- 남은 일 확인")
		self.ai = self.ai_patch.start()
		self.addCleanup(self.ai_patch.stop)
		redact_pii.host_pattern.cache_clear()
		self.addCleanup(redact_pii.host_pattern.cache_clear)
		self.sources()
		self.plans()

	def sources(self, cls="personal", doc_dirs=(), extra=""):
		self.cls = cls
		(self.hub / "sources.toml").write_text(
			f"vault = {json.dumps(str(self.vault))}\n"
			f"[[classify]]\npath = {json.dumps(str(self.repo))}\nclass = '{cls}'\nname = 'acme'\n"
			f"[[repo]]\nid = 'p01'\npath = {json.dumps(str(self.repo))}\nclass = '{cls}'\nname = 'acme'\n"
			f"doc_dirs = {json.dumps(list(doc_dirs))}\n{extra}", encoding="utf-8")

	def plans(self, limit=20, size=10, company=True, extra="", per_plan=None):
		limit_line = f"daily_limit_calls = {per_plan}\n" if per_plan else ""
		(self.hub / "plans.toml").write_text(
			"[[plans]]\nid = 'p01'\ntool = 'codex'\nowner = 'personal'\n"
			f"allow = ['personal','company','study','career']\n{limit_line}"
			"[routing]\npersonal = ['p01']\nstudy = ['p01']\ncareer = ['p01']\n"
			f"company = {json.dumps(['p01'] if company else [])}\n"
			f"[backfill]\ndaily_limit_calls = {limit}\nbatch_size = {size}\n{extra}", encoding="utf-8")

	def gzip(self, relative, text):
		path = self.hub / "archive" / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		with gzip.open(path, "wt", encoding="utf-8") as file:
			file.write(text)
		return path

	def codex(self, sid="s01", day="2026-08-01", question="무엇을 바꿨나요?", answer="수정 완료", **meta):
		payload = {"id": sid, "cwd": str(self.repo), "timestamp": day + "T01:00:00Z", **meta}
		rows = [{"type": "session_meta", "payload": payload}]
		for role, text, channel in (("user", question, None), ("assistant", "중간 진행", "commentary"),
				("assistant", answer, "final")):
			if text is not None:
				rows.append({"type": "response_item", "timestamp": day + "T01:10:00Z", "payload": {
					"type": "message", "role": role, "channel": channel,
					"content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}})
		path = self.gzip("codex/sessions/2026/08/01/" + sid + ".jsonl.gz",
			"\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
		return path

	def command(self, *args):
		self.stdout, self.stderr = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
			return cli.main(["backfill", *args])

	def prepare(self, kind="sessions", *extra):
		return self.command("--project", "p01", "--kind", kind, *extra)

	def batch(self, index=-1):
		store = Store(self.hub / "ledger.db", readonly=True)
		try:
			batch = store.rows("c4_batches")[index]
			return batch, store.items(batch["id"])
		finally:
			store.close()

	def snapshot(self, root=None):
		root = root or self.root
		return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
			for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

	def git(self, *args, cwd=None, day=None, email=None):
		env = dict(os.environ)
		if day:
			env.update(GIT_AUTHOR_DATE=day, GIT_COMMITTER_DATE=day)
		if email:
			env.update(GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_NAME="xyz")
		return subprocess.run(["git", "-C", str(cwd or self.repo), *args], env=env,
			capture_output=True, text=True, check=True).stdout.strip()

	def init_git(self, root=None):
		self.git("init", "-q", cwd=root)
		self.git("config", "user.name", "acme", cwd=root)
		self.git("config", "user.email", "acme@example.test", cwd=root)
