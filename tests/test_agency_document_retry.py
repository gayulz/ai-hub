"""합성 실행으로 재시도 연결과 비공개 막힘·승인 경계를 검사한다."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agency import document_retry, document_run, project, runner
from agency.__main__ import main as agency_main
from tests import test_agency_document as fixtures


class DocumentRetryTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = str(Path(temporary.name).resolve())
		# 아래 시험의 임시 경로도 macOS의 시스템 별칭을 거치지 않게 한다.
		factory = tempfile.TemporaryDirectory
		patch = mock.patch.object(tempfile, "TemporaryDirectory",
			side_effect=lambda *args, **kwargs: factory(*args, dir=self.root, **kwargs))
		patch.start()
		self.addCleanup(patch.stop)

	def test_private_blockers_are_preserved_only_in_owner_file(self):
		with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,
				{"AI_HUB_HOME": temporary}):
			run = document_run.DocumentRun("2099-01-01-001", {"inputs": ["synthetic"]}, temporary,
				kind="analysis")
			run.save()
			worker = document_run.DocumentFlow(run, None, {})
			marker = "합성 비공개 막힘 원문"
			self.assertFalse(worker.absorb({"blockers": [marker]}))
			for name in ("run.json", "events.jsonl"):
				self.assertNotIn(marker, (run.folder / name).read_text())
			files = list((run.folder / "private-blockers").iterdir())
			self.assertEqual(len(files), 1)
			self.assertIn(marker, files[0].read_text())
			self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

	def test_only_project_approved_restricted_plans_are_accepted(self):
		department = SimpleNamespace(allow_plans=("writer", "other"), inputs=())
		plans = {"writer": SimpleNamespace(tool="codex", allow=("personal",)),
			"other": SimpleNamespace(tool="agy", allow=("personal",))}
		with mock.patch.object(document_retry, "load_plans", return_value=SimpleNamespace(plans=plans)):
			self.assertEqual(document_retry.approved({"private_plans": ["writer"]}, department), ["writer"])
			self.assertEqual(document_retry.approved({}, department), ["writer"])
			for row in ({"private_plans": []}, {"private_plans": ["other"]}, {"private_plans": ["unknown"]}):
				with self.subTest(row=row), self.assertRaises(ValueError):
					document_retry.approved(row, department)

	def test_missing_independent_reviewer_stops_before_provider(self):
		with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,
				{"AI_HUB_HOME": temporary}):
			run = document_run.DocumentRun("2099-01-01-002", {"inputs": []}, temporary, kind="analysis")
			# 입력 내용을 읽기 전에 승인된 검토 후보가 없는 경계를 검사한다.
			path = Path(temporary) / "synthetic.txt"
			path.write_text("합성 입력")
			path.chmod(0o600)
			run.request["inputs"] = [str(path)]
			run.save()
			worker = document_run.DocumentFlow(run, None, {"reviewer": []})
			with mock.patch.object(document_run.flow, "role_plans", return_value=[]), \
					mock.patch.object(document_run.Flow, "ask") as ask:
				self.assertIsNone(worker.ask("review", "합성 검토", {}, False))
			ask.assert_not_called()
			self.assertEqual(run.status, "사용자 입력 필요")
			self.assertFalse(run.approval_pending)

	def test_record_fingerprint_accepts_private_folder_with_legacy_read_permissions(self):
		with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,
				{"AI_HUB_HOME": temporary}):
			run = document_run.DocumentRun("2099-01-01-004", {}, temporary, kind="analysis")
			run.folder.mkdir(parents=True, mode=0o700)
			request = run.folder / "request.toml"
			request.write_text("[request]\nkind='analysis'\n")
			request.chmod(0o600)
			run.save()
			path = run.folder / "run.json"
			for mode in (0o600, 0o644):
				path.chmod(mode)
				before = path.read_bytes()
				self.assertEqual(document_retry.fingerprint(run)["run.json"],
					document_retry.inputs._hash(before))
				self.assertEqual(path.stat().st_mode & 0o777, mode)
				self.assertEqual(path.read_bytes(), before)
			path.chmod(0o666)
			with self.assertRaises(ValueError):
				document_retry.fingerprint(run)
			path.chmod(0o644)
			run.folder.chmod(0o755)
			with self.assertRaises(ValueError):
				document_retry.fingerprint(run)
			run.folder.chmod(0o700)
			request.chmod(0o644)
			with self.assertRaises(ValueError):
				document_retry.fingerprint(run)
			request.chmod(0o600)
			with mock.patch.object(document_retry.os, "getuid", return_value=os.getuid() + 1):
				with self.assertRaises(ValueError):
					document_retry.fingerprint(run)
			os.link(path, run.folder / "linked.json")
			with self.assertRaises(ValueError):
				document_retry.fingerprint(run)

	def test_revision_keeps_existing_instructions_and_changes_only_details(self):
		import tomllib
		from orchestrator import draft
		original = {"request": {"kind": "analysis", "details": "기존 합성 지침", "goal": "합성"},
			"budget": {"calls": 4, "minutes": 10, "fixes": 1}}
		raw = draft.serialize(original).encode("utf-8")
		with mock.patch("vaultfilter.public.Rules") as rules:
			rules.return_value.kinds.return_value = []
			revised = tomllib.loads(document_retry.revised_request(raw, "새 합성 지침").decode("utf-8"))
		self.assertEqual(revised["request"]["details"], "기존 합성 지침\n\n추가 일반 작성 지침:\n새 합성 지침")
		self.assertEqual(revised["budget"], original["budget"])
		revised["request"]["details"] = original["request"]["details"]
		self.assertEqual(revised, original)

	def test_sensitive_details_and_unavailable_dictionary_are_rejected(self):
		import json
		from orchestrator import draft
		raw = draft.serialize({"request": {"kind": "analysis", "details": "기존 지침"},
			"budget": {"calls": 4, "minutes": 10, "fixes": 1}}).encode()
		with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,
				{"AI_HUB_HOME": temporary}):
			folder = Path(temporary) / "redaction"
			folder.mkdir()
			path = folder / "dictionary.json"
			path.write_text(json.dumps({"entries": [{"value": "synthetic-sensitive-name",
				"kind": "회사명", "token": "company01"}]}))
			values = ["sample" + "@" + "example.invalid", "ghp_" + "a" * 36,
				"synthetic-sensitive-name"]
			for value in values:
				with self.subTest(kind=values.index(value)), self.assertRaises(ValueError) as caught:
					document_retry.revised_request(raw, value)
				self.assertNotIn(value, str(caught.exception))
			path.write_text('{"entries": []}')
			with self.assertRaisesRegex(ValueError, "민감값 검사"):
				document_retry.revised_request(raw, "유효한 일반 지침")

	def test_details_file_requires_private_regular_owned_bounded_input(self):
		with tempfile.TemporaryDirectory() as temporary:
			path = Path(temporary) / "details.txt"
			path.write_text("근거가 없는 항목은 미확인으로 표시한다")
			path.chmod(0o600)
			self.assertEqual(document_retry.read_details(path), path.read_text())
			path.chmod(0o644)
			with self.assertRaises(ValueError):
				document_retry.read_details(path)
			path.chmod(0o600)
			link = Path(temporary) / "symbolic.txt"
			link.symlink_to(path)
			with self.assertRaises(ValueError):
				document_retry.read_details(link)
			with mock.patch.object(document_retry.os, "getuid", return_value=os.getuid() + 1):
				with self.assertRaises(ValueError):
					document_retry.read_details(path)
			path.write_bytes(b"x" * 16001)
			with self.assertRaises(ValueError):
				document_retry.read_details(path)
			path.write_text("일반 지침")
			os.link(path, Path(temporary) / "hard.txt")
			with self.assertRaises(ValueError):
				document_retry.read_details(path)

	def test_legacy_private_project_requires_independent_reviewer_before_input(self):
		plans = {"writer": SimpleNamespace(id="writer", tool="codex", model=None,
			allow=("personal",)), "clone": SimpleNamespace(id="clone", tool="codex", model=None,
			allow=("personal",))}
		department = SimpleNamespace(external_ai=True, allow_plans=("writer", "clone"),
			prepare=(), inputs=())
		loaded = SimpleNamespace(plans=plans)
		def candidates(loaded, config, role):
			return [plans[value] for value in config["implementer" if role == "implement" else "reviewer"]]
		with mock.patch.object(document_retry.project, "load", return_value={}), \
				mock.patch.object(document_retry, "load_plans", return_value=loaded), \
				mock.patch.object(document_retry.orchestration, "orchestration_config",
					return_value={"implementer": ["writer"], "reviewer": ["clone"]}), \
				mock.patch("orchestrator.flow.role_plans", side_effect=candidates):
			with self.assertRaisesRegex(ValueError, "독립 검토"):
				document_retry.configuration(department, private=True, identity="synthetic",
					loader=lambda: loaded)
			with mock.patch.object(document_retry.project, "load", return_value={"private_plans": ["writer"]}):
				config = document_retry.configuration(department, private=True, identity="synthetic",
					loader=lambda: loaded)
			self.assertEqual(config["implementer"], ["writer"])
			self.assertEqual(config["reviewer"], ["writer"])
			self.assertEqual(document_retry.independent([plans["writer"]], [
				{"role": "implement", "ok": True, "plan": "writer", "tool": "codex"}]), [])

	def test_previous_run_tampering_is_rejected(self):
		with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ,
				{"AI_HUB_HOME": temporary}):
			run = document_run.DocumentRun("2099-01-01-003", {}, temporary, kind="analysis")
			run.folder.mkdir(parents=True, mode=0o700)
			request = run.folder / "request.toml"
			request.write_text("[request]\nkind='analysis'\n")
			request.chmod(0o600)
			run.finish("실패", "합성 실패")
			(run.folder / "run.json").chmod(0o600)
			task = {"document_retries": [{"run_id": run.run_id, "files": document_retry.fingerprint(run),
				"request_path": str(request), "request_sha256": document_retry.inputs._hash(request.read_bytes())}]}
			document_retry.validate_history(task)
			request.write_text("변조")
			with self.assertRaises(ValueError):
				document_retry.validate_history(task)


class DocumentRetryIntegrationTest(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.DocumentAgencyTest(methodName="runTest")
		self.fixture.setUp()
		self.addCleanup(self.fixture.doCleanups)

	def failed(self):
		f = self.fixture
		identity = f.planned()
		f.blockers = ["합성 막힘"]
		self.assertEqual(runner.run(identity)["status"], "막힘")
		row = project.load(identity)
		task = row["tasks"][0]
		execution = document_run.load(task["run_id"])
		return identity, task, execution

	def test_official_retry_preserves_history_and_stops_before_unapproved_review(self):
		from agency import departments, private_provider
		from jobs import materials, storage
		f = self.fixture
		with storage.transaction() as root:
			material, _ = materials.add(root, "합성 재시도 허용 입력")
			materials.permission(root, material["id"], True)
		config = f.hub / "departments.toml"
		config.write_text(config.read_text().replace('allow_plans=["writer", "reviewer"]',
			'allow_plans=["writer", "private-reviewer"]') + '\ninputs=["jobs-allowed"]\n')
		f.department = departments.load_departments()["docs"]
		patch = mock.patch.object(private_provider, "call", side_effect=f.fake)
		patch.start()
		self.addCleanup(patch.stop)
		identity, task, previous = self.failed()
		self.assertTrue(previous.request["inputs"])
		self.assertEqual(len(previous.request["inputs"]), 1)
		self.assertEqual(Path(previous.request["inputs"][0]).read_text(), "합성 재시도 허용 입력")
		original = Path(task["request_path"]).read_bytes()
		files = document_retry.fingerprint(previous)
		used = dict(project.load(identity)["used"])
		self.assertGreater(used["calls"], 0)
		details_file = f.hub / "retry-details.txt"
		details_file.write_text("검증된 근거만으로 작성하고 근거 없는 항목은 미확인으로 표시한다")
		details_file.chmod(0o600)
		self.assertEqual(agency_main(["document-retry", identity, "t1", "--plan", "writer",
			"--details-file", str(details_file)]), 0)
		prepared = project.load(identity)
		revision = prepared["tasks"][0]
		self.assertEqual(prepared["used"], used)
		self.assertNotEqual(revision["request_path"], task["request_path"])
		self.assertEqual(Path(task["request_path"]).read_bytes(), original)
		self.assertEqual(revision["document_retries"][0]["files"], files)
		request = document_run.orchestration.read_request(revision["request_path"])["request"]
		self.assertIn("미확인", request["details"])
		self.assertTrue(request["details"].startswith(previous.request["details"] + "\n\n"))
		self.assertEqual(request["inputs"], previous.request["inputs"])
		f.blockers = []
		before = len(f.calls)
		result = runner.run(identity)
		self.assertEqual(result["status"], "막힘")
		current = project.load(identity)
		new_run = document_run.load(current["tasks"][0]["run_id"])
		self.assertNotEqual(new_run.run_id, previous.run_id)
		self.assertEqual(new_run.handoff["document_retry"]["previous_run"], previous.run_id)
		self.assertEqual(new_run.request["inputs"], previous.request["inputs"])
		self.assertEqual(new_run.status, "사용자 입력 필요")
		self.assertFalse(new_run.approval_pending)
		self.assertEqual([call[0] for call in f.calls[before:]], ["writer"])
		self.assertEqual(current["used"]["calls"], used["calls"] + 1)
		runner._usage(identity, new_run)
		self.assertEqual(project.load(identity)["used"], current["used"])
		self.assertEqual(document_retry.fingerprint(previous), files)
		self.assertEqual(Path(task["request_path"]).read_bytes(), original)
		self.assertFalse((project._folder(identity) / "delivery.json").exists())

	def test_retry_rejects_other_owner_and_changed_request(self):
		identity, task, execution = self.failed()
		other = project.new(self.fixture.department, "합성", "합성 요청")["id"]
		before = project.load(identity)
		with self.assertRaises(ValueError):
			document_retry.prepare(other, "t1", ["writer"], details="근거를 구분한다")
		self.assertEqual(project.load(identity), before)
		self.assertNotIn("request_sha256", task)
		path = Path(task["request_path"])
		path.write_bytes(path.read_bytes() + b"\n# changed\n")
		with self.assertRaises(ValueError):
			document_retry.prepare(identity, "t1", ["writer"], details="근거를 구분한다")
		self.assertEqual(project.load(identity), before)

	def test_retry_requires_changed_general_instructions(self):
		identity, task, execution = self.failed()
		before = project.load(identity)
		original = execution.request.get("details", "")
		for details in (None, "", "\x00", original):
			with self.subTest(details=details), self.assertRaises(ValueError):
				document_retry.prepare(identity, "t1", ["writer"], details=details)
		self.assertEqual(project.load(identity), before)
		self.assertFalse(list(project._folder(identity).glob("t1-retry-*.toml")))

	def test_exhausted_document_budget_keeps_budget_reason(self):
		identity = self.fixture.planned()
		with project._edit(identity, "synthetic_budget") as row:
			row["budget"]["calls"] = 3
		result = runner.run(identity)
		self.assertEqual(result["status"], "막힘")
		self.assertIn("예산", result["blocked_reason"])
		self.assertFalse((project._folder(identity) / "delivery.json").exists())

	def test_existing_document_flow_with_usage_remains_compatible(self):
		identity = self.fixture.planned()
		self.assertGreater(project.load(identity)["used"]["calls"], 0)
		self.assertEqual(runner.run(identity)["status"], "검수")


if __name__ == "__main__":
	unittest.main()
