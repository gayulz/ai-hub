"""합성 분석 실행으로 독립 판정과 기계 검증의 승인 경계를 검사한다."""
import copy
from dataclasses import replace
import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from agency import acceptance, delivery, document_review, document_run, documents, planner, private_provider, project, runner
from orchestrator import draft, providers
from orchestrator.__main__ import read_request
from orchestrator.flow import Flow
from orchestrator.record import Run, new_run_id
import test_agency_planner as planner_fixtures
import test_agency_runner as runner_fixtures
class PeriodicReplanTest(unittest.TestCase):
	def setUp(self):
		self.fixture = planner_fixtures.AgencyPlannerTest(methodName="runTest")
		self.fixture.setUp()
		self.addCleanup(self.fixture.doCleanups)
		row = self.fixture.confirmed()
		self.identity = row["id"]
		with project._edit(self.identity, "synthetic_schedule") as current:
			current["schedule_repo"] = "xyz"
		planner.plan(self.identity)
		self.before = project.load(self.identity)
		self.original = {item["request_path"]: Path(item["request_path"]).read_bytes() for item in self.before["tasks"]}
		self.option = json.dumps({"project": self.identity, "reports": {"t1": ["tests"]}})
	def replan(self, value=None):
		with mock.patch.dict(os.environ, {"AI_HUB_PERIODIC_REPLAN": self.option if value is None else value}):
			return planner.plan(self.identity)
	def test_explicit_conversion_preserves_originals_budget_dependencies_and_spec(self):
		self.replan()
		current = project.load(self.identity)
		for key in ("request_text", "spec", "spec_hash", "answers", "budget", "used", "branch", "status"):
			self.assertEqual(current[key], self.before[key])
		self.assertEqual(current["audit_report_mode"], "explicit-v1")
		self.assertEqual(current["tasks"][1]["depends_on"], ["t1"])
		for path, data in self.original.items():
			self.assertEqual(Path(path).read_bytes(), data)
		for task in current["tasks"]:
			self.assertEqual((task["status"], task["run_id"], task["commit"]), ("대기", "", ""))
			self.assertNotIn(task["request_path"], self.original)
		report, code = [read_request(task["request_path"])["request"] for task in current["tasks"]]
		self.assertEqual((report["kind"], report["report_checks"]), ("analysis", ["tests"]))
		self.assertFalse({"repo", "base_branch", "owned_paths", "test_command"} & report.keys())
		self.assertEqual((code["kind"], code["report_dependencies"]), ("code", ["t1"]))
		self.assertEqual(self.fixture.ask.call_count, 1)
	def test_existing_external_plan_can_be_replanned_without_running_its_repository(self):
		selected = replace(planner.targets.target("xyz"), id="blog")
		with mock.patch.object(planner.targets, "target", return_value=selected):
			self.replan()
		self.assertEqual(project.load(self.identity)["audit_report_mode"], "explicit-v1")
		for path, data in self.original.items():
			self.assertEqual(Path(path).read_bytes(), data)
	def test_replan_records_report_to_report_dependencies(self):
		option = json.dumps({"project": self.identity, "reports": {"t1": ["tests"], "t2": ["dependencies"]}})
		self.replan(option)
		task = project.load(self.identity)["tasks"][1]
		request = read_request(task["request_path"])["request"]
		self.assertEqual((request["kind"], request["report_dependencies"]), ("analysis", ["t1"]))
		self.assertEqual(task["depends_on"], ["t1"])
	def test_started_task_and_wrong_project_are_rejected_without_state_change(self):
		for key, value in (("status", "실행 중"), ("run_id", "synthetic-run"), ("commit", "a" * 40)):
			with self.subTest(key=key):
				with project._edit(self.identity, "synthetic_change") as row:
					row["tasks"][0][key] = value
				snapshot = copy.deepcopy(project.load(self.identity))
				with self.assertRaises(ValueError):
					self.replan()
				self.assertEqual(project.load(self.identity), snapshot)
				with project._edit(self.identity, "synthetic_restore") as row:
					row["tasks"][0][key] = self.before["tasks"][0][key]
		wrong = json.dumps({"project": "p-20261028-999", "reports": {"t1": ["tests"]}})
		with self.assertRaises(ValueError):
			self.replan(wrong)
		duplicate = '{"project":' + json.dumps(self.identity) + ',"project":' + json.dumps(self.identity) + ',"reports":{"t1":["tests"]}}'
		with self.assertRaises(ValueError):
			self.replan(duplicate)
		for path, data in self.original.items():
			self.assertEqual(Path(path).read_bytes(), data)
	def test_concurrent_project_change_refuses_replacement(self):
		original = planner._commit_replan
		def concurrent(*args, **kwargs):
			project.update_task(self.identity, "t1", status="실행 중")
			return original(*args, **kwargs)
		with mock.patch.object(planner, "_commit_replan", side_effect=concurrent), self.assertRaises(ValueError):
			self.replan()
		current = project.load(self.identity)
		self.assertEqual(current["tasks"][0]["status"], "실행 중")
		self.assertNotIn("audit_report_mode", current)
		self.assertEqual([task["request_path"] for task in current["tasks"]], list(self.original))
		for path, data in self.original.items():
			self.assertEqual(Path(path).read_bytes(), data)
	def test_changed_source_during_conversion_is_not_overwritten(self):
		original = planner._commit_replan
		path = Path(self.before["tasks"][0]["request_path"])
		def concurrent(*args, **kwargs):
			path.write_bytes(path.read_bytes() + b"\n# synthetic change\n")
			return original(*args, **kwargs)
		with mock.patch.object(planner, "_commit_replan", side_effect=concurrent), self.assertRaises(ValueError):
			self.replan()
		self.assertTrue(path.read_bytes().endswith(b"# synthetic change\n"))
		current = project.load(self.identity)
		self.assertEqual(current, self.before)
	def test_interrupted_replan_can_retry_without_removing_old_preparation(self):
		with mock.patch.object(planner, "_commit_replan", side_effect=ValueError("합성 중단")), self.assertRaises(ValueError):
			self.replan()
		folder = project._folder(self.identity)
		previous = list(folder.glob("tasks-report-*"))
		self.assertEqual(len(previous), 1)
		snapshot = {path.name: path.read_bytes() for path in previous[0].iterdir()}
		self.replan()
		self.assertEqual(len(list(folder.glob("tasks-report-*"))), 2)
		self.assertEqual({path.name: path.read_bytes() for path in previous[0].iterdir()}, snapshot)
	def test_final_review_interruption_reserves_usage_and_never_repeats_automatically(self):
		with project._edit(self.identity, "synthetic_review") as row:
			row["status"], row["budget"], row["used"] = "검수", {"calls": 2, "minutes": 1}, {"calls": 0, "seconds": 0}
		plan = SimpleNamespace(id="synthetic", tool="claude", model="")
		with mock.patch.object(document_review.ai, "ask", side_effect=KeyboardInterrupt) as call:
			with self.assertRaises(KeyboardInterrupt):
				document_review.periodic_call(self.identity, self.fixture.department, "합성", {}, [plan])
			with self.assertRaises(RuntimeError):
				document_review.periodic_call(self.identity, self.fixture.department, "합성", {}, [plan])
		self.assertEqual(call.call_count, 1)
		row = project.load(self.identity)
		self.assertEqual((row["used"]["calls"], row["periodic_final_reviews"][-1]["state"]), (1, "running"))
		self.assertGreaterEqual(row["used"]["seconds"], row["periodic_final_reviews"][-1]["timeout_seconds"])
	def test_final_review_preserves_only_the_classified_failure_reason(self):
		with project._edit(self.identity, "synthetic_review") as row:
			row["status"], row["budget"], row["used"] = "검수", {"calls": 2, "minutes": 1}, {"calls": 0, "seconds": 0}
		plan = SimpleNamespace(id="synthetic", tool="claude", model="")
		with mock.patch.object(document_review.ai, "ask", side_effect=RuntimeError("네트워크·서버 오류: synthetic-private")), \
				self.assertRaisesRegex(RuntimeError, "네트워크·서버 오류") as caught:
			document_review.periodic_call(self.identity, self.fixture.department, "합성", {}, [plan])
		self.assertNotIn("synthetic-private", str(caught.exception))
	def test_limit_from_confirmed_spec_includes_test_files(self):
		row = copy.deepcopy(self.before)
		row["audit_report_mode"] = "explicit-v1"
		row["spec"]["goal"] = "코드 수정은 1~3개 파일 이내이며 시험 파일도 포함한다."
		row["spec_hash"] = planner.inputs._hash(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode())
		self.assertEqual(project.periodic_file_limit(row), 3)
		row["spec"]["goal"] += " 변조"
		with self.assertRaises(ValueError):
			project.periodic_file_limit(row)
		row["audit_report_mode"] = ""
		self.assertIsNone(project.periodic_file_limit(row))
	def test_conflicting_file_limits_are_unverified(self):
		row = copy.deepcopy(self.before)
		row["audit_report_mode"] = "explicit-v1"
		row["spec"]["goal"] = "코드 파일 2개 이내, 시험 파일 1개 이내로 제한한다."
		row["spec_hash"] = planner.inputs._hash(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode())
		with self.assertRaisesRegex(ValueError, "제한"):
			project.periodic_file_limit(row)
	def test_report_does_not_describe_requested_checks_as_completed(self):
		text = documents.render_evidence({"checks": [{"kind": "tests", "status": "unverified", "reason": "source_unavailable"}]})
		self.assertIn("요청된 점검", text)
		self.assertNotIn("확인한 요청", text)
		self.assertIn("실행하지 않았습니다", text)
	def test_interrupted_evidence_publication_can_resume_without_removing_fragments(self):
		self.replan()
		project.update_task(self.identity, "t1", status="실행 중")
		task = project.load(self.identity)["tasks"][0]
		file = documents.evidence_path(self.identity, "t1")
		with mock.patch.object(documents.os, "link", side_effect=OSError("합성 중단")), self.assertRaises(OSError):
			documents.prepare_evidence(self.identity, task)
		fragments = list(file.parent.glob(".evidence-t1-*"))
		self.assertEqual(len(fragments), 1)
		self.assertFalse(file.exists())
		anchor = documents.prepare_evidence(self.identity, task)
		self.assertTrue(fragments[0].exists())
		task["report_evidence"] = anchor
		self.assertEqual(documents.read_evidence(self.identity, task)["checks"][0]["status"], "unverified")
	def test_interruption_after_link_recovers_and_rejects_extra_hardlink(self):
		self.replan()
		project.update_task(self.identity, "t1", status="실행 중")
		task = project.load(self.identity)["tasks"][0]
		link = os.link
		def interrupted(*args, **kwargs):
			link(*args, **kwargs)
			raise OSError("합성 중단")
		with mock.patch.object(documents.os, "link", side_effect=interrupted), self.assertRaises(OSError):
			documents.prepare_evidence(self.identity, task)
		file = documents.evidence_path(self.identity, "t1")
		self.assertEqual(file.stat().st_nlink, 2)
		task["report_evidence"] = documents.prepare_evidence(self.identity, task)
		self.assertEqual(documents.read_evidence(self.identity, task)["checks"][0]["status"], "unverified")
		os.link(file, file.parent / "synthetic-unapproved-link")
		with self.assertRaises(ValueError):
			documents.read_evidence(self.identity, task)
	def test_report_can_check_branch_metadata_without_isolation_inputs(self):
		row = {"id": self.identity, "audit_report_mode": "explicit-v1", "schedule_repo": "other",
			"branch": "agency/synthetic", "audit_base": "a" * 40}
		planner.checks.git(self.fixture.repo, "-c", "user.name=synthetic", "-c", "user.email=synthetic", "commit", "-qm", "합성")
		planner.checks.git(self.fixture.repo, "worktree", "add", "-qb", "linked", str(self.fixture.root / "linked"))
		selected = SimpleNamespace(id="other", path=self.fixture.root / "linked")
		with mock.patch("agency.schedule.target", return_value="other"), \
				mock.patch("orchestrator.targets.target", return_value=selected), \
				mock.patch.object(planner, "load_departments", return_value={"xyz": self.fixture.department}), \
				mock.patch("orchestrator.checks.git", side_effect=["", "a" * 40]) as git:
			self.assertEqual(documents.unchanged_repo(row, self.fixture.department), selected)
		self.assertEqual([call.args[1] for call in git.call_args_list], ["check-ref-format", "rev-parse"])
	def test_blog_report_final_review_is_blocked_before_provider_or_git(self):
		row = {"id": self.identity, "department": "xyz", "status": "검수", "schedule_repo": "blog",
			"audit_report_mode": "explicit-v1", "branch": "agency/synthetic", "audit_base": "a" * 40,
			"tasks": [{"status": "완료", "report_seal": {"sha256": "a" * 64}, "commit": ""}],
			"spec": {"done_criteria": ["합성"]}}
		selected = SimpleNamespace(id="blog", path=self.fixture.repo)
		with mock.patch.object(acceptance.project, "load", return_value=row), \
				mock.patch.object(acceptance.project, "transition", return_value={"status": "막힘"}), \
				mock.patch.object(acceptance, "load_departments", return_value={"xyz": self.fixture.department}), \
				mock.patch("agency.schedule.target", return_value="blog"), \
				mock.patch("orchestrator.targets.target", return_value=selected), \
				mock.patch("orchestrator.checks.git") as git, mock.patch.object(acceptance.ai, "ask") as ask:
			result = acceptance.review(self.identity)
		self.assertEqual((result["status"], result["unverified"]), ("막힘", 1))
		ask.assert_not_called()
	def test_project_file_limit_counts_code_and_test_files_across_all_commits(self):
		case = runner_fixtures.RunnerTest(methodName="runTest")
		case.setUp()
		self.addCleanup(case.doCleanups)
		row = case.new()
		with project._edit(row["id"], "synthetic_limit") as saved:
			saved["audit_report_mode"] = "explicit-v1"
			saved["audit_base"] = case.head
			saved["spec"]["goal"] = "코드 수정은 1~3개 파일 이내이며 시험 파일도 포함한다."
			saved["spec_hash"] = planner.inputs._hash(json.dumps(saved["spec"], ensure_ascii=False, sort_keys=True).encode())
		row = project.load(row["id"])
		for name in ("a.py", "b.py", "test_a.py"):
			(case.repo / name).write_text("# synthetic\n", encoding="utf-8")
		case.git("add", "--", "a.py", "b.py", "test_a.py")
		case.git("commit", "-q", "-m", "합성 세 파일")
		three = case.git("rev-parse", "HEAD")
		self.assertEqual(set(documents.periodic_changed_paths(row, case.repo, three)), {"a.py", "b.py", "test_a.py"})
		(case.repo / "test_b.py").write_text("# synthetic\n", encoding="utf-8")
		case.git("add", "--", "test_b.py")
		case.git("commit", "-q", "-m", "합성 네 번째 파일")
		with self.assertRaisesRegex(ValueError, "파일"):
			documents.periodic_changed_paths(row, case.repo, case.git("rev-parse", "HEAD"))
	def test_blog_execution_stays_blocked_before_repository_or_provider_access(self):
		case = runner_fixtures.RunnerTest(methodName="runTest")
		case.setUp(); self.addCleanup(case.doCleanups)
		row = case.new()
		selected = SimpleNamespace(id="blog", path=case.repo)
		with mock.patch.object(runner.targets, "target", return_value=selected), \
				mock.patch.object(runner.targets, "targets", return_value={"blog": SimpleNamespace(path=case.repo)}), \
				mock.patch.object(runner, "git") as git, \
				mock.patch.object(runner.meeting, "ensure") as meeting:
			result = runner.run(row["id"])
		self.assertEqual(result["status"], "막힘")
		git.assert_not_called()
		meeting.assert_not_called()
		case.start_mock.assert_not_called()
	def report_case(self):
		case = runner_fixtures.RunnerTest(methodName="runTest")
		case.setUp()
		self.addCleanup(case.doCleanups)
		row = case.new()
		identity, task = row["id"], row["tasks"][0]
		workdir = project._folder(identity) / "work"
		request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
			"goal": "합성 점검", "done": ["미검증 보고"], "forbidden": [], "details": "합성",
			"report_mode": "periodic-v1", "report_project": identity, "report_task": "t1", "report_checks": ["tests"]},
			"budget": {"calls": 4, "minutes": 5, "fixes": 0}}
		path = Path(task["request_path"])
		path.write_text(draft.serialize(request), encoding="utf-8")
		path.chmod(0o600)
		with project._edit(identity, "synthetic_report_mode") as saved:
			saved["schedule_repo"], saved["audit_report_mode"] = "sample", "explicit-v1"
			saved["tasks"][0]["request_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text(encoding="utf-8").replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		return case, identity
	def test_report_saved_before_interruption_resumes_without_duplicate_review_or_usage(self):
		case, identity = self.report_case()
		save = documents.save
		def interrupted(*args, **kwargs):
			save(*args, **kwargs)
			raise KeyboardInterrupt
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as review:
			with mock.patch.object(documents, "save", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
			before = project.load(identity)
			self.assertEqual(before["tasks"][0]["status"], "실행 중")
			self.assertTrue(before["tasks"][0]["run_id"])
			self.assertEqual(runner.run(identity)["status"], "검수")
		current = project.load(identity)
		self.assertEqual(current["used"], before["used"])
		self.assertEqual(current["tasks"][0]["status"], "완료")
		self.assertEqual(review.call_count, 1)
	def test_review_saved_before_interruption_is_reused_without_another_provider_call(self):
		case, identity = self.report_case()
		save = documents.save_review
		def interrupted(*args, **kwargs):
			save(*args, **kwargs)
			raise KeyboardInterrupt
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as review:
			with mock.patch.object(documents, "save_review", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
			task = project.load(identity)["tasks"][0]
			self.assertEqual(task["status"], "실행 중")
			file = case.hub / "runs" / task["run_id"] / "review-0.json"
			self.assertEqual(file.stat().st_mode & 0o777, 0o600)
			self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(review.call_count, 1)
		current = project.load(identity)
		self.assertEqual(current["used"]["calls"], 1)
		raw = json.loads((case.hub / "runs" / task["run_id"] / "run.json").read_text())
		self.assertEqual(raw["document"], "")
		self.assertIn("private_document", raw["handoff"])
		case, identity = self.report_case()
		with mock.patch.object(private_provider, "call", return_value=reply) as review:
			with mock.patch.object(documents, "save_review", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
			task = project.load(identity)["tasks"][0]
			execution = document_run.load(task["run_id"])
			cache = execution.folder / "review-cache" / (execution.calls[-1]["periodic_review_output_sha256"] + ".json")
			self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
			original = cache.read_bytes()
			cache.write_bytes(original + b"synthetic-tamper")
			with self.assertRaises(ValueError):
				document_review.restore_periodic_review(execution, execution.calls[-1]["periodic_review_output_sha256"])
			cache.write_bytes(original)
			self.assertEqual(runner.run(identity)["status"], "검수")
			review.assert_called_once()
	def test_completed_review_before_seal_recovers_without_another_provider_call(self):
		case, identity = self.report_case()
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as review:
			with mock.patch.object(document_run, "report_seal", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
			task = project.load(identity)["tasks"][0]
			execution = document_run.load(task["run_id"])
			self.assertEqual((task["status"], execution.status), ("실행 중", "완료"))
			self.assertNotIn("periodic_report_seal", execution.handoff)
			self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(review.call_count, 1)
		self.assertEqual(project.load(identity)["used"]["calls"], 1)
	def test_periodic_report_provider_timeout_is_capped_by_remaining_project_budget(self):
		case, identity = self.report_case()
		with project._edit(identity, "synthetic_remaining_budget") as row:
			row["budget"] = {"calls": 5, "minutes": 1}
			row["used"] = {"calls": 4, "seconds": 20}
		def call(plan, writer, prompt, cwd, schema, timeout):
			self.assertLessEqual(timeout, 39)
			return providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", side_effect=call) as review:
			self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(review.call_count, 1)
		self.assertEqual(project.load(identity)["used"]["calls"], 5)
	def test_periodic_report_does_not_start_below_the_provider_minimum(self):
		case, identity = self.report_case()
		with project._edit(identity, "synthetic_remaining_budget") as row:
			row["budget"] = {"calls": 5, "minutes": 1}
			row["used"] = {"calls": 0, "seconds": 45}
		with mock.patch.object(private_provider, "call") as review:
			self.assertEqual(runner.run(identity)["status"], "막힘")
		review.assert_not_called()
	def test_failed_periodic_provider_call_is_measured_and_charged(self):
		case, identity = self.report_case()
		def failure(*args, **kwargs):
			time.sleep(0.003)
			raise providers.ProviderError("exit", "합성 실패")
		with mock.patch.object(private_provider, "call", side_effect=failure):
			self.assertEqual(runner.run(identity)["status"], "막힘")
		row = project.load(identity)
		execution = document_run.load(row["tasks"][0]["run_id"])
		self.assertEqual(row["used"]["calls"], len(execution.calls))
		self.assertGreater(row["used"]["seconds"], 0)
		self.assertTrue(all(call["seconds"] > 0 for call in execution.calls))
		self.assertEqual(sum(call["seconds"] for call in execution.calls), row["used"]["seconds"])
	def test_periodic_usage_is_not_charged_twice_after_interrupted_run_save(self):
		case, identity = self.report_case()
		save = document_run.DocumentRun.save
		interrupted = {"done": False}
		def once(execution):
			if execution.handoff.get("agency_usage") and not interrupted["done"]:
				interrupted["done"] = True
				raise KeyboardInterrupt
			return save(execution)
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as review:
			with mock.patch.object(document_run.DocumentRun, "save", autospec=True, side_effect=once), self.assertRaises(KeyboardInterrupt):
				runner.run(identity)
			before = project.load(identity)["used"]
			self.assertEqual(before["calls"], 1)
			self.assertEqual(runner.run(identity)["status"], "검수")
		self.assertEqual(project.load(identity)["used"], before)
		self.assertEqual(review.call_count, 1)
		case, identity = self.report_case()
		with mock.patch.object(private_provider, "call", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			runner.run(identity)
		row = project.load(identity)
		execution = document_run.load(row["tasks"][0]["run_id"])
		self.assertIsNone(execution.calls[-1]["ok"])
		self.assertGreater(execution.calls[-1]["seconds"], 0)
		self.assertEqual(documents.settle_periodic_usage(identity, execution)["used"]["seconds"], execution.calls[-1]["seconds"])
		with mock.patch.object(private_provider, "call") as repeated:
			self.assertEqual(runner.run(identity)["status"], "막힘")
			repeated.assert_not_called()
	def test_later_report_includes_verified_prior_report_and_detects_tampering(self):
		case = runner_fixtures.RunnerTest(methodName="runTest")
		case.setUp()
		self.addCleanup(case.doCleanups)
		row = case.new(2)
		identity = row["id"]
		workdir = project._folder(identity) / "work"
		for index, task in enumerate(row["tasks"], 1):
			request = {"request": {"kind": "analysis", "workdir": str(workdir), "role": "backend",
				"goal": f"합성 점검 {index}", "done": ["미검증 보고"], "forbidden": [], "details": "합성",
				"report_mode": "periodic-v1", "report_project": identity, "report_task": task["id"],
				"report_checks": ["tests"]}, "budget": {"calls": 4, "minutes": 5, "fixes": 0}}
			if index == 2:
				request["request"]["report_dependencies"] = ["t1"]
			path = Path(task["request_path"])
			path.write_text(draft.serialize(request), encoding="utf-8")
			path.chmod(0o600)
		with project._edit(identity, "synthetic_report_mode") as saved:
			saved["schedule_repo"], saved["audit_report_mode"] = "sample", "explicit-v1"
			for task in saved["tasks"]:
				task["request_sha256"] = hashlib.sha256(Path(task["request_path"]).read_bytes()).hexdigest()
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text(encoding="utf-8").replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .2, {})
		with mock.patch.object(private_provider, "call", return_value=reply):
			result = runner.run(identity)
			self.assertEqual(result["status"], "검수", result)
		prior = documents.read(identity, "t1").decode()
		later = documents.read(identity, "t2").decode()
		self.assertIn("## 선행 보고 t1", later)
		self.assertIn(prior, later)
		first = documents.path(identity, "t1")
		first.write_text("# 합성 변조\n", encoding="utf-8")
		with self.assertRaises(ValueError):
			documents.read_evidence(identity, project.load(identity)["tasks"][1])
if __name__ == "__main__":
	unittest.main()
