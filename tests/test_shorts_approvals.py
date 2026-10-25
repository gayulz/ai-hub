"""승인의 식별값·판 검사, 동시 클릭과 두 롤백 경로."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from shorts import Conflict, StoreError, TransitionError, approvals, attempts, schema, states
from test_shorts_support import HASH, ShortsCase


class ApprovalTest(ShortsCase):
	def test_mismatched_digest_revision_stage_and_repeated_click(self):
		run = self.pending()
		digest, revision = run["review"]["digest"], run["revision"]
		for stage, value, rev in (("script", HASH, revision), ("script", digest, revision - 1),
				("preview", digest, revision), ("script", digest, True)):
			with self.subTest(stage=stage, revision=rev), self.assertRaises(Conflict):
				self.call(approvals.approve, stage, value, rev)
		self.assertEqual(self.read(), run)
		approved = self.call(approvals.approve, "script", digest, revision)
		self.assertEqual((approved["stage"], approved["status"]), ("voice", "waiting"))
		self.assertEqual(approved["approvals"]["script"]["intake_id"], run["intake_id"])
		self.assertEqual(approved["approvals"]["script"]["round"], self.number)
		with self.assertRaises(Conflict):
			self.call(approvals.approve, "script", digest, revision)

	def test_two_concurrent_identical_approvals_succeed_exactly_once(self):
		run = self.pending()
		gate = threading.Barrier(2)
		def click():
			gate.wait(timeout=5)
			try:
				self.call(approvals.approve, "script", run["review"]["digest"], run["revision"])
				return "ok"
			except Conflict:
				return "conflict"
		with ThreadPoolExecutor(max_workers=2) as pool:
			results = list(pool.map(lambda _: click(), range(2)))
		self.assertCountEqual(results, ("ok", "conflict"))
		self.assertEqual(self.read()["revision"], run["revision"] + 1)

	def test_changed_bundle_rejects_approval_from_an_open_screen(self):
		run = self.pending()
		changed = self.add("script-v2.txt", "script", b"second")
		self.assertNotEqual(run["review"]["digest"], changed["review"]["digest"])
		with self.assertRaises(Conflict):
			self.call(approvals.approve, "script", run["review"]["digest"], run["revision"])
		with self.assertRaises(Conflict):
			self.call(approvals.approve, "script", run["review"]["digest"], changed["revision"])

	def test_script_change_invalidates_both_approvals_and_rolls_back(self):
		self.to_stage("final")
		before = self.read()
		self.assertTrue(states.approval_valid(before, "script"))
		self.assertTrue(states.approval_valid(before, "preview"))
		run = self.add("script-v2.txt", "script", b"changed")
		self.assertEqual(run["approvals"], {"script": None, "preview": None})
		self.assertEqual((run["stage"], run["status"]), ("script", "awaiting_user"))
		for stage in ("script", "preview"):
			with self.assertRaises(Conflict):
				self.call(approvals.approve, stage, before["approvals"][stage]["digest"], run["revision"])
		self.call(approvals.approve, "script", run["review"]["digest"], run["revision"])
		self.assertEqual(self.read()["stage"], "voice")

	def test_voice_scene_and_preview_changes_invalidate_only_preview(self):
		for kind in ("voice", "scene_code", "preview"):
			with self.subTest(kind=kind):
				self.to_stage("final")
				before = self.read()
				run = self.add(kind + "-v2.txt", kind)
				self.assertEqual(run["approvals"]["script"], before["approvals"]["script"])
				self.assertIsNone(run["approvals"]["preview"])
				self.assertEqual((run["stage"], run["status"]), ("preview", "awaiting_user"))
				with self.assertRaises(Conflict):
					self.call(approvals.approve, "preview", before["approvals"]["preview"]["digest"], run["revision"])
				self.call(approvals.approve, "preview", run["review"]["digest"], run["revision"])

	def test_report_and_final_artifacts_do_not_invalidate_approvals(self):
		self.to_stage("final")
		before = self.read()
		for kind in ("report", "final"):
			run = self.add(kind + ".txt", kind)
			self.assertEqual(run["approvals"], before["approvals"])
			self.assertEqual((run["stage"], run["status"]), ("final", "waiting"))

	def test_request_changes_returns_to_same_stage_work(self):
		for stage in ("script", "preview"):
			with self.subTest(stage=stage):
				run = self.pending(stage)
				changed = self.call(approvals.request_changes, stage)
				self.assertEqual((changed["stage"], changed["status"], changed["review"]), (stage, "waiting", None))
				with self.assertRaises(Conflict):
					self.call(approvals.approve, stage, run["review"]["digest"], run["revision"])
				self.call(approvals.start_stage)
				run = self.call(approvals.complete_stage)
				self.call(approvals.approve, stage, run["review"]["digest"], run["revision"])

	def test_voice_and_final_running_require_current_bound_approvals(self):
		for stage in ("voice", "final"):
			with self.subTest(stage=stage):
				with self.store.locked(self.run["intake_id"], self.number) as record:
					record.run.update(stage=stage, status="waiting")
					record.save()
				with self.assertRaises(TransitionError):
					self.call(approvals.start_stage)
				self.assertEqual(self.read()["status"], "waiting")

	def test_cannot_await_or_rollback_while_an_attempt_is_unsettled(self):
		self.to_stage("voice")
		self.call(approvals.start_stage)
		self.call(attempts.intend, "generate_voice", HASH)
		with self.assertRaises(TransitionError):
			self.add("script-v2.txt", "script")
		self.assertFalse((self.round_path() / "artifacts/script-v2.txt").exists())
		with self.assertRaises(TransitionError):
			self.call(approvals.complete_stage)

	def test_approval_cannot_be_transplanted_to_another_intake_round_or_manifest(self):
		self.to_stage("voice")
		original = self.read()
		for key, value in (("intake_id", "s-" + "b" * 32), ("round", 2), ("digest", HASH)):
			run = copy.deepcopy(original)
			run["approvals"]["script"][key] = value
			with self.subTest(key=key), self.assertRaises(StoreError):
				schema.validate(run)

	def test_serialized_running_voice_and_final_cannot_bypass_approval_guards(self):
		for stage in ("voice", "final"):
			run = copy.deepcopy(self.run)
			run.update(stage=stage, status="running")
			with self.subTest(stage=stage), self.assertRaises(StoreError):
				schema.validate(run)
