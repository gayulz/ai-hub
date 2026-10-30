"""합성 자료로 비공개 문서의 기록과 요금제 경계를 확인한다."""

import os
import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agency import departments, document_run, private_provider, project, runner
from collector.config import ConfigError
from orchestrator import flow, providers


class PrivateDocumentTest(unittest.TestCase):
	def test_review_saves_no_private_summary_or_finding(self):
		with tempfile.TemporaryDirectory() as temporary:
			home = str(Path(temporary).resolve())
			with mock.patch.dict(os.environ, {"AI_HUB_HOME": home}):
				self.check_private_review(home)

	def check_private_review(self, home):
		marker = "합성 비공개 자료 표시"
		input_path = Path(home) / "input.txt"
		input_path.write_text("합성 입력")
		input_path.chmod(0o600)
		run = document_run.DocumentRun("2099-01-01-001", {"goal": "합성", "done": ["완료"],
			"inputs": [str(input_path)]}, home, kind="analysis", worktree=home,
			step="review", document="합성 납품", checks=[{"name": "문서", "ok": True}])
		run.save()
		worker = document_run.DocumentFlow(run, None, {"reviewer": []})
		finding = {"id": marker, "severity": "major", "file": marker, "line": 1,
			"problem": marker, "evidence": marker, "suggestion": marker}
		answer = {"verdict": "changes", "summary": marker, "findings": [finding]}
		with mock.patch.object(worker, "_review_isolation_required", return_value=False), \
				mock.patch.object(flow, "role_plans", return_value=[]), \
				mock.patch.object(flow.Flow, "ask", return_value=answer):
			worker.review()
		self.assertEqual(run.step, "fix")
		for name in ("run.json", "review-0.json", "events.jsonl"):
			self.assertNotIn(marker, (run.folder / name).read_text())
		self.assertEqual(worker._private_findings[0]["problem"], marker)
		reloaded = document_run.DocumentFlow(document_run.DocumentRun(**vars(document_run.load(run.run_id))),
			None, {"reviewer": []})
		with mock.patch.object(reloaded, "ask", side_effect=AssertionError("검토 전에 보완 금지")):
			reloaded.fix()
		self.assertEqual(reloaded.run.step, "review")
		with mock.patch.object(document_run.prompts, "fix", return_value="합성 보완") as prompt, \
				mock.patch.object(worker, "_prompt", return_value="합성 요청"), \
				mock.patch.object(worker, "ask", return_value={"document": "수정 문서", "blockers": []}):
			worker.fix()
		self.assertEqual(prompt.call_args.args[2][0]["problem"], marker)
		self.assertNotIn(marker, (run.folder / "run.json").read_text())

	def test_career_input_rejects_unapproved_plan(self):
		row = {"id": "docs", "name": "합성", "scope": "합성", "deliverable": "document", "repos": [],
			"allow_plans": ["personal"], "external_ai": True, "playbook": "sample",
			"budget": {"calls": 5, "minutes": 5}, "inputs": ["jobs-allowed"]}
		plans = {"personal": SimpleNamespace(tool="codex", allow=("personal",))}
		with self.assertRaisesRegex(ConfigError, "career"):
			departments._department(row, 1, {}, plans)
		plans["personal"].allow = ("personal", "career")
		self.assertEqual(departments._department(row, 1, {}, plans).inputs, ("jobs-allowed",))

	def test_private_document_is_separate_from_run_record_and_detects_tampering(self):
		with tempfile.TemporaryDirectory() as temporary:
			home = str(Path(temporary).resolve())
			with mock.patch.dict(os.environ, {"AI_HUB_HOME": home}):
				marker = "합성 원문이 들어간 납품 문서"
				run = document_run.DocumentRun("2099-01-01-002", {"inputs": ["/synthetic/input"]}, home,
					kind="analysis", document=marker)
				run.save()
				self.assertNotIn(marker, (run.folder / "run.json").read_text())
				self.assertEqual(document_run.load(run.run_id).document, marker)
				department = SimpleNamespace(id="docs", budget={"calls": 5, "minutes": 5}, deliverable="document")
				identity = project.new(department, "합성", "합성 요청")["id"]
				self.assertEqual(runner._usage(identity, run).document, marker)
				self.assertNotIn(marker, (run.folder / "run.json").read_text())
				path = next((run.folder / "documents").iterdir())
				path.write_text("변조")
				with self.assertRaises(ValueError):
					document_run.load(run.run_id)

	def test_private_call_disables_file_tools_and_rejects_unrestricted_provider(self):
		plan = SimpleNamespace(id="safe", tool="codex", model=None)
		result = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
		with tempfile.TemporaryDirectory() as directory, \
				mock.patch.object(providers, "_codex", return_value=(["codex", "-"], True)), \
				mock.patch.object(providers, "_unwrap", return_value=({}, {})), \
				mock.patch.object(private_provider.readonly_guard, "snapshot", return_value={}), \
				mock.patch.object(private_provider.readonly_guard, "verify"), \
				mock.patch.object(private_provider.subprocess, "run", return_value=result) as call:
			private_provider.call(plan, False, "합성 입력", Path(directory), {}, 30)
			argv = call.call_args.args[0]
			self.assertIn("--ignore-user-config", argv)
			self.assertIn("shell_tool", argv)
			self.assertIn("web_search=\"disabled\"", argv)
			plan.tool = "agy"
			with self.assertRaises(providers.ProviderError):
				private_provider.call(plan, False, "합성 입력", Path(directory), {}, 30)
			self.assertEqual(call.call_count, 1)

	def test_verified_input_contents_are_in_prompt_without_other_files(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			allowed, denied = root / "allowed.txt", root / "denied.txt"
			allowed.write_text("합성 허용 내용")
			allowed.chmod(0o600)
			denied.write_text("합성 비허용 내용")
			denied.chmod(0o600)
			run = document_run.DocumentRun("2099-01-01-003", {"inputs": [str(allowed)]}, str(root), kind="analysis")
			worker = document_run.DocumentFlow(run, None, {})
			with mock.patch.object(flow, "role_plans", return_value=[]), \
					mock.patch.object(flow.Flow, "ask", return_value={}) as ask:
				worker.ask("implement", "합성 요청", {}, False)
			prompt = ask.call_args.args[1]
			self.assertIn("합성 허용 내용", prompt)
			self.assertNotIn("합성 비허용 내용", prompt)

	def test_private_configuration_uses_only_restricted_independent_plans(self):
		plans = {"writer": SimpleNamespace(id="writer", tool="codex", model=None, allow=("personal", "career")),
			"unsafe": SimpleNamespace(id="unsafe", tool="agy", model=None, allow=("personal", "career")),
			"reviewer": SimpleNamespace(id="reviewer", tool="claude", model=None, allow=("personal", "career"))}
		department = SimpleNamespace(external_ai=True, allow_plans=("writer", "unsafe", "reviewer"),
			prepare=(), inputs=("jobs-allowed",))
		with mock.patch.object(document_run, "load_plans", return_value=SimpleNamespace(plans=plans)), \
				mock.patch.object(document_run.orchestration, "orchestration_config",
					return_value={"implementer": ["writer"], "reviewer": ["unsafe"]}):
			config = document_run._configuration(department)
			self.assertEqual(config["implementer"], ["writer"])
			self.assertNotIn("unsafe", config["reviewer"])
			self.assertIn("reviewer", config["reviewer"])
			department.allow_plans = ("writer", "unsafe")
			with self.assertRaisesRegex(ValueError, "독립"):
				document_run._configuration(department)


if __name__ == "__main__":
	unittest.main()
