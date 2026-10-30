"""거절된 정기 보고만 다시 열고 기존 격리 근거의 범위를 드러낸다."""
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_agency_periodic_recovery as fixtures
from agency import documents, document_run, private_provider, project, runner
from agency.__main__ import main as agency_main
from orchestrator import draft, providers


class PeriodicRetryTest(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.RecoveryTest(methodName="runTest")
		self.fixture.setUp()
		self.addCleanup(self.fixture.doCleanups)
		f = self.fixture
		row = project.load(f.identity)
		task = row["tasks"][1]
		request_path = Path(task["request_path"])
		import tomllib
		request = tomllib.loads(request_path.read_text())
		request["request"].pop("report_dependencies", None)
		content = draft.serialize(request).encode()
		fixtures.write_private(request_path, content)
		with project._edit(f.identity, "synthetic_independent_task") as current:
			current["tasks"][1]["depends_on"] = []
			current["tasks"][1]["request_sha256"] = fixtures.digest(content)
		policy = f.facts["tests"]["partial"]["policy"]
		policy.update(network="deny-all", protected=[str(Path.home()), str(f.case.repo)],
			exec=["/synthetic/Python", "/synthetic/python3.14"])
		record = json.loads(f.record.read_text())
		record["policy_sha256"] = fixtures.digest(json.dumps(policy, sort_keys=True).encode())
		command = ("/synthetic/python3.14", "-m", "unittest", "discover", "-s", "tests")
		record["command_sha256"] = fixtures.digest(json.dumps(command).encode())
		record["guard"] = {key: "denied" for key in (
			"read", "read_link", "read_unlisted", "read_volume_alias", "write", "binary", "binary_link",
			"script", "script_link", "hardlink", "tcp", "unix", "child_read", "child_binary", "child_tcp", "child_unix", "source_write")}
		record["guard"].update(source_read="allowed", scratch_write="allowed")
		record["guard_process"] = {"status": "passed", "controls": "synthetic-operations-allowed",
			"execution": {"descendants": "inherited-sandbox; membership-not-observed"}}
		record["no_fork_guard"] = {name: "denied" for name in ("fork", "child", "posix_spawn", "posix_spawnp")}
		record["no_fork_process"] = {"status": "passed"}
		fixtures.write_private(f.record, json.dumps(record).encode())
		f.facts["tests"]["partial"]["record"]["sha256"] = fixtures.digest(f.record.read_bytes())
		f.write_facts()
		self.assertEqual(f.replan(), 0)

	def failed_then_completed_dependency(self):
		from agency import periodic_retry
		f = self.fixture
		failure = providers.Reply({"verdict": "changes", "summary": "격리 설명 부족", "findings": []}, .1, {})
		with mock.patch.object(periodic_retry, "isolation_summary", return_value=""), \
				mock.patch.object(private_provider, "call", return_value=failure):
			self.assertEqual(runner.run(f.identity)["status"], "막힘")
		old_task = project.load(f.identity)["tasks"][0]
		old_run = document_run.load(old_task["run_id"])
		old_bytes = (old_run.folder / "run.json").read_bytes()
		passed = providers.Reply({"verdict": "pass", "summary": "의존성 설명 확인", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=passed):
			self.assertEqual(runner.run(f.identity)["status"], "막힘")
		return old_task, old_run, old_bytes, copy.deepcopy(project.load(f.identity)["tasks"][1])

	def test_retry_preserves_previous_run_and_completed_task_then_rechecks_report(self):
		f = self.fixture
		old_task, old_run, old_bytes, completed = self.failed_then_completed_dependency()
		self.assertEqual(completed["status"], "완료")
		option = {"project": f.identity, "retry": "t1"}
		with mock.patch.dict(os.environ, {"AI_HUB_PERIODIC_REPLAN": json.dumps(option)}), \
				redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
			self.assertEqual(agency_main(["plan", f.identity]), 0)
		row = project.load(f.identity)
		self.assertEqual(row["tasks"][0]["status"], "대기")
		self.assertEqual(row["tasks"][0]["run_id"], old_task["run_id"])
		self.assertEqual(row["tasks"][1], completed)
		passed = providers.Reply({"verdict": "pass", "summary": "격리 근거 확인", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=passed):
			self.assertEqual(runner.run(f.identity)["status"], "검수")
		row = project.load(f.identity)
		self.assertEqual(row["tasks"][1], completed)
		self.assertEqual((old_run.folder / "run.json").read_bytes(), old_bytes)
		report = document_run.verified_report(f.identity, row["tasks"][0])
		for text in ("python3.14 -m unittest discover -s tests", "임시 HOME", "합성 보호", "네트워크",
				"자식 프로세스", "브라우저", "미확인"):
			self.assertIn(text, report)
		self.assertNotIn(str(Path.home()), report)

	def test_retry_rejects_unchanged_report_or_tampered_record(self):
		from agency import periodic_retry
		f = self.fixture
		self.failed_then_completed_dependency()
		before = project.load(f.identity)
		with mock.patch.object(periodic_retry, "isolation_summary", return_value=""):
			with self.assertRaises(ValueError):
				periodic_retry.prepare(f.identity, "t1")
		self.assertEqual(project.load(f.identity), before)
		recovery = Path(before["periodic_recovery"]["path"]).parent
		record = recovery / "isolation-record.json"
		record.chmod(0o600)
		record.write_bytes(b"tampered")
		with self.assertRaises(ValueError):
			periodic_retry.prepare(f.identity, "t1")
		self.assertEqual(project.load(f.identity), before)


if __name__ == "__main__":
	unittest.main()
