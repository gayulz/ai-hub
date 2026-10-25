"""임시 홈과 합성 접수만 쓰는 제작 계약 시험 준비."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shorts import Store, approvals, artifacts, schema

SID = "s-" + "a" * 32
OTHER = "s-" + "b" * 32
HASH = "1" * 64


class ShortsCase(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.hub = self.root / "hub"
		(self.hub / "shorts").mkdir(parents=True, mode=0o700)
		(self.hub / "redaction").mkdir()
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "company01"},
			{"value": "xyz", "kind": "서비스명", "token": "service01"}]}), encoding="utf-8")
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch("vaultfilter.public._allowed", return_value=set()),
				mock.patch("vaultfilter.public._allowed_binaries", return_value=set())):
			patch.start()
			self.addCleanup(patch.stop)
		self.intakes = {}
		self.make_intake(SID)
		self.store = Store()
		self.run = self.store.new(SID)
		self.number = self.run["round"]

	def make_intake(self, identity):
		folder = self.hub / "shorts" / identity
		folder.mkdir(mode=0o700)
		data = {"version": 1, "id": identity, "created": schema.now(), "status": "intake_confirmed",
			"story": "합성 이야기", "source_dir": str(self.root / "source"), "save_dir": str(self.root / "save"),
			"conditions": [{"id": f"c{i:02d}", "label": "합성 조건", "value": "합성 응답"} for i in range(13)],
			"confirmed_by": "office"}
		path = folder / "intake.json"
		path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
		path.chmod(0o600)
		self.intakes[path] = path.read_bytes()
		return folder

	def tearDown(self):
		for path, original in self.intakes.items():
			self.assertEqual(path.read_bytes(), original, "접수 원문은 한 바이트도 바뀌면 안 됩니다")
		self.assertFalse((self.root / "source").exists())
		self.assertFalse((self.root / "save").exists())

	def read(self):
		return self.store.read(SID, self.number)

	def call(self, function, *args, **kwargs):
		return function(self.store, SID, self.number, *args, **kwargs)

	def add(self, name, kind, content=b"synthetic"):
		return self.call(artifacts.register, name, kind, content)

	def to_stage(self, wanted):
		while self.read()["stage"] != wanted:
			run = self.call(approvals.start_stage)
			if run["stage"] in ("script", "preview"):
				self.add(run["stage"] + ".txt", run["stage"])
			run = self.call(approvals.complete_stage)
			if run["status"] == "awaiting_user":
				self.call(approvals.approve, run["stage"], run["review"]["digest"], run["revision"])
		return self.read()

	def pending(self, stage="script"):
		self.to_stage(stage)
		self.call(approvals.start_stage)
		self.add(stage + ".txt", stage)
		return self.call(approvals.complete_stage)

	def round_path(self, number=None):
		return self.hub / "shorts" / SID / "rounds" / f"round-{number or self.number:03d}"
