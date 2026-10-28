"""임시 원장과 가짜 공급자로 문서 납품의 전체 경계를 검사한다."""

import io
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agency import acceptance, ai, deliver_cli, delivery, document_run, documents, intake, planner, project, runner, tick
from agency.departments import load_departments
from collector.config import ConfigError, load_plans
from office.projects import read_projects
from orchestrator import flow, prompts, providers
from orchestrator.record import Run
from jobs import materials, storage


class DocumentAgencyTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve() / "hub"
		self.hub.mkdir()
		(self.hub / "plans.toml").write_text('''[[plans]]
id="writer"
tool="codex"
owner="personal"
allow=["personal", "career"]
[[plans]]
id="reviewer"
tool="agy"
owner="personal"
allow=["personal", "career"]
[[plans]]
id="private-reviewer"
tool="claude"
owner="personal"
allow=["personal", "career"]
[[plans]]
id="local"
tool="ollama"
model="local"
owner="personal"
allow=["personal"]
[orchestration]
implementer=["writer"]
reviewer=["reviewer"]
''', encoding="utf-8")
		(self.hub / "departments.toml").write_text('''[[department]]
id="docs"
name="합성 부서"
scope="합성 문서"
deliverable="document"
repos=[]
allow_plans=["writer", "reviewer"]
playbook="sample"
budget={calls=20, minutes=30}
''', encoding="utf-8")
		(self.hub / "redaction").mkdir()
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "synthetic-private-name", "kind": "회사명", "token": "company01"}]}), encoding="utf-8")
		skill = self.hub / "skills/sample"
		skill.mkdir(parents=True)
		(skill / "SKILL.md").write_text("---\nname: sample\ndescription: 합성 절차\n---\n"
			"주어진 자료로 문서를 작성한다.", encoding="utf-8")
		self.spec = {"goal": "합성 문서 작성", "target": "두 문서", "out_of_scope": "외부 게시",
			"done_criteria": ["첫 자료 포함", "둘째 자료 포함"], "budget": {"calls": 20, "minutes": 30}}
		self.calls, self.quota, self.verdict = [], False, "pass"
		self.task_review, self.blockers = "pass", []
		self.long_document = False
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch.object(providers, "call", side_effect=self.fake),
				mock.patch.object(flow, "call", side_effect=self.fake),
				mock.patch("agency.runner.git", side_effect=AssertionError("문서 작업의 Git 호출 금지")),
				mock.patch("orchestrator.branch_apply.apply_to_branch", side_effect=AssertionError("반영 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		self.department = load_departments()["docs"]
	def fake(self, plan, writer, prompt, cwd, schema, timeout):
		self.calls.append((plan.id, writer, prompt, cwd, schema))
		self.assertFalse(writer)
		if schema is intake.SCHEMA:
			data = {"spec": self.spec, "questions": [], "summary": "합성 문서 작성\n두 문서 납품\n외부 게시 제외"}
		elif schema is planner.DOCUMENT_SCHEMA:
			data = {"size": "medium", "tasks": [{"title": f"문서 {number}", "role": "plan",
				"goal": f"합성 문서 {number}", "done": [f"자료 {number} 포함"], "forbidden": [],
				"details": "합성 명세를 따른다", "budget": {"calls": 4, "minutes": 10, "fixes": 1},
				"depends_on": [1] if number == 2 else []} for number in (1, 2)],
				"decision": {"chosen": "문서별 작성", "rejected": "한 문서로 합치기",
					"assumptions": "합성 자료", "verification": "내용 대조"},
				"meeting": {"needed": False, "topic": ""}}
		elif schema is prompts.ANALYST_SCHEMA:
			if self.quota:
				raise providers.ProviderError("exit", "합성 한도 소진")
			number = "2" if "합성 문서 2" in prompt else "1"
			document = "# 납품\n자료 " + number + " 포함\n"
			if self.long_document:
				document += "가" * 70000 + "문서 끝 표시"
			data = {"summary": "합성 문서 작성", "document": document, "blockers": self.blockers}
		elif schema is prompts.REVIEW_SCHEMA:
			if self.long_document:
				self.assertIn("문서 끝 표시", prompt)
			data = {"verdict": self.task_review, "summary": "합성 판정", "findings": []}
		elif schema is acceptance.SCHEMA:
			self.assertIn(plan.id, ("reviewer", "private-reviewer"))
			self.assertIn("자료 1 포함", prompt)
			self.assertIn("자료 2 포함", prompt)
			data = {"summary": "문서 대조", "criteria": [{"criterion": criterion, "verdict": self.verdict,
				"evidence": "납품 문서의 자료를 확인했다"} for criterion in self.spec["done_criteria"]]}
		else:
			self.fail("예상하지 않은 공급자 호출")
		return providers.Reply(data, 0.1, {})
	def planned(self):
		identity = project.new(self.department, "합성 문서", "두 문서를 작성해 주세요")["id"]
		questions = intake.step(identity)["questions"]
		self.assertEqual(len(questions), 1)
		intake.answer(identity, questions[0]["no"], "맞아")
		self.assertEqual(project.load(identity)["status"], "명세 확정")
		planner.plan(identity)
		return identity
	def reviewed(self):
		identity = self.planned()
		self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertTrue(acceptance.review(identity)["passed"])
		return identity
	def test_intake_to_delivery_uses_readonly_analysis_and_preserves_office_status(self):
		identity = self.planned()
		row = project.load(identity)
		self.assertEqual(row["branch"], "")
		for task in row["tasks"]:
			data = tomllib.loads(Path(task["request_path"]).read_text())
			self.assertEqual(data["request"]["kind"], "analysis")
			self.assertEqual(data["request"]["workdir"], str(project._folder(identity) / "work"))
			self.assertTrue({"repo", "owned_paths", "base_branch", "test_command", "commit_message"}.isdisjoint(data["request"]))
		self.assertEqual(runner.run(identity)["status"], "검수")
		row = project.load(identity)
		for task in row["tasks"]:
			path = documents.path(identity, task["id"])
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			execution = Run.load(task["run_id"])
			self.assertEqual((execution.kind, execution.branch, execution.status), ("analysis", "", "완료"))
			self.assertFalse((execution.folder / "document.md").exists())
			self.assertEqual(path.read_text(), execution.document)
			self.assertEqual([call["plan"] for call in execution.calls], ["writer", "reviewer"])
		self.assertTrue(acceptance.review(identity)["passed"])
		bundle = delivery.load_bundle(identity)
		self.assertEqual(len(bundle["documents"]), 2)
		self.assertTrue(all(len(item["sha256"]) == 64 for item in bundle["documents"]))
		self.assertEqual(read_projects()["items"][0]["status"], "납품 대기")
		output = io.StringIO()
		with redirect_stdout(output):
			self.assertEqual(deliver_cli.main(["deliver", identity]), 0)
		self.assertIn(bundle["documents"][0]["path"], output.getvalue())
		result = delivery.approve(identity, bundle["hash"])
		self.assertEqual(result["status"], "완료")
		self.assertEqual(result["documents"], [item["path"] for item in bundle["documents"]])
		self.assertEqual(read_projects()["items"][0]["status"], "완료")
		self.assertTrue(all(not writer for _, writer, _, _, _ in self.calls))
		second_prompt = next(prompt for _, _, prompt, _, schema in self.calls
			if schema is prompts.ANALYST_SCHEMA and "목표: 합성 문서 2" in prompt)
		self.assertIn("자료 1 포함", second_prompt)
		self.assertTrue(all(not str(cwd).startswith(str(project._folder(identity)))
			for plan, _, _, cwd, _ in self.calls if plan == "reviewer"))
	def test_fail_and_unverified_do_not_create_approval(self):
		for verdict in ("fail", "unverified"):
			with self.subTest(verdict=verdict):
				identity = self.planned()
				self.assertEqual(runner.run(identity)["status"], "검수")
				self.verdict = verdict
				result = acceptance.review(identity)
				self.assertEqual((result["status"], result["passed"]), ("막힘", False))
				self.assertEqual(result["failed"], self.spec["done_criteria"])
				self.assertFalse((project._folder(identity) / "delivery.json").exists())
	def test_task_blockers_and_review_changes_never_reach_project_review(self):
		for reason in ("blockers", "changes"):
			with self.subTest(reason=reason):
				identity = self.planned()
				self.blockers = ["확인할 자료가 없다"] if reason == "blockers" else []
				self.task_review = "changes" if reason == "changes" else "pass"
				result = runner.run(identity)
				self.assertEqual(result["status"], "막힘")
				self.assertFalse(documents.path(identity, "t1").exists())
	def test_long_document_is_sent_in_full_to_task_reviewer_and_on_fix(self):
		identity = self.planned()
		self.long_document = True
		self.task_review = "changes"
		self.assertEqual(runner.run(identity)["status"], "막힘")
		fixes = [prompt for _, _, prompt, _, schema in self.calls
			if schema is prompts.ANALYST_SCHEMA and "직전 문서 시작" in prompt]
		self.assertEqual(len(fixes), 1)
		self.assertIn("문서 끝 표시", fixes[0])
	def test_tick_progresses_document_project_and_hooks_observe_changes(self):
		identity = project.new(self.department, "합성 문서", "두 문서를 작성해 주세요")["id"]
		with mock.patch("agency.hooks.changed") as changed:
			self.assertEqual(tick._advance(project.load(identity))["action"], "intake")
			question = intake._questions(identity)["open"][0]
			intake.answer(identity, question["no"], "맞아")
			for expected in ("plan", "run", "review"):
				self.assertEqual(tick._advance(project.load(identity))["action"], expected)
			self.assertEqual(project.load(identity)["status"], "납품 대기")
			self.assertTrue(any(call.args[2] == "납품 대기" for call in changed.call_args_list))
	def test_document_or_bundle_change_rejects_approval(self):
		identity = self.reviewed()
		bundle = delivery.load_bundle(identity)
		path = documents.path(identity, "t1")
		original = path.read_bytes()
		path.write_text("검수 뒤 변경", encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "문서.*바뀌"):
			delivery.approve(identity, bundle["hash"])
		self.assertEqual(project.load(identity)["status"], "납품 대기")
		path.write_bytes(original)
		with self.assertRaises(ValueError):
			delivery.approve(identity, "0" * 64)
		self.assertEqual(project.load(identity)["status"], "납품 대기")
	def test_change_during_review_or_approval_is_blocked(self):
		identity = self.planned()
		self.assertEqual(runner.run(identity)["status"], "검수")
		original = ai.ask
		def change_after_review(*args, **kwargs):
			result = original(*args, **kwargs)
			documents.path(identity, "t1").write_text("판정 중 변경", encoding="utf-8")
			return result
		with mock.patch.object(ai, "ask", side_effect=change_after_review):
			result = acceptance.review(identity)
		self.assertEqual((result["passed"], result["status"]), (False, "막힘"))
		identity = self.reviewed()
		bundle = delivery.load_bundle(identity)
		append = delivery._append
		def change_after_approval(*args, **kwargs):
			append(*args, **kwargs)
			documents.path(identity, "t1").write_text("승인 중 변경", encoding="utf-8")
		with mock.patch.object(delivery, "_append", side_effect=change_after_approval):
			self.assertEqual(delivery.approve(identity, bundle["hash"])["status"], "막힘")
		self.assertEqual(project.load(identity)["status"], "막힘")
	def test_same_model_under_another_plan_is_not_an_independent_reviewer(self):
		plans_path = self.hub / "plans.toml"
		plans_path.write_text(plans_path.read_text().replace('[orchestration]', '''[[plans]]
id="clone"
tool="codex"
owner="personal"
allow=["personal"]
[orchestration]''').replace('reviewer=["reviewer"]', 'reviewer=["clone", "reviewer"]'))
		config = self.hub / "departments.toml"
		config.write_text(config.read_text().replace('allow_plans=["writer", "reviewer"]',
			'allow_plans=["writer", "clone", "reviewer"]'))
		self.department = load_departments()["docs"]
		identity = self.reviewed()
		self.assertTrue(delivery.load_bundle(identity))
		self.assertNotIn("clone", [call[0] for call in self.calls])
		writer = load_plans().plans["writer"]
		calls = [{"role": "implement", "ok": True, "plan": "writer", "tool": "codex", "model": "model-a"}]
		candidates = [replace(writer, id="a", model="model-a"), replace(writer, id="b", model="model-b")]
		self.assertEqual([plan.id for plan in document_run.independent(candidates, calls)], ["b"])

	def test_quota_retry_preserves_old_run_and_does_not_duplicate_usage(self):
		identity = self.planned()
		self.quota = True
		result = runner.run(identity)
		self.assertEqual(result["status"], "막힘")
		self.assertIn("한도 소진", result["blocked_reason"])
		row = project.load(identity)
		old = row["tasks"][0]["run_id"]
		self.assertEqual(row["tasks"][0]["status"], "대기")
		self.quota = False
		self.assertEqual(runner.run(identity)["status"], "검수")
		row = project.load(identity)
		self.assertNotEqual(row["tasks"][0]["run_id"], old)
		self.assertEqual(Run.load(old).status, "실패")
		self.assertEqual(row["used"]["calls"], 7)

	def test_interrupted_execution_is_linked_before_call_and_resumed_once(self):
		identity = self.planned()
		with mock.patch.object(document_run.DocumentFlow, "run_all", side_effect=KeyboardInterrupt):
			with self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
		row = project.load(identity)
		run_id = row["tasks"][0]["run_id"]
		self.assertTrue(run_id)
		self.assertEqual(row["tasks"][0]["status"], "실행 중")
		self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(project.load(identity)["tasks"][0]["run_id"], run_id)
		self.assertEqual(len(list((self.hub / "runs").iterdir())), 2)

	def test_failed_report_write_keeps_run_for_retry_without_another_call(self):
		identity = self.planned()
		with mock.patch.object(document_run, "private_write", side_effect=OSError("합성 실패")):
			self.assertEqual(runner.run(identity)["status"], "막힘")
		row = project.load(identity)
		run_id = row["tasks"][0]["run_id"]
		self.assertTrue(run_id)
		self.assertEqual(Run.load(run_id).status, "완료")
		self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(project.load(identity)["tasks"][0]["run_id"], run_id)
		self.assertEqual(sum(schema is prompts.ANALYST_SCHEMA for _, _, _, _, schema in self.calls), 2)

	def test_resume_rechecks_run_and_task_ownership_under_the_run_lock(self):
		identity = self.planned()
		with mock.patch.object(document_run.DocumentFlow, "run_all", side_effect=KeyboardInterrupt):
			with self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
		task = project.load(identity)["tasks"][0]
		execution = document_run.load(task["run_id"])
		workdir = project._folder(identity) / "work"
		check = document_run.check
		def finish_between_checks(*args):
			check(*args)
			if finish_between_checks.count == 0:
				latest = document_run.DocumentRun.load(execution.run_id)
				latest.finish("완료", "합성 완료")
			finish_between_checks.count += 1
		finish_between_checks.count = 0
		with mock.patch.object(document_run, "check", side_effect=finish_between_checks), \
				mock.patch.object(document_run.DocumentFlow, "run_all") as execute:
			result = document_run.resume(identity, task, execution, self.department, workdir)
		self.assertEqual(result.status, "완료")
		execute.assert_not_called()

	def test_workdir_change_is_rejected_before_any_task_provider_call(self):
		identity = self.planned()
		request = Path(project.load(identity)["tasks"][0]["request_path"])
		text = request.read_text()
		request.write_text(text.replace(str(project._folder(identity) / "work"), str(self.hub)))
		before = len(self.calls)
		with self.assertRaisesRegex(ValueError, "읽기 전용 작업 폴더"):
			runner.run(identity)
		self.assertEqual(len(self.calls), before)

	def test_budget_and_unavailable_independent_reviewer_block(self):
		identity = self.planned()
		with project._edit(identity, "합성 예산") as row:
			row["budget"]["calls"] = 3
		self.assertIn("예산", runner.run(identity)["blocked_reason"])
		identity = self.planned()
		self.assertEqual(runner.run(identity)["status"], "검수")
		with mock.patch("agency.ai.review_plans", return_value=[]):
			result = acceptance.review(identity)
		self.assertFalse(result["passed"])
		self.assertIn("구현에 쓰지 않은", project.load(identity)["blocked_reason"])

	def test_existing_document_is_not_overwritten_and_symlinks_are_rejected(self):
		identity = self.planned()
		path = documents.path(identity, "t1")
		path.parent.mkdir()
		path.write_text("기존 문서")
		self.assertEqual(runner.run(identity)["status"], "막힘")
		self.assertEqual(path.read_text(), "기존 문서")
		identity = self.planned()
		path = documents.path(identity, "t1")
		outside = self.hub / "outside"
		outside.write_text("보존")
		path.parent.mkdir()
		path.symlink_to(outside)
		with self.assertRaises(ValueError):
			documents.save(identity, "t1", "다른 문서")
		self.assertEqual(outside.read_text(), "보존")

	def test_changed_dependency_is_not_passed_to_the_next_task(self):
		identity = self.planned()
		save = documents.save
		def alter_dependency(*args, **kwargs):
			result = save(*args, **kwargs)
			if args[1] == "t1":
				documents.path(identity, "t1").write_text("변경된 선행 문서", encoding="utf-8")
			return result
		with mock.patch.object(documents, "save", side_effect=alter_dependency):
			result = runner.run(identity)
		self.assertEqual(result["status"], "막힘")
		self.assertIn("선행 납품 문서", result["blocked_reason"])
		self.assertFalse(documents.path(identity, "t2").exists())

	def test_local_only_department_never_calls_external_provider(self):
		config = self.hub / "departments.toml"
		config.write_text(config.read_text().replace('allow_plans=["writer", "reviewer"]',
			'allow_plans=["local"]\nexternal_ai=false'))
		department = load_departments()["docs"]
		identity = project.new(department, "합성", "합성")["id"]
		with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
			intake.step(identity)
		project.update_spec(identity, **self.spec)
		project.confirm_spec(identity, department)
		with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
			planner.plan(identity)
		with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
			runner.run(identity)
		self.assertEqual(tick._advance(project.load(identity))["action"], "skip")
		project.transition(identity, "계획")
		request = project._folder(identity) / "task.toml"
		request.write_text("합성 요청")
		project.add_task(identity, "합성 작업", str(request))
		project.transition(identity, "실행 중")
		project.update_task(identity, "t1", status="완료")
		project.transition(identity, "검수")
		with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
			acceptance.review(identity)
		self.assertEqual(self.calls, [])
		config.write_text(config.read_text().replace('allow_plans=["local"]', 'allow_plans=["writer"]'))
		with self.assertRaises(ConfigError):
			load_departments()

	def test_prepared_and_allowed_inputs_reach_document_requests_and_fake_provider(self):
		with storage.transaction() as root:
			old, _ = materials.add(root, "합성 이전 입력")
			materials.permission(root, old["id"], True)
			latest, _ = materials.add(root, "합성 최신 입력", previous=old["id"])
			materials.permission(root, latest["id"], True)
			denied, _ = materials.add(root, "합성 비허용 입력")
		config = self.hub / "departments.toml"
		config.write_text(config.read_text().replace('allow_plans=["writer", "reviewer"]',
			'allow_plans=["writer", "private-reviewer"]') + "\nprepare=[[" + json.dumps(sys.executable)
			+ ', "-c", "print(123)"]]\ninputs=["jobs-allowed"]\n')
		self.department = load_departments()["docs"]
		identity = self.planned()
		paths = [str(project._folder(identity) / "inputs/prepare-1.txt"),
			str(project._folder(identity) / "inputs" / ("materials-" + latest["id"] + ".md"))]
		for task in project.load(identity)["tasks"]:
			request = tomllib.loads(Path(task["request_path"]).read_text())["request"]
			self.assertEqual(request["inputs"], paths)
		for value in (old["id"], denied["id"]):
			self.assertFalse((project._folder(identity) / "inputs" / ("materials-" + value + ".md")).exists())
		def locked_fake(*args, **kwargs):
			if kwargs:
				args = (args[0], *(kwargs[key] for key in ("writer", "prompt", "cwd", "schema", "timeout")))
			with self.assertRaises(ConfigError):
				with storage.transaction():
					pass
			reply = self.fake(*args)
			if args[4] is acceptance.SCHEMA:
				reply.data["summary"] = "합성 최신 입력"
				for item in reply.data["criteria"]:
					item["evidence"] = "합성 최신 입력"
			if args[4] is prompts.REVIEW_SCHEMA:
				reply.data["summary"] = "합성 최신 입력"
			return reply
		with mock.patch("agency.private_provider.call", side_effect=locked_fake):
			self.assertEqual(runner.run(identity)["status"], "검수")
			self.assertTrue(acceptance.review(identity)["passed"])
		report = (project._folder(identity) / "report.md").read_text()
		self.assertNotIn("합성 최신 입력", report)
		self.assertNotIn("합성 최신 입력", (project._folder(identity) / "delivery.json").read_text())
		with storage.transaction() as root:
			materials.permission(root, latest["id"], False)
		project.transition(identity, "실행 중")
		project.transition(identity, "검수")
		with mock.patch("agency.private_provider.call") as call:
			self.assertEqual(acceptance.review(identity)["status"], "막힘")
			call.assert_not_called()
		for task in project.load(identity)["tasks"]:
			execution = Run.load(task["run_id"])
			self.assertEqual(execution.request["inputs"], paths)
			report = (execution.folder / "report.md").read_text()
			self.assertNotIn("합성 최신 입력", (execution.folder / "review-0.json").read_text())
			self.assertIn("입력 자료: 2건", report)
			self.assertNotIn("합성 최신 입력", report)
			self.assertNotIn("123", report)
		prompts_used = [prompt for _, _, prompt, _, schema in self.calls if schema is prompts.ANALYST_SCHEMA]
		self.assertTrue(all(all(path in prompt for path in paths) for prompt in prompts_used))

	def test_prepare_failure_blocks_plan_and_resume_does_not_repeat_success(self):
		counter, ready = self.hub / "counter", self.hub / "ready"
		command = [sys.executable, "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
			"p.write_text((p.read_text() if p.exists() else '')+'x'); print('ok')", str(counter)]
		failure = [sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 1)", str(ready)]
		config = self.hub / "departments.toml"
		config.write_text(config.read_text() + "\nprepare=" + json.dumps([command, failure]) + "\n")
		self.department = load_departments()["docs"]
		identity = project.new(self.department, "합성", "합성")["id"]
		question = intake.step(identity)["questions"][0]
		intake.answer(identity, question["no"], "맞아")
		with self.assertRaisesRegex(ValueError, "2번 실패"):
			planner.plan(identity)
		self.assertEqual(project.load(identity)["status"], "막힘")
		ready.touch()
		project.resume(identity)
		planner.plan(identity)
		self.assertEqual(counter.read_text(), "x")

	def test_revoked_material_blocks_execution_before_fake_provider(self):
		with storage.transaction() as root:
			item, _ = materials.add(root, "합성 허용 자료")
			materials.permission(root, item["id"], True)
		config = self.hub / "departments.toml"
		config.write_text(config.read_text().replace('allow_plans=["writer", "reviewer"]',
			'allow_plans=["writer", "private-reviewer"]') + '\ninputs=["jobs-allowed"]\n')
		self.department = load_departments()["docs"]
		identity = self.planned()
		with storage.transaction() as root:
			materials.permission(root, item["id"], False)
		with mock.patch("agency.private_provider.call") as call:
			self.assertEqual(runner.run(identity)["status"], "막힘")
			self.assertEqual(runner.run(identity)["status"], "막힘")
			self.assertEqual(project.load(identity)["blocked_from"], "실행 중")
			call.assert_not_called()
