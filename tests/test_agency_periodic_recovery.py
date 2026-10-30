"""합성 프로젝트의 검토 거절, 사실 고정, 재검토와 오염 거부를 연결한다."""

import copy
import hashlib
import io
import json
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_agency_runner as fixtures
from agency import ai, delivery, document_review, document_run, documents, planner, private_provider, project, runner
from agency.__main__ import main as agency_main
from orchestrator import draft, providers


def digest(value):
	return hashlib.sha256(value).hexdigest()


def write_private(path, data):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(data)
	path.chmod(0o600)


class RecoveryTest(unittest.TestCase):
	def setUp(self):
		self.case = fixtures.RunnerTest(methodName="runTest")
		self.case.setUp()
		self.addCleanup(self.case.doCleanups)
		case = self.case
		(case.repo / "tests").mkdir()
		(case.repo / "tests/test_example.py").write_text("def test_sample(): pass\n", encoding="utf-8")
		(case.repo / "requirements.txt").write_text("sample-package==1.2.3\n", encoding="utf-8")
		case.git("add", "--", "tests/test_example.py", "requirements.txt")
		case.git("commit", "-q", "-m", "합성 사실 자료")
		case.head = case.git("rev-parse", "HEAD")
		(case.hub / "redaction").mkdir()
		(case.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "private-example", "kind": "회사명", "token": "company01"}]}), encoding="utf-8")
		plans = case.hub / "plans.toml"
		plans.write_text(plans.read_text().replace('tool = "agy"', 'tool = "claude"'), encoding="utf-8")
		row = case.new(3)
		self.identity = row["id"]
		self.folder = project._folder(self.identity)
		self.criterion = "확인된 시험과 의존성 사실 및 미검증 범위를 보고한다"
		with project._edit(self.identity, "synthetic_periodic") as current:
			current["schedule_repo"], current["audit_report_mode"] = "sample", "explicit-v1"
			current["spec"]["done_criteria"] = [self.criterion]
			current["spec_hash"] = digest(json.dumps(current["spec"], ensure_ascii=False, sort_keys=True).encode())
			current["tasks"][2]["depends_on"] = ["t1", "t2"]
			for index, task in enumerate(current["tasks"]):
				request = {"role": "backend", "goal": "합성 점검", "done": [self.criterion],
					"forbidden": [], "details": "합성 자료"}
				if index < 2:
					request.update(kind="analysis", workdir=str(self.folder / "work"), report_mode="periodic-v1",
						report_project=self.identity, report_task=task["id"],
						report_checks=["tests"] if index == 0 else ["dependencies"])
					if index == 1:
						request["report_dependencies"] = ["t1"]
				else:
					request.update(kind="code", repo=str(case.repo), base_branch=current["branch"], owned_paths=["value.txt"])
				prepared = {"request": request, "budget": {"calls": 4, "minutes": 5, "fixes": 0}}
				path = Path(task["request_path"])
				write_private(path, draft.serialize(prepared).encode())
				task["request_sha256"] = digest(path.read_bytes())
		failure = providers.Reply({"verdict": "changes", "summary": "근거 부족", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=failure):
			self.assertEqual(runner.run(self.identity)["status"], "막힘")
			first = project.load(self.identity)["tasks"][0]["run_id"]
			with project._edit(self.identity, "synthetic_previous_attempt") as current:
				current["tasks"][0]["status"], current["tasks"][0]["run_id"] = "대기", ""
			self.assertEqual(runner.run(self.identity)["status"], "막힘")
		second = project.load(self.identity)["tasks"][0]["run_id"]
		self.assertNotEqual(first, second)
		self.old_runs = (first, second)
		self.old_run_bytes = {number: (document_run.load(number).folder / "run.json").read_bytes() for number in self.old_runs}
		self.original_ledger = (self.folder / "project.json").read_bytes()
		self.original_tasks = copy.deepcopy(project.load(self.identity)["tasks"])
		self.original_requests = {task["request_path"]: Path(task["request_path"]).read_bytes() for task in self.original_tasks}
		self.original_evidence = documents.evidence_path(self.identity, "t1").read_bytes()
		self.old_document = documents.path(self.identity, "t1")
		write_private(self.old_document, document_run.load(second).document.encode())
		self.old_document_bytes = self.old_document.read_bytes()
		self._make_facts()

	def _make_facts(self):
		case = self.case
		file = {"path": "tests/test_example.py", "sha256": digest(b"synthetic test"), "provenance": "synthetic-inline-v1"}
		policy = {"files": [[file["path"], file["sha256"], file["provenance"]]], "version": "synthetic-v1"}
		counts = {"ran": 3, "failures": 0, "errors": 0, "skipped": 1}
		identity = "a" * 64
		record = {"identity": identity, "executed": True, "status": "passed", "ok": True,
			"process": {"status": "passed"}, "tests": counts,
			"policy_sha256": digest(json.dumps(policy, sort_keys=True).encode())}
		self.record = self.folder / "evidence/isolation" / (identity + ".json")
		write_private(self.record, json.dumps(record).encode())
		self.facts = {"version": 1, "project": self.identity, "base_commit": case.head, "date": "2026-10-30",
			"tests": {"command": "python3 -m unittest", "files": ["tests/test_example.py"], "file_count": 1,
				"full_not_run_reason": "운영 자료 격리 범위가 준비되지 않았습니다",
				"partial": {"executed": True, "status": "passed", "counts": counts, "files": [file],
					"record": {"name": self.record.name, "sha256": digest(self.record.read_bytes())}, "policy": policy}},
			"dependencies": {"declarations": [{"path": "requirements.txt", "lines": ["sample-package==1.2.3"],
				"installed": [{"name": "sample-package", "version": "1.2.3"}],
				"installed_unverified": "실행 환경의 설치 상태를 별도로 확인하지 않았습니다"}],
				"transitive_unverified": "전이 목록을 수집하지 않았습니다", "advisories_unverified": "보안 공지를 조회하지 않았습니다",
				"support_unverified": "지원 기간을 확인하지 않았습니다"}}
		self.facts_path = self.folder / "prepared-facts.json"
		self.write_facts()
		self.choice = {"project": self.identity, "reports": {"t1": ["tests"], "t2": ["dependencies"],
			"t3": ["tests", "dependencies"]}, "recover": True, "facts": str(self.facts_path),
			"isolation_record": str(self.record)}

	def write_facts(self):
		write_private(self.facts_path, json.dumps(self.facts, ensure_ascii=False).encode())

	def replan(self, choice=None):
		with mock.patch.dict(os.environ, {"AI_HUB_PERIODIC_REPLAN": json.dumps(choice or self.choice)}), \
				redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
			return agency_main(["plan", self.identity])

	def test_recovery_preserves_both_failed_runs_and_reaches_delivery_waiting(self):
		case = self.case
		self.assertEqual(self.replan(), 0)
		row = project.load(self.identity)
		self.assertEqual((row["status"], row["blocked_from"]), ("막힘", "실행 중"))
		self.assertEqual([item["run_id"] for item in row["tasks"]], ["", "", ""])
		self.assertEqual(row["tasks"][2]["depends_on"], ["t1", "t2"])
		folder = Path(row["periodic_recovery"]["path"]).parent
		self.assertEqual((folder / "original-project.json").read_bytes(), self.original_ledger)
		self.assertEqual(stat.S_IMODE((folder / "original-project.json").stat().st_mode), 0o400)
		self.assertTrue(all(Path(path).read_bytes() == data for path, data in self.original_requests.items()))
		self.assertEqual((self.folder / "evidence/t1.json").read_bytes(), self.original_evidence)
		self.assertEqual(self.old_document.read_bytes(), self.old_document_bytes)
		self.assertTrue(all((document_run.load(number).folder / "run.json").read_bytes() == data
			for number, data in self.old_run_bytes.items()))
		self.assertEqual(self.old_document.read_bytes(), self.old_document_bytes)
		reply = providers.Reply({"verdict": "pass", "summary": "사실과 미검증 구분", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as reviewer:
			with redirect_stdout(io.StringIO()):
				self.assertEqual(agency_main(["run", self.identity]), 0)
		self.assertEqual(reviewer.call_count, 3)
		row = project.load(self.identity)
		self.assertEqual(row["status"], "검수")
		self.assertTrue(all(task.get("report_seal") and not task["commit"] for task in row["tasks"]))
		self.assertTrue(all(task["run_id"] not in self.old_runs for task in row["tasks"]))
		report = document_run.verified_report(self.identity, row["tasks"][2])
		for phrase in (case.head, "python3 -m unittest", "tests/test_example.py", "실행 3개, 실패 0개, 오류 0개, 건너뜀 1개",
				"전체 시험 결과가 아닙니다", "sample-package==1.2.3", "설치 상태 미확인", "전이 의존성 미확인", "보안 공지 미확인",
				"지원 상태 미확인", "수정 완료 여부는 확인하지 않았습니다", "선행 보고 t1", "선행 보고 t2"):
			self.assertIn(phrase, report)
		self.assertNotIn("의존성: 안전한 자료와 격리 실행 근거가 없어", report)
		answer = ai.Answer({"criteria": [{"criterion": self.criterion, "verdict": "pass", "evidence": "합성 보고"}],
			"summary": "합성 확인"}, "reviewer", .1)
		with mock.patch.object(document_review.ai, "ask", return_value=answer) as final, redirect_stdout(io.StringIO()):
			self.assertEqual(agency_main(["review", self.identity]), 0)
		final.assert_called_once()
		self.assertEqual(project.load(self.identity)["status"], "납품 대기")
		self.assertEqual(delivery.load_bundle(self.identity)["project"], self.identity)
		self.assertEqual(case.git("rev-parse", row["branch"]), case.head)
		self.assertTrue(all((document_run.load(number).folder / "run.json").read_bytes() == data
			for number, data in self.old_run_bytes.items()))

	def test_wrong_project_base_record_counts_policy_or_declaration_is_rejected(self):
		baseline = copy.deepcopy(self.facts)
		mutations = [lambda value: value.update(project="p-20261030-999"),
			lambda value: value.update(base_commit="b" * 40),
			lambda value: value["tests"]["partial"]["counts"].update(ran=4),
			lambda value: value["tests"]["partial"]["record"].update(sha256="b" * 64),
			lambda value: value["tests"]["partial"]["files"][0].update(sha256="b" * 64),
			lambda value: value["dependencies"]["declarations"][0].update(lines=["sample-package==9"]),
			lambda value: value["tests"].update(file_count=2),
			lambda value: value["tests"].update(command="python3 -m different")]
		before = project.load(self.identity)
		for mutate in mutations:
			self.facts = copy.deepcopy(baseline)
			mutate(self.facts)
			self.write_facts()
			with self.subTest(mutate=mutate):
				self.assertEqual(self.replan(), 1)
				self.assertEqual(project.load(self.identity), before)
		self.facts = baseline
		self.write_facts()
		other = self.case.root / "other-project" / self.record.name
		write_private(other, self.record.read_bytes())
		choice = {**self.choice, "isolation_record": str(other)}
		self.assertEqual(self.replan(choice), 1)
		self.assertEqual(project.load(self.identity), before)

	def test_links_or_existing_final_report_cannot_be_used_for_recovery(self):
		before = project.load(self.identity)
		link = self.folder / "facts-link.json"
		link.symlink_to(self.facts_path)
		self.assertEqual(self.replan({**self.choice, "facts": str(link)}), 1)
		report = self.folder / "report.md"
		write_private(report, b"previous final review")
		self.assertEqual(self.replan(), 1)
		self.assertEqual(report.read_bytes(), b"previous final review")
		self.assertEqual(project.load(self.identity), before)

	def test_failed_synthetic_result_is_reported_as_failure(self):
		record = json.loads(self.record.read_bytes())
		record.update(status="failed", ok=False, process={"status": "failed"})
		record["tests"].update(failures=1)
		write_private(self.record, json.dumps(record).encode())
		partial = self.facts["tests"]["partial"]
		partial.update(status="failed", counts=record["tests"])
		partial["record"]["sha256"] = digest(self.record.read_bytes())
		self.write_facts()
		self.assertEqual(self.replan(), 0)
		reply = providers.Reply({"verdict": "pass", "summary": "합성 결과 보고", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=reply):
			self.assertEqual(runner.run(self.identity)["status"], "검수")
		task = project.load(self.identity)["tasks"][0]
		report = document_run.verified_report(self.identity, task)
		self.assertIn("별도 합성 격리 결과: 실패, 실행 3개, 실패 1개", report)
		self.assertNotIn("별도 합성 격리 결과: 통과", report)

	def test_isolation_record_with_another_project_or_commit_is_rejected(self):
		original = json.loads(self.record.read_bytes())
		for field, value in (("project", "p-20261030-999"), ("base_commit", "b" * 40)):
			record = {**original, field: value}
			write_private(self.record, json.dumps(record).encode())
			self.facts["tests"]["partial"]["record"]["sha256"] = digest(self.record.read_bytes())
			self.write_facts()
			with self.subTest(field=field):
				self.assertEqual(self.replan(), 1)

	def test_changed_preservation_or_request_blocks_execution(self):
		self.assertEqual(self.replan(), 0)
		row = project.load(self.identity)
		folder = Path(row["periodic_recovery"]["path"]).parent
		for path in (folder / "manifest.json", folder / "original-project.json", folder / "facts.json",
				folder / "isolation-record.json", self.record,
				self.old_document, document_run.load(self.old_runs[0]).folder / "run.json",
				Path(row["tasks"][0]["request_path"])):
			original = path.read_bytes()
			mode = stat.S_IMODE(path.stat().st_mode)
			path.chmod(0o600)
			path.write_bytes(original + b" ")
			with self.subTest(path=path.name):
				with mock.patch.object(private_provider, "call") as provider:
					self.assertEqual(runner.run(self.identity)["status"], "막힘")
				provider.assert_not_called()
			path.write_bytes(original)
			path.chmod(mode)
		self.record.chmod(0o644)
		with mock.patch.object(private_provider, "call") as provider:
			self.assertEqual(runner.run(self.identity)["status"], "막힘")
		provider.assert_not_called()
		self.record.chmod(0o600)
		with project._edit(self.identity, "synthetic_spec_tamper") as current:
			current["spec"]["goal"] += " 변조"
			current["spec_hash"] = digest(json.dumps(current["spec"], ensure_ascii=False, sort_keys=True).encode())
		with mock.patch.object(private_provider, "call") as provider:
			self.assertEqual(runner.run(self.identity)["status"], "막힘")
		provider.assert_not_called()
		old = json.loads(self.original_ledger)
		with project._edit(self.identity, "synthetic_restore_spec") as current:
			current["spec"], current["spec_hash"] = old["spec"], old["spec_hash"]
		with mock.patch.object(private_provider, "call", return_value=providers.Reply(
				{"verdict": "pass", "summary": "합성", "findings": []}, .1, {})):
			self.assertEqual(runner.run(self.identity)["status"], "검수")

	def test_interrupted_new_report_resumes_without_repeating_review(self):
		self.assertEqual(self.replan(), 0)
		save = documents.save
		def interrupted(*args, **kwargs):
			save(*args, **kwargs)
			raise KeyboardInterrupt
		reply = providers.Reply({"verdict": "pass", "summary": "합성", "findings": []}, .1, {})
		with mock.patch.object(private_provider, "call", return_value=reply) as reviewer:
			with mock.patch.object(documents, "save", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
				runner.run(self.identity)
			first = project.load(self.identity)["tasks"][0]
			self.assertEqual(first["status"], "실행 중")
			self.assertNotIn(first["run_id"], self.old_runs)
			self.assertEqual(runner.run(self.identity)["status"], "검수")
		self.assertEqual(reviewer.call_count, 3)
		self.assertEqual(project.load(self.identity)["tasks"][0]["run_id"], first["run_id"])


if __name__ == "__main__":
	unittest.main()
