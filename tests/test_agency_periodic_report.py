"""코드 프로젝트에서 명시된 보고와 코드 작업의 경계를 확인한다."""
import copy
import hashlib
import io
import json
import os
import platform
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import test_agency_runner as fixtures
from agency import acceptance, ai, delivery, document_review, document_run, documents, planner, private_provider, project, runner, periodic_results
from office import agency as agency_actions
from orchestrator import __main__ as orchestration, draft, flow, providers, targets
from orchestrator.record import Run, new_run_id
from test_agency_planner import sample
class PeriodicPlanTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.target = targets.Target("synthetic", self.root / "repo", "python3 -m unittest")
		self.rules = SimpleNamespace(kinds=lambda value: [])
		self.work = self.root / "project" / "work"
	def response(self):
		data = sample()
		data["tasks"][0].update(task_kind="report", report_checks=["tests"], owned_paths=[], commit_message="")
		data["tasks"][1].update(task_kind="code", report_checks=[])
		return data
	def test_report_and_code_keep_dependency_and_distinct_boundaries(self):
		requests = planner._requests(self.response(), self.rules, self.target, "agency/synthetic", self.work,
			allow_reports=True)
		report, code = (item["request"] for item in requests)
		self.assertEqual((report["kind"], report["workdir"]), ("analysis", str(self.work)))
		self.assertEqual((report["report_task"], report["report_checks"]), ("t1", ["tests"]))
		self.assertFalse({"repo", "base_branch", "owned_paths", "test_command"} & report.keys())
		self.assertEqual((code["kind"], code["repo"], code["base_branch"]),
			("code", str(self.target.path), "agency/synthetic"))
		self.assertEqual(code["test_command"], self.target.test_command)
		self.assertEqual(code["report_dependencies"], ["t1"])
		self.assertEqual(self.response()["tasks"][1]["depends_on"], [1])
	def test_report_kind_must_be_explicitly_enabled_and_cannot_write(self):
		data = self.response()
		with self.assertRaises(ValueError):
			planner._requests(data, self.rules, self.target, "agency/synthetic", self.work)
		for field, value in (("owned_paths", ["src/a.py"]), ("commit_message", "임의 커밋"),
				("task_kind", "unknown"), ("workdir", "/untrusted"), ("report_checks", []),
				("report_checks", ["tests", "tests"]), ("report_checks", ["untrusted"])):
			mutated = copy.deepcopy(data)
			mutated["tasks"][0][field] = value
			with self.subTest(field=field), self.assertRaises(ValueError):
				planner._requests(mutated, self.rules, self.target, "agency/synthetic", self.work,
					allow_reports=True)
	def test_ordinary_code_plan_keeps_existing_format(self):
		requests = planner._requests(sample(), self.rules, self.target, "agency/synthetic", self.work)
		self.assertTrue(all(item["request"]["kind"] == "code" for item in requests))
		self.assertFalse(planner._report_enabled({"schedule_repo": "synthetic"}, self.target))
		self.assertFalse(planner._report_enabled({"schedule_repo": "synthetic", "audit_report_mode": True}, self.target))
		self.assertTrue(planner._report_enabled({"schedule_repo": "synthetic", "audit_report_mode": "explicit-v1"}, self.target))
class MixedRunnerTest(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.RunnerTest(methodName="runTest")
		self.fixture.setUp()
		self.addCleanup(self.fixture.doCleanups)
	def pin_tasks(self, identity):
		row = project.load(identity)
		for task in row["tasks"]:
			Path(task["request_path"]).chmod(0o600)
		with project._edit(identity, "synthetic_request_hashes") as current:
			for task in current["tasks"]:
				task["request_sha256"] = hashlib.sha256(Path(task["request_path"]).read_bytes()).hexdigest()
	def test_report_is_stored_before_dependent_code_without_changing_main(self):
		case = self.fixture
		row = case.new(2)
		identity = row["id"]
		with project._edit(identity, "synthetic_report_mode") as current:
			current["schedule_repo"] = "sample"
			current["audit_report_mode"] = "explicit-v1"
		workdir = project._folder(identity) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 점검", "done": ["근거와 미검증을 보고"], "forbidden": [], "details": "합성 자료",
			"report_mode": "periodic-v1", "report_project": identity, "report_task": "t1", "report_checks": ["tests"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		Path(row["tasks"][0]["request_path"]).write_text(draft.serialize(request), encoding="utf-8")
		Path(row["tasks"][0]["request_path"]).chmod(0o600)
		code_path = Path(row["tasks"][1]["request_path"])
		with code_path.open("a", encoding="utf-8") as stream:
			stream.write("\nreport_project=" + json.dumps(identity) + "\nreport_task=\"t2\"\nreport_dependencies=[\"t1\"]\n")
		self.pin_tasks(identity)
		def fake_report(project_id, task, department, target):
			self.assertEqual(project_id, identity)
			self.assertEqual(target, workdir)
			target.mkdir(mode=0o700, exist_ok=True)
			execution = document_run.DocumentRun(new_run_id(), request["request"], str(target),
				kind="analysis", worktree=str(target), step="review")
			shutil.copyfile(task["request_path"], execution.folder / "request.toml")
			(execution.folder / "request.toml").chmod(0o600)
			execution.save()
			documents.link_periodic_run(identity, task["id"], execution.run_id)
			execution.document = documents.render_evidence(documents.read_evidence(identity, task))
			execution.handoff["periodic_evidence_sha256"] = task["report_evidence"]["sha256"]
			execution.calls = [{"role": "review", "plan": "reviewer", "tool": "claude", "ok": True, "seconds": 1}]
			execution.checks = [{"name": "문서", "ok": True, "detail": "합성"}]
			review = {"verdict": "pass", "findings": []}
			documents.pin_review(execution, "합성 검토", review)
			documents.save_review(execution, review)
			execution.finish("완료", "합성 검토 통과")
			execution.handoff["periodic_report_seal"] = document_run.report_seal(execution)
			execution.save()
			case.order.append("report")
			return execution
		before = case.snapshot()
		with mock.patch.object(document_run, "start", side_effect=fake_report), \
				mock.patch.object(periodic_results, "prepare", wraps=periodic_results.prepare) as prepare:
			result = runner.run(identity)
		prepare.assert_called_once()
		self.assertEqual(prepare.call_args.args[0], identity)
		self.assertEqual(result["status"], "검수")
		self.assertEqual(case.order, ["report", "t2"])
		self.assertEqual(case.snapshot(), before)
		current = project.load(identity)
		self.assertEqual([task["status"] for task in current["tasks"]], ["완료", "완료"])
		self.assertEqual(current["tasks"][0]["commit"], "")
		self.assertEqual(document_run.verified_report(identity, current["tasks"][0]),
			documents.render_evidence(documents.read_evidence(identity, current["tasks"][0])))
		self.assertTrue(current["tasks"][1]["commit"])
		self.assertIn("## 미검증", documents.read(identity, "t1").decode())
		criteria = [{"criterion": item, "verdict": "pass", "evidence": "합성 결과"}
			for item in current["spec"]["done_criteria"]]
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text(encoding="utf-8").replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		answer = ai.Answer({"criteria": criteria, "summary": "합성 검수 통과"}, "reviewer", 0.1)
		with mock.patch.object(acceptance, "_test", return_value={"command": "합성 시험", "ok": True, "summary": "통과"}), \
				mock.patch.object(ai, "ask", return_value=answer):
			self.assertEqual(acceptance.review(identity)["status"], "납품 대기")
		bundle = delivery.load_bundle(identity)
		self.assertEqual(bundle["periodic"]["base"], case.head)
		self.assertEqual(bundle["periodic"]["reports"], documents.periodic_manifest(project.load(identity)))
		with project._edit(identity, "synthetic_tamper_dependency") as altered:
			altered["tasks"][1]["depends_on"] = []
		with self.assertRaises(ValueError):
			delivery.load_bundle(identity)
		with project._edit(identity, "synthetic_restore_dependency") as restored:
			restored["tasks"][1]["depends_on"] = ["t1"]
		file = documents.path(identity, "t1")
		file.write_text("# 합성 변조\n", encoding="utf-8")
		with self.assertRaises(ValueError):
			delivery.load_bundle(identity)
	def test_periodic_file_limit_blocks_candidate_before_project_branch_apply(self):
		case, row = self.fixture, self.fixture.new()
		identity = row["id"]
		with project._edit(identity, "synthetic_limit") as current:
			current["schedule_repo"], current["audit_report_mode"] = "sample", "explicit-v1"
			current["spec"]["goal"] = "변경 파일 1개 이내"
			current["spec_hash"] = hashlib.sha256(json.dumps(current["spec"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
			path = Path(current["tasks"][0]["request_path"]); path.chmod(0o600)
			current["tasks"][0]["request_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
		def two_files(path, *, on_created=None):
			execution = case.start(path) if on_created is None else case.start(path, on_created=on_created)
			work = Path(execution.worktree); (work / "extra.py").write_text("# synthetic\n", encoding="utf-8")
			fixtures.checks.git(work, "add", "--", "extra.py")
			fixtures.checks.git(work, "commit", "-q", "-m", "합성 두 번째 파일")
			return execution
		with mock.patch.object(orchestration, "start_run", side_effect=two_files), \
				mock.patch.object(runner, "apply_to_branch") as apply:
			result = runner.run(identity)
		self.assertEqual(result["status"], "막힘"); self.assertIn("변경 파일 2개", result["blocked_reason"])
		apply.assert_not_called()
	def test_unverified_machine_evidence_yields_reviewed_report_without_a_writer(self):
		case = self.fixture
		row = case.new()
		identity = row["id"]
		with project._edit(identity, "synthetic_report_mode") as current:
			current["schedule_repo"] = "sample"
			current["audit_report_mode"] = "explicit-v1"
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text(encoding="utf-8").replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		workdir = project._folder(identity) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 시험 보고", "done": ["시험 미실행을 명확히 보고"], "forbidden": [], "details": "합성",
			"report_mode": "periodic-v1", "report_project": identity, "report_task": "t1", "report_checks": ["tests"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		request_path = Path(row["tasks"][0]["request_path"])
		request_path.write_text(draft.serialize(request), encoding="utf-8")
		request_path.chmod(0o600)
		self.pin_tasks(identity)
		def review(plan, writer, prompt, cwd, schema, timeout):
			self.assertFalse(writer)
			self.assertNotEqual(cwd, case.repo)
			self.assertIn("미검증", prompt)
			return providers.Reply({"verdict": "pass", "summary": "미검증을 정직하게 보고함", "findings": []}, .2, {})
		with mock.patch("orchestrator.test_isolation.requires", return_value=True), \
				mock.patch.object(private_provider, "call", side_effect=review) as reviewer:
			result = runner.run(identity)
		self.assertEqual(result["status"], "검수")
		reviewer.assert_called_once()
		task = project.load(identity)["tasks"][0]
		self.assertEqual(project.load(identity)["audit_base"], case.head)
		self.assertEqual(task["commit"], "")
		report = document_run.verified_report(identity, task)
		self.assertIn("## 미검증", report)
		self.assertIn("실행하지 않았습니다", report)
		self.assertNotIn("통과", report)
	def test_unmarked_code_project_rejects_report_request(self):
		case = self.fixture
		row = case.new()
		workdir = project._folder(row["id"]) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 점검", "done": ["근거"], "forbidden": [], "details": "합성"},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		Path(row["tasks"][0]["request_path"]).write_text(draft.serialize(request), encoding="utf-8")
		with mock.patch.object(document_run, "start") as start, self.assertRaises(ValueError):
			runner.run(row["id"])
		start.assert_not_called()
	def test_tampered_periodic_request_is_rejected_before_implementation(self):
		case = self.fixture
		row = case.new()
		path = Path(row["tasks"][0]["request_path"])
		path.chmod(0o600)
		with project._edit(row["id"], "synthetic_report_mode") as current:
			current["schedule_repo"] = "sample"
			current["audit_report_mode"] = "explicit-v1"
			current["tasks"][0]["request_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
		path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
		result = runner.run(row["id"])
		self.assertEqual(result["status"], "막힘")
		self.assertIn("요청서", result["blocked_reason"])
		case.start_mock.assert_not_called()
	def test_unsealed_report_blocks_the_dependent_code_task(self):
		case = self.fixture
		row = case.new(2)
		identity = row["id"]
		with project._edit(identity, "synthetic_report_mode") as current:
			current["schedule_repo"] = "sample"
			current["audit_report_mode"] = "explicit-v1"
		workdir = project._folder(identity) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 점검", "done": ["근거"], "forbidden": [], "details": "합성",
			"report_mode": "periodic-v1", "report_project": identity, "report_task": "t1", "report_checks": ["tests"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		Path(row["tasks"][0]["request_path"]).write_text(draft.serialize(request), encoding="utf-8")
		Path(row["tasks"][0]["request_path"]).chmod(0o600)
		self.pin_tasks(identity)
		def fake_report(project_id, task, department, target):
			target.mkdir(mode=0o700, exist_ok=True)
			execution = document_run.DocumentRun(new_run_id(), request["request"], str(target),
				kind="analysis", worktree=str(target), step="review")
			shutil.copyfile(task["request_path"], execution.folder / "request.toml")
			execution.save()
			documents.link_periodic_run(identity, task["id"], execution.run_id)
			execution.document = "## 근거\n합성\n## 미검증\n없음\n"
			execution.finish("완료", "합성 미검증 결과")
			return execution
		with mock.patch.object(document_run, "start", side_effect=fake_report):
			result = runner.run(identity)
		self.assertEqual(result["status"], "막힘")
		self.assertEqual([task["status"] for task in project.load(identity)["tasks"]], ["실패", "대기"])
		case.start_mock.assert_not_called()
	def test_completed_report_anchor_cannot_be_changed_or_reused(self):
		case = self.fixture
		row = case.new()
		seal = {"version": 1, "sha256": "a" * 64}
		evidence = {"version": 1, "sha256": "c" * 64}
		project.update_task(row["id"], "t1", status="실행 중")
		project.update_task(row["id"], "t1", report_evidence=evidence)
		saved = project.update_task(row["id"], "t1", status="완료", commit="", report_seal=seal)
		self.assertEqual(saved["tasks"][0]["report_seal"], seal)
		for changes in ({"report_seal": {"version": 1, "sha256": "b" * 64}, "status": "완료"},
				{"report_evidence": {"version": 1, "sha256": "d" * 64}},
				{"status": "대기"}, {"run_id": "another-run"}):
			with self.subTest(changes=changes), self.assertRaises(ValueError):
				project.update_task(row["id"], "t1", **changes)
		self.assertEqual(project.load(row["id"])["tasks"][0]["report_seal"], seal)
	def test_report_only_delivery_uses_office_document_bundle_without_merge(self):
		case = self.fixture
		row = case.new(count=0, status="접수 중")
		identity = row["id"]
		criterion = "실행하지 않은 시험을 미검증으로 보고한다"
		project.update_spec(identity, goal="합성 점검", target="로컬 보고", out_of_scope="외부 접속",
			done_criteria=[criterion], budget=case.department.budget)
		project.confirm_spec(identity, case.department)
		project.transition(identity, "계획")
		with project._edit(identity, "synthetic_report_mode") as current:
			current["schedule_repo"] = "sample"
			current["audit_report_mode"] = "explicit-v1"
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text(encoding="utf-8").replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		workdir = project._folder(identity) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 시험 보고", "done": [criterion], "forbidden": [], "details": "합성",
			"report_mode": "periodic-v1", "report_project": identity, "report_task": "t1", "report_checks": ["tests"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		path = project._folder(identity) / "t1.toml"
		path.write_text(draft.serialize(request), encoding="utf-8")
		path.chmod(0o600)
		project.add_task(identity, "합성 보고", str(path), request_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
		review = providers.Reply({"verdict": "pass", "summary": "합성 보고 적합", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=review):
			self.assertEqual(runner.run(identity)["status"], "검수")
		answer = ai.Answer({"criteria": [{"criterion": criterion, "verdict": "pass", "evidence": "미검증 표시 확인"}],
			"summary": "합성 보고 확인"}, "reviewer", .1)
		with project._edit(identity, "synthetic_final_budget") as current:
			current["budget"], current["used"] = {"calls": 2, "minutes": 1}, {"calls": 1, "seconds": 20}
		def final_review(*args, **kwargs):
			self.assertLessEqual(kwargs["timeout"], 39)
			self.assertEqual(len(kwargs["plans"]), 1)
			return answer
		with mock.patch.object(document_review.ai, "ask", side_effect=final_review) as final:
			self.assertTrue(acceptance.review(identity)["passed"])
			self.assertIn("점검 미검증 1건", (project._folder(identity) / "report.md").read_text())
			cached = document_review.periodic_call(identity, case.department, final.call_args.args[1],
				final.call_args.args[2], final.call_args.kwargs["plans"])
			self.assertEqual(cached.data, answer.data)
			final.assert_called_once()
		self.assertEqual(project.load(identity)["used"]["calls"], 2)
		bundle = delivery.load_bundle(identity)
		view = agency_actions.detail(identity, "delivery")
		self.assertEqual((bundle["version"], view["kind"], view["actions"]), (2, "document", ["document"]))
		file = documents.path(identity, "t1")
		original = file.read_bytes()
		file.write_bytes(original + b"changed")
		with self.assertRaises(ValueError):
			delivery.load_bundle(identity)
		file.write_bytes(original)
		tree = case.git("rev-parse", row["branch"] + "^{tree}")
		changed = case.git("commit-tree", tree, "-p", case.head, "-m", "합성 변경")
		case.git("update-ref", "refs/heads/" + row["branch"], changed)
		with self.assertRaises(ValueError):
			delivery.load_bundle(identity)
		case.git("update-ref", "refs/heads/" + row["branch"], case.head)
		payload = {"action": "agency_approve", "id": identity, "hash": bundle["hash"]}
		agency_actions.validate(payload)
		with mock.patch.object(delivery, "_merge_test") as merge:
			status, result = agency_actions.execute(case.hub, payload, io.StringIO())
		self.assertEqual((status, result["status"]), (200, "완료"))
		merge.assert_not_called()
		self.assertEqual(case.git("rev-parse", row["branch"]), case.head)
class ReportIntegrityTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.enterContext(mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}))
		self.identity = "p-20261028-111"
		self.folder = project._folder(self.identity)
		self.folder.mkdir(parents=True, mode=0o700)
		self.work = self.folder / "work"
		self.work.mkdir(mode=0o700)
		self.request = {"request": {"kind": "analysis", "workdir": str(self.work), "role": "backend",
			"goal": "합성 보고", "done": ["합성 근거"], "forbidden": [], "details": "합성 자료",
			"report_mode": "periodic-v1", "report_project": self.identity, "report_task": "t1",
			"report_checks": ["tests"]}, "budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		self.request_path = self.folder / "t1.toml"
		self.request_path.write_text(draft.serialize(self.request), encoding="utf-8")
		self.request_path.chmod(0o600)
		self.execution = document_run.DocumentRun(new_run_id(), self.request["request"], str(self.work),
			kind="analysis", worktree=str(self.work), step="review")
		shutil.copyfile(self.request_path, self.execution.folder / "request.toml")
		(self.execution.folder / "request.toml").chmod(0o600)
		self.task = {"id": "t1", "title": "합성", "request_path": str(self.request_path), "depends_on": [],
			"status": "실행 중", "run_id": "", "commit": "",
			"request_sha256": hashlib.sha256(self.request_path.read_bytes()).hexdigest()}
		self.task["report_evidence"] = documents.prepare_evidence(self.identity, self.task)
		self.task["run_id"] = self.execution.run_id
		self.row = {"id": self.identity, "status": "실행 중", "tasks": [self.task], "schedule_repo": "synthetic",
			"audit_report_mode": "explicit-v1"}
		self.enterContext(mock.patch.object(project, "load", return_value=self.row))
		self.execution.document = documents.render_evidence(documents.read_evidence(self.identity, self.task))
		self.execution.handoff["periodic_evidence_sha256"] = self.task["report_evidence"]["sha256"]
		self.execution.calls = [{"role": "review", "plan": "reviewer", "tool": "claude", "ok": True}]
		self.execution.checks = [{"name": "문서", "ok": True, "detail": "합성"}]
		review = {"verdict": "pass", "summary": "합성", "findings": []}
		documents.pin_review(self.execution, "합성 검토", review)
		documents.save_review(self.execution, review)
		self.execution.finish("완료", "합성 검토 통과")
		self.seal = document_run.report_seal(self.execution)
		self.execution.handoff["periodic_report_seal"] = self.seal
		self.execution.save()
		documents.save(self.identity, "t1", self.execution.document)
		self.task.update(status="완료", report_seal=self.seal)
	def test_verified_report_matches_request_review_and_private_document(self):
		self.task["status"] = "실행 중"; self.execution.status = "작업 중"; self.execution.save(); worker = document_run.DocumentFlow(self.execution, None, {})
		self.assertFalse(worker._review_isolation_required())
		original = self.execution.document; self.execution.document += "변조"
		self.assertTrue(worker._review_isolation_required())
		self.execution.document = original
		calls = self.execution.calls; self.execution.calls = []; self.assertTrue(worker._review_isolation_required()); self.execution.calls = calls
		stored = self.execution.folder / "run.json"; content = stored.read_bytes()
		modified = json.loads(content); modified["request"]["goal"] = "변조"
		stored.write_text(json.dumps(modified), encoding="utf-8")
		self.assertTrue(worker._review_isolation_required()); stored.write_bytes(content)
		self.row["status"] = "검수"; self.assertTrue(worker._review_isolation_required()); self.row["status"] = "실행 중"
		self.task["status"] = "완료"; self.execution.status = "완료"; self.execution.save()
		self.assertEqual(document_run.verified_report(self.identity, self.task), self.execution.document)
	def test_tampered_document_review_or_execution_is_rejected(self):
		for target in ("document", "review", "execution"):
			with self.subTest(target=target):
				if target == "document":
					path = documents.path(self.identity, "t1")
				elif target == "review":
					path = self.execution.folder / "review-0.json"
				else:
					path = self.execution.folder / "run.json"
				original = path.read_bytes()
				path.write_bytes(original + b" ")
				if target == "execution":
					row = json.loads(original)
					row["document"] += "변조"
					path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
				with self.assertRaises(ValueError):
					document_run.verified_report(self.identity, self.task)
				path.write_bytes(original)
	def test_hardlinked_document_cannot_be_used_as_verified_evidence(self):
		os.link(documents.path(self.identity, "t1"), self.folder / "synthetic-link")
		with self.assertRaises(ValueError):
			document_run.verified_report(self.identity, self.task)
	def test_dependent_code_receives_only_verified_report_and_rejects_tampering(self):
		repo = self.root / "synthetic-repo"
		repo.mkdir()
		self.enterContext(mock.patch.object(targets, "REPO", repo))
		branch = "agency/" + self.identity
		request = {"request": {"repo": str(repo), "kind": "code", "role": "backend", "goal": "합성 수정",
			"done": ["합성"], "owned_paths": ["src/a.py"], "base_branch": branch,
			"report_project": self.identity, "report_task": "t2", "report_dependencies": ["t1"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		path = self.folder / "t2.toml"
		path.write_text(draft.serialize(request), encoding="utf-8")
		path.chmod(0o600)
		code = Run(new_run_id(), request["request"], str(repo), worktree=str(self.root / "worktree"), step="implement")
		shutil.copyfile(path, code.folder / "request.toml")
		(code.folder / "request.toml").chmod(0o600)
		task = {"id": "t2", "title": "합성 수정", "request_path": str(path), "depends_on": ["t1"],
			"status": "실행 중", "run_id": "", "commit": "", "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
		self.row.update(status="실행 중", branch=branch, tasks=[self.task, task])
		without = {key: value for key, value in request["request"].items() if key != "report_dependencies"}
		with self.assertRaises(ValueError):
			runner._verify_report_dependencies(self.row, task, without)
		runner._verify_report_dependencies(self.row, task, request["request"])
		worker = flow.Flow(code, None, {})
		with mock.patch.object(worker, "ask", return_value=None) as ask:
			worker.implement()
		self.assertIn(self.execution.document, ask.call_args.args[1])
		with mock.patch.object(flow.checks, "git", return_value="합성 변경"), \
				mock.patch.object(worker, "ask", return_value=None) as ask:
			worker.review()
		self.assertIn(self.execution.document, ask.call_args.args[1])
		with mock.patch.object(worker, "ask", return_value=None) as ask:
			worker.fix()
		self.assertIn(self.execution.document, ask.call_args.args[1])
		documents.path(self.identity, "t1").write_text("변조", encoding="utf-8")
		with mock.patch.object(worker, "ask") as ask, self.assertRaises(ValueError):
			worker.implement()
		ask.assert_not_called()
if __name__ == "__main__":
	unittest.main()
