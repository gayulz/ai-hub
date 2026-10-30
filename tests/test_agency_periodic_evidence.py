"""정기 점검 근거와 실제 합성 격리 결과의 연결을 확인한다."""
import hashlib
import json
import os
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agency import documents, project
from orchestrator import draft

class ReportEvidenceTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.enterContext(mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}))
		self.identity = "p-20261028-112"
		self.folder = project._folder(self.identity)
		self.folder.mkdir(parents=True, mode=0o700)
		self.request_path = self.folder / "t1.toml"
		request = {"request": {"kind": "analysis", "workdir": str(self.folder / "work"), "role": "backend",
			"goal": "합성 점검", "done": ["근거와 미검증"], "forbidden": [], "details": "합성",
			"report_mode": "periodic-v1", "report_project": self.identity, "report_task": "t1",
			"report_checks": ["tests", "dependencies"]}, "budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		self.request_path.write_text(draft.serialize(request), encoding="utf-8")
		self.request_path.chmod(0o600)
		self.task = {"id": "t1", "request_path": str(self.request_path), "status": "실행 중", "run_id": "",
			"request_sha256": hashlib.sha256(self.request_path.read_bytes()).hexdigest()}
	def test_missing_execution_evidence_is_explicitly_unverified_and_persistent(self):
		with mock.patch("orchestrator.test_isolation.run_isolated") as backend:
			anchor = documents.prepare_evidence(self.identity, self.task)
		backend.assert_not_called()
		self.task["report_evidence"] = anchor
		evidence = documents.read_evidence(self.identity, self.task)
		self.assertEqual([item["status"] for item in evidence["checks"]], ["unverified", "unverified"])
		self.assertEqual(documents.prepare_evidence(self.identity, self.task), anchor)
		report = documents.render_evidence(evidence)
		self.assertIn("## 근거", report)
		self.assertIn("## 미검증", report)
		self.assertNotIn("통과", report)
	def test_policy_runs_isolated_test_and_records_its_result(self):
		from orchestrator import test_isolation
		name = "b" * 64
		result = {"identity": name, "executed": True, "ok": True, "status": "passed",
			"process": {"status": "passed"}, "tests": {"ran": 2, "failures": 0, "errors": 0, "skipped": 1}}
		def run(policy, command, folder):
			self.assertEqual((policy, command), ("합성 정책", ("synthetic",)))
			folder.mkdir(mode=0o700)
			file = folder / (name + ".json")
			file.write_text(json.dumps(result), encoding="utf-8")
			file.chmod(0o600)
			return result
		with mock.patch.object(test_isolation, "run_isolated", side_effect=run) as backend:
			self.task["report_evidence"] = documents.prepare_evidence(
				self.identity, self.task, policy="합성 정책", command=("synthetic",))
		backend.assert_called_once()
		with mock.patch.object(test_isolation, "run_isolated") as repeated:
			anchor = documents.prepare_evidence(self.identity, self.task, policy="합성 정책", command=("synthetic",))
		repeated.assert_not_called()
		self.assertEqual(anchor, self.task["report_evidence"])
		report = documents.render_evidence(documents.read_evidence(self.identity, self.task))
		self.assertIn("실행 2개, 실패 0개, 오류 0개, 건너뜀 1개", report)
		self.assertIn("실제 저장소 전체 시험은 미검증", report)
	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	@unittest.skipIf(os.environ.get("AIHUB_SKIP_NESTED_SANDBOX") == "1", "운영체제 격리 중첩은 별도 합성 시험으로 확인")
	def test_real_isolated_synthetic_result_reaches_report(self):
		from orchestrator import checks, test_isolation
		launcher = Path(sys.executable).resolve()
		framework = launcher.parent.parent
		engine = framework / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		content = b"import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n"
		files = (test_isolation.SyntheticFile("tests/test_sample.py", content, test_isolation.digest(content), "synthetic-inline-v1"),)
		runtime = tuple(test_isolation.RuntimeFile(str(path), test_isolation.digest(path.read_bytes()), index < 2)
			for index, path in enumerate((launcher, engine, library, framework / "Python")))
		stdlib = framework / "lib" / ("python" + framework.name)
		policy = test_isolation.Policy(files, runtime, str(launcher), (str(self.hub),),
			stdlib_sha256=checks.isolation_runtime_tree(stdlib))
		command = (str(launcher), "-m", "unittest", "discover", "-s", "tests")
		self.task["report_evidence"] = documents.prepare_evidence(self.identity, self.task, policy=policy, command=command)
		report = documents.render_evidence(documents.read_evidence(self.identity, self.task))
		self.assertIn("실행 1개, 실패 0개, 오류 0개, 건너뜀 0개", report)
		self.assertIn("실제 저장소 전체 시험은 미검증", report)
	def test_interrupted_report_reuses_completed_isolation_record(self):
		from orchestrator import test_isolation
		name = "c" * 64
		folder = self.folder / "evidence" / "isolation"
		folder.mkdir(parents=True, mode=0o700)
		record = folder / (name + ".json")
		record.write_text(json.dumps({"identity": name, "executed": True, "ok": True, "status": "passed",
			"process": {"status": "passed"}, "tests": {"ran": 1, "failures": 0, "errors": 0, "skipped": 0}}), encoding="utf-8")
		record.chmod(0o600)
		blocked = {"identity": name, "executed": False, "status": "unverified",
			"reason": "같은 조건의 시험 기록이 있어 자동으로 반복하지 않습니다"}
		with mock.patch.object(test_isolation, "run_isolated", return_value=blocked) as backend:
			self.task["report_evidence"] = documents.prepare_evidence(
				self.identity, self.task, policy="합성 정책", command=("synthetic",))
		backend.assert_called_once()
		report = documents.render_evidence(documents.read_evidence(self.identity, self.task))
		self.assertIn("실행 1개, 실패 0개, 오류 0개, 건너뜀 0개", report)
	def test_synthetic_result_is_reported_as_partial_and_rechecked(self):
		folder = self.folder / "evidence" / "isolation"
		folder.mkdir(parents=True, mode=0o700)
		name = "a" * 64 + ".json"
		record = folder / name
		result = {"identity": "a" * 64, "executed": True, "ok": True, "status": "passed",
			"process": {"status": "passed"}, "tests": {"ran": 3, "failures": 0, "errors": 0, "skipped": 1}}
		record.write_text(json.dumps(result), encoding="utf-8")
		record.chmod(0o600)
		self.task["report_evidence"] = documents.prepare_evidence(self.identity, self.task, isolation_attempt=name)
		evidence = documents.read_evidence(self.identity, self.task)
		report = documents.render_evidence(evidence)
		self.assertIn("실행 3개, 실패 0개, 오류 0개, 건너뜀 1개", report)
		self.assertIn("합성 부분 시험만 실행해 전체 시험은 미검증", report)
		self.assertNotIn("시험: 안전한 자료와 격리 실행 근거가 없어 실행하지 않았습니다", report)
		self.assertIn("실제 저장소 전체 시험은 미검증", report)
		self.assertEqual(evidence["checks"][0]["status"], "unverified")
		result["tests"]["ran"] = 4
		record.write_text(json.dumps(result), encoding="utf-8")
		with self.assertRaises(ValueError):
			documents.read_evidence(self.identity, self.task)
	def test_tampered_evidence_or_request_never_becomes_a_pass(self):
		anchor = documents.prepare_evidence(self.identity, self.task)
		self.task["report_evidence"] = anchor
		path = documents.evidence_path(self.identity, "t1")
		original = path.read_bytes()
		value = json.loads(original)
		value["checks"][0]["status"] = "passed"
		path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
		self.task["report_evidence"] = {"version": 1, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
		with self.assertRaises(ValueError):
			documents.read_evidence(self.identity, self.task)
		path.write_bytes(original)
		self.task["report_evidence"] = anchor
		self.request_path.write_text(self.request_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
		with self.assertRaises(ValueError):
			documents.read_evidence(self.identity, self.task)
