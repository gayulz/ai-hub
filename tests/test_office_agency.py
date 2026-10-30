"""임시 원장과 가짜 납품으로 사무실 대행사 창구를 확인한다."""

import importlib
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from agency import departments, intake, project
from collector.backfill_common import locked
from office import actions, agency
from test_office_actions import ActionCase
import test_office_actions_http as http_tests


class OfficeAgencyTest(ActionCase):
	request = http_tests.OfficeActionHTTPTest.request

	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token=secrets.token_urlsafe(32))
		self.department = departments.Department("xyz", "합성 부서", "합성 범위", ("ai-hub",), ("xyz",), False,
			"xyz", (), "", (), (), {"calls": 3, "minutes": 5})
		patch = mock.patch.object(departments, "load_departments", return_value={"xyz": self.department})
		patch.start()
		self.addCleanup(patch.stop)
		self.identity = project.new(self.department, "합성 프로젝트", "합성 요청")["id"]
		intake._save_questions(self.identity, {"open": [
			{"no": 1, "question": "첫 합성 질문?", "asked": "2026-10-28T10:00:00+09:00"},
			{"no": 2, "question": "둘째 합성 질문?", "asked": "2026-10-28T10:00:00+09:00"}], "next_no": 3})
		self.bundle = {"hash": "a" * 64, "criteria": [{"criterion": "합성 기준", "verdict": "pass"}],
			"tests": {"ok": True, "summary": "합성 시험 통과"}, "changed": ["x", "y"],
			"actions": ["merge", "push"], "restart_notice": "", "repo": str(self.repo)}

	def waiting(self):
		with project._edit(self.identity, "fixture") as row:
			row["status"] = "납품 대기"

	def post(self, **values):
		status, _, content = self.request({"id": self.identity, **values})
		return status, json.loads(content)

	def logs(self):
		return [json.loads(line) for line in (self.hub / "logs/office-actions.jsonl").read_text().splitlines()]

	def test_office_deferred_and_external_ai_disabled_questions_are_answered_without_telegram(self):
		with mock.patch("office.telegram_client.Client", return_value=SimpleNamespace(status="꺼짐")):
			telegram_link = importlib.import_module("agency.telegram_link")
		state = {"asks": {"synthetic": {"no": 1, "done": False}, "other": {"no": 2, "done": False}}, "answered": 0}
		with mock.patch.object(telegram_link.telegram_ask, "current", return_value={"value": "사무실에서 답할게"}), \
				mock.patch.object(telegram_link, "_ask") as send:
			telegram_link._sync(project.load(self.identity), state)
			send.assert_not_called()
		self.assertTrue(state["asks"]["synthetic"]["done"])
		self.assertEqual([q["no"] for q in agency.detail(self.identity, "questions")["questions"]], [1, 2])
		with mock.patch.object(telegram_link, "_ask") as send:
			code, result = self.post(action="agency_answer", no=1, text="합성 답변")
			send.assert_not_called()
		self.assertEqual((code, result["ok"]), (200, True))
		self.assertEqual(project.load(self.identity)["answers"][0]["answer"], "합성 답변")
		self.assertEqual([q["no"] for q in agency.detail(self.identity, "questions")["questions"]], [2])
		self.assertEqual(self.post(action="agency_answer", no=1, text="다른 답")[0], 409)
		self.assertEqual(self.logs(), [{"action": "agency_answer", "id": self.identity, "result": "완료"},
			{"action": "agency_answer", "id": self.identity, "result": "거부"}])
		with mock.patch.object(telegram_link, "client", SimpleNamespace(status="켜짐")), \
				mock.patch.object(telegram_link, "_sync") as sync:
			telegram_link.sync()
			sync.assert_not_called()
		fake_client = SimpleNamespace(status="켜짐", send_message=mock.Mock())
		with mock.patch.object(telegram_link, "client", fake_client):
			telegram_link.notify(self.identity, "접수 중", "납품 대기")
		fake_client.send_message.assert_called_once_with("회사 프로젝트 1건 상태 변경, 사무실에서 확인")

	def test_partial_answer_failure_is_not_repeated(self):
		with mock.patch.object(intake, "_save_questions", side_effect=OSError):
			self.assertEqual(self.post(action="agency_answer", no=1, text="한 번만")[0], 500)
		self.assertEqual(self.post(action="agency_answer", no=1, text="반복")[0], 409)
		self.assertEqual(len(project.load(self.identity)["answers"]), 1)

	def test_confirmation_answer_reports_transition_to_spec_confirmed(self):
		project.update_spec(self.identity, goal="합성 목표", target="합성 대상", out_of_scope="합성 제외",
			done_criteria=["합성 기준"])
		intake._save_questions(self.identity, {"open": [{"no": 3, "question": intake.CONFIRM_PREFIX
			+ "첫째 줄\n둘째 줄\n셋째 줄", "asked": "2026-10-28T10:00:00+09:00"}], "next_no": 4})
		code, result = self.post(action="agency_answer", no=3, text="맞아")
		self.assertEqual((code, result["status"]), (200, "명세 확정"))
		self.assertEqual(project.load(self.identity)["status"], "명세 확정")

	def test_payload_and_http_guards(self):
		valid = {"action": "agency_answer", "id": self.identity, "no": 1, "text": "가" * 1000}
		bad = [{**valid, "id": value} for value in ("../xyz", "p-20261399-001", "p-20261028-000")]
		bad += [{**valid, "no": value} for value in (0, True, "1", -1)]
		bad += [{**valid, "text": value} for value in ("", "가" * 1001, "가\n나", "가\x7f나", 3)]
		bad += [{**valid, "extra": "xyz"}, {"action": "agency_approve", "id": self.identity, "hash": "A" * 64}]
		bad += [{"action": action, "id": self.identity, "hash": digest, **extra}
			for action, extra in (("agency_approve", {}), ("agency_reject", {"text": "합성 이유"}))
			for digest in ("a" * 63, "g" * 64, 1, None)]
		with mock.patch.object(intake, "answer") as call:
			for payload in bad:
				with self.subTest(payload=repr(payload)[:100]):
					self.assertEqual(self.request(payload)[0], 400)
			for expected, headers in ((403, {"Origin": None}), (403, {"Origin": "http://invalid"}),
					(403, {"X-Office-Token": "wrong"}), (403, {"Host": "invalid"}), (415, {"Content-Type": "text/plain"})):
				self.assertEqual(self.request(valid, headers=headers)[0], expected)
			self.assertEqual(self.request(valid, body=b" " * 16385)[0], 413)
			self.assertEqual(self.request(valid, body=b"{")[0], 400)
			call.assert_not_called()
		self.assertEqual(self.request(valid)[0], 200)

	def test_delivery_posts_use_the_same_http_security_boundary(self):
		for action in ("agency_approve", "agency_reject"):
			payload = {"action": action, "id": self.identity, "hash": self.bundle["hash"]}
			if action == "agency_reject":
				payload["text"] = "합성 이유"
			with self.subTest(action=action), mock.patch.object(agency, "execute") as execute:
				for expected, headers in ((403, {"Origin": "http://invalid"}), (403, {"X-Office-Token": "wrong"}),
						(403, {"Host": "invalid"}), (415, {"Content-Type": "text/plain"})):
					self.assertEqual(self.request(payload, headers=headers)[0], expected)
				self.assertEqual(self.request(payload, body=b" " * 16385)[0], 413)
				self.assertEqual(self.request({**payload, "extra": "xyz"})[0], 400)
				execute.assert_not_called()

	def test_detail_requires_session_and_does_not_return_spec_or_answers(self):
		path = "/api/agency?id=" + self.identity + "&view=questions"
		self.assertEqual(self.request(method="GET", path=path, headers={"X-Office-Token": None})[0], 403)
		status, _, content = self.request(method="GET", path=path)
		data = json.loads(content)
		self.assertEqual(status, 200)
		self.assertEqual(set(data), {"id", "title", "questions"})
		self.assertNotIn("asked", data["questions"][0])

	def test_periodic_evidence_projects_states_and_counts_without_private_fields(self):
		row = {"id": self.identity, "audit_report_mode": "explicit-v1", "schedule_repo": "synthetic",
			"tasks": [{"id": "t1"}]}
		report = {"task": "t1", **{key: "b" * 64 for key in
			("sha256", "request_sha256", "evidence_sha256", "report_seal_sha256")}}
		bundle = {"version": 2, "documents": [report]}
		counts = {"ran": 3, "failures": 0, "errors": 0, "skipped": 1}
		evidence = {"checks": [{"kind": "tests", "status": "unverified", "reason": "합성 비공개"},
			{"kind": "dependencies", "status": "unverified"}],
			"synthetic_test": {"status": "passed", "counts": counts, "name": "합성 비공개"}}
		with mock.patch.object(agency.documents, "periodic_manifest", return_value=[report]), \
				mock.patch.object(agency.documents, "read_evidence", return_value=evidence):
			result = agency.periodic_evidence(row, bundle)
			self.assertEqual(result, {"verified": True, "checks": [
				{"kind": "tests", "status": "unverified"}, {"kind": "dependencies", "status": "unverified"}],
				"synthetic": [{"status": "passed", "counts": counts}]})
			self.assertNotIn("합성 비공개", json.dumps(result, ensure_ascii=False))
			for status in ("passed", "failed", "unverified"):
				evidence["checks"][1]["status"] = status
				self.assertEqual(agency.periodic_evidence(row, bundle)["checks"][1]["status"], status)
			counts["ran"] = 0
			self.assertFalse(agency.periodic_evidence(row, bundle)["verified"])
			counts["ran"] = 3
			evidence["facts"] = {"partial": evidence.pop("synthetic_test")}
			self.assertEqual(agency.periodic_evidence(row, bundle)["synthetic"], [{"status": "passed", "counts": counts}])

	def test_periodic_integrity_failure_blocks_approval_without_receipt(self):
		row = {**project.load(self.identity), "status": "납품 대기", "audit_report_mode": "explicit-v1",
			"schedule_repo": "synthetic", "tasks": [{"id": "t1"}]}
		report = {"task": "t1", **{key: "b" * 64 for key in
			("sha256", "request_sha256", "evidence_sha256", "report_seal_sha256")}}
		bundle = {"version": 2, "hash": "a" * 64, "criteria": [], "documents": [report]}
		for boundary in ("periodic_manifest", "read_evidence"):
			with self.subTest(boundary=boundary), mock.patch.object(agency.project, "load", return_value=row), \
					mock.patch.object(agency.delivery, "load_bundle", return_value=bundle), \
					mock.patch.object(agency.documents, "periodic_manifest", return_value=[report]), \
					mock.patch.object(agency.documents, "read_evidence", return_value={}), \
					mock.patch.object(agency.documents, boundary, side_effect=ValueError("합성 변조")), \
					mock.patch.object(agency, "reserve") as reserve, \
					mock.patch.object(agency.delivery, "approve") as approve:
				self.assertFalse(agency.detail(self.identity, "delivery")["periodic_evidence"]["verified"])
				code, result = agency.execute(self.hub, {"action": "agency_approve", "id": self.identity,
					"hash": bundle["hash"]}, mock.Mock())
				self.assertEqual((code, result["ok"]), (409, False))
				reserve.assert_not_called()
				approve.assert_not_called()
		with mock.patch.object(agency.documents, "periodic_manifest", return_value=[report]):
			self.assertFalse(agency.periodic_evidence(row, {**bundle, "documents": []})["verified"])
			self.assertFalse(agency.periodic_evidence(row, {**bundle, "documents": [report, report]})["verified"])
		with mock.patch.object(agency.project, "load", return_value=row), \
				mock.patch.object(agency.delivery, "load_bundle", side_effect=ValueError("합성 비공개")):
			result = agency.detail(self.identity, "delivery")
			self.assertFalse(result["periodic_evidence"]["verified"])
			self.assertEqual(result["hash"], "")
			self.assertNotIn("합성 비공개", json.dumps(result, ensure_ascii=False))

	def test_delivery_approval_hash_failure_reason_and_duplicate(self):
		self.waiting()
		def approve(identity, digest):
			project.transition(identity, "납품 중")
			project.transition(identity, "막힘", reason="합성 push 실패")
			return {"status": "막힘", "pushed": False, "remote_matches": False}
		with mock.patch.object(agency.delivery, "load_bundle", return_value=self.bundle), \
				mock.patch.object(agency.delivery, "approve", side_effect=approve) as call:
			self.assertEqual(agency.detail(self.identity, "delivery")["changed"], 2)
			self.assertEqual(self.post(action="agency_approve", hash="b" * 64)[0], 409)
			code, value = self.post(action="agency_approve", hash=self.bundle["hash"])
			self.assertEqual((code, value["ok"], value["reason"]), (409, False, "합성 push 실패"))
			self.assertFalse(value["pushed"] or value["remote_matches"])
			self.assertEqual(self.post(action="agency_approve", hash=self.bundle["hash"])[0], 409)
			call.assert_called_once_with(self.identity, self.bundle["hash"])
		self.assertNotIn("합성 push 실패", json.dumps(self.logs(), ensure_ascii=False))

	def test_document_bundle_detail_and_rejection_use_project_workdir(self):
		self.waiting()
		document = {"version": 2, "hash": "c" * 64, "criteria": [{"criterion": "합성 기준", "verdict": "pass"}],
			"documents": [{"task": "t1", "path": "합성", "sha256": "d" * 64}]}
		with mock.patch.object(agency.delivery, "load_bundle", return_value=document):
			detail = agency.detail(self.identity, "delivery")
			self.assertEqual((detail["kind"], detail["documents"], detail["actions"]), ("document", ["t1"], ["document"]))
			self.assertNotIn("tests", detail)
			with mock.patch.object(agency.delivery, "reject", side_effect=lambda identity, reason:
					project.transition(identity, "막힘", reason="납품 반려: " + reason)) as call:
				self.assertEqual(self.post(action="agency_reject", hash=document["hash"], text="합성 반려 사유")[0], 200)
			call.assert_called_once_with(self.identity, "합성 반려 사유")

	def test_rejection_checks_hash_and_duplicate(self):
		self.waiting()
		def reject(identity, reason):
			return project.transition(identity, "막힘", reason="납품 반려: " + reason)
		with mock.patch.object(agency.delivery, "load_bundle", return_value=self.bundle), \
				mock.patch.object(agency.delivery, "reject", side_effect=reject) as call:
			self.assertEqual(self.post(action="agency_reject", hash="b" * 64, text="이유")[0], 409)
			self.assertEqual(self.post(action="agency_reject", hash=self.bundle["hash"], text="합성 반려 사유")[0], 200)
			self.assertEqual(self.post(action="agency_reject", hash=self.bundle["hash"], text="이유")[0], 409)
			project.resume(self.identity)
			code, result = self.post(action="agency_reject", hash=self.bundle["hash"], text="이유")
			self.assertEqual(code, 409)
			self.assertIn("이미 처리", result["message"])
			self.assertEqual(self.post(action="agency_approve", hash=self.bundle["hash"])[0], 409)
			call.assert_called_once_with(self.identity, "합성 반려 사유")
		self.assertNotIn("합성 반려 사유", json.dumps(self.logs(), ensure_ascii=False))

	def test_delivery_receipt_symlink_blocks_execution(self):
		self.waiting()
		(self.hub / "run").mkdir(exist_ok=True)
		(self.hub / "run/agency-receipts").symlink_to(self.root, target_is_directory=True)
		with mock.patch.object(agency.delivery, "load_bundle", return_value=self.bundle), \
				mock.patch.object(agency.delivery, "approve") as call:
			self.assertEqual(self.post(action="agency_approve", hash=self.bundle["hash"])[0], 409)
			call.assert_not_called()

	def test_rejection_waits_for_the_repository_lock_and_rechecks_hash(self):
		self.waiting()
		changed = {**self.bundle, "hash": "b" * 64}
		with mock.patch.object(agency.delivery, "load_bundle", side_effect=[self.bundle, self.bundle, changed]), \
				mock.patch.object(agency.delivery, "reject") as call:
			with locked("agency-repo:" + self.bundle["repo"]):
				self.assertEqual(self.post(action="agency_reject", hash=self.bundle["hash"], text="이유")[0], 409)
			self.assertEqual(self.post(action="agency_reject", hash=self.bundle["hash"], text="이유")[0], 409)
			call.assert_not_called()

	def test_uncertain_delivery_is_not_retried_on_the_same_bundle(self):
		self.waiting()
		with mock.patch.object(agency.delivery, "load_bundle", return_value=self.bundle), \
				mock.patch.object(agency.delivery, "approve", side_effect=OSError("합성 오류")) as call:
			self.assertEqual(self.post(action="agency_approve", hash=self.bundle["hash"])[0], 500)
			code, result = self.post(action="agency_approve", hash=self.bundle["hash"])
			self.assertEqual(code, 409)
			self.assertIn("불확실", result["message"])
			call.assert_called_once()

	def test_concurrent_requests_call_approval_once(self):
		self.waiting()
		started, release = threading.Event(), threading.Event()
		def approve(identity, digest):
			started.set()
			if not release.wait(5):
				raise RuntimeError("가짜 납품 대기 실패")
			project.transition(identity, "납품 중")
			project.transition(identity, "완료")
			return {"status": "완료", "pushed": True, "remote_matches": True}
		with mock.patch.object(agency.delivery, "load_bundle", return_value=self.bundle), \
				mock.patch.object(agency.delivery, "approve", side_effect=approve) as call, ThreadPoolExecutor() as pool:
			first = pool.submit(self.post, action="agency_approve", hash=self.bundle["hash"])
			try:
				self.assertTrue(started.wait(5))
				self.assertEqual(self.post(action="agency_approve", hash=self.bundle["hash"])[0], 409)
			finally:
				release.set()
			self.assertEqual(first.result(timeout=5)[0], 200)
			call.assert_called_once()
