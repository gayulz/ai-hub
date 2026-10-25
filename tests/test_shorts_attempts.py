"""외부 호출 없는 시도 원장, 복구·대조·취소·자동 수정 한도."""

import copy
import json
import os
from unittest import mock

from shorts import Conflict, Store, StoreError, TransitionError, approvals, attempts, files, schema, slot, states
from test_shorts_support import HASH, SID, ShortsCase


class AttemptTest(ShortsCase):
	def prepare(self):
		self.to_stage("prepare")
		self.call(approvals.start_stage)

	def test_intent_is_fsynced_on_disk_before_the_synthetic_call(self):
		self.prepare()
		writes, calls = [], []
		real_sync = os.fsync
		def sync(fd):
			writes.append(os.fstat(fd).st_ino)
			return real_sync(fd)
		with mock.patch.object(files.os, "fsync", side_effect=sync):
			row = self.call(attempts.intend, "prepare_workspace", HASH)
		def synthetic_call():
			disk = json.loads((self.round_path() / "run.json").read_text())
			self.assertEqual(disk["attempts"][-1], row)
			self.assertEqual(row["status"], "intended")
			self.assertRegex(row["attempt_id"], r"^a-[0-9a-f]{32}$")
			self.assertIn((self.round_path() / "run.json").stat().st_ino, writes)
			self.assertEqual(slot.owner(Store())["attempt_id"], row["attempt_id"])
			calls.append("합성 호출")
		synthetic_call()
		self.assertEqual(calls, ["합성 호출"])

	def test_crash_before_call_or_before_external_id_is_saved_is_needs_check(self):
		self.prepare()
		row = self.call(attempts.intend, "prepare_workspace", HASH)
		self.assertIsNone(row["external_id"])
		# 호출 직전 중단과 접수 직후 식별자 저장 전 중단은 디스크에서 구별할 수 없다.
		recovered = attempts.recover(Store(), SID, self.number)
		self.assertEqual((recovered["status"], recovered["attempts"][0]["status"]), ("needs_check", "needs_check"))
		self.assertIsNone(recovered["attempts"][0]["external_id"])
		self.assertIsNotNone(slot.owner(self.store))
		with self.assertRaises(TransitionError):
			self.call(attempts.intend, "prepare_workspace", HASH)
		with self.assertRaises(TransitionError):
			self.call(approvals.start_stage)
		with self.assertRaises(Conflict):
			self.store.new(SID)
		self.assertEqual(len(self.read()["attempts"]), 1)

	def test_running_attempt_recovery_retains_external_id_without_retrying(self):
		self.prepare()
		row = self.call(attempts.intend, "check_environment", HASH)
		self.call(attempts.mark_running, row["attempt_id"], external_id="remote-001")
		run = self.call(attempts.recover)
		self.assertEqual((run["attempts"][0]["status"], run["attempts"][0]["external_id"]), ("needs_check", "remote-001"))
		self.assertEqual(self.call(attempts.recover), run)
		with self.assertRaises(TransitionError):
			self.call(attempts.finish, row["attempt_id"], "succeeded", confirmed_end=True)

	def test_resolve_each_result_persists_human_note_then_allows_a_new_attempt(self):
		self.prepare()
		for result, status in (("ended_ok", "succeeded"), ("ended_failed", "failed"), ("not_started", "cancelled")):
			with self.subTest(result=result):
				row = self.call(attempts.intend, "check_environment", HASH)
				self.call(attempts.recover)
				resolved = self.call(attempts.resolve, row["attempt_id"], result, "합성 대조 근거를 확인함")
				self.assertEqual(resolved["attempts"][-1]["status"], status)
				self.assertEqual(resolved["attempts"][-1]["resolution"]["result"], result)
				self.assertEqual(resolved["attempts"][-1]["resolution"]["note"], "합성 대조 근거를 확인함")
				self.assertEqual(resolved["status"], "waiting")
				self.assertIsNone(slot.owner(self.store))
				self.call(approvals.start_stage)
		self.assertEqual(len(self.read()["attempts"]), 3)

	def test_unconfirmed_finish_or_wrong_resolution_cannot_free_slot(self):
		self.prepare()
		row = self.call(attempts.intend, "check_environment", HASH)
		with self.assertRaises(TransitionError):
			self.call(attempts.resolve, row["attempt_id"], "not_started", "합성 확인")
		self.call(attempts.mark_running, row["attempt_id"])
		for confirmation in (False, 1, None):
			with self.assertRaises(TransitionError):
				self.call(attempts.finish, row["attempt_id"], "succeeded", confirmed_end=confirmation)
		for error in ("원문 오류", "x" * 1000):
			with self.assertRaises(StoreError):
				self.call(attempts.finish, row["attempt_id"], "failed", error_code=error, confirmed_end=True)
		self.assertEqual(self.read()["attempts"][0]["status"], "running")
		self.assertIsNotNone(slot.owner(self.store))
		run = self.call(attempts.finish, row["attempt_id"], "succeeded", result_digest=HASH, confirmed_end=True)
		self.assertEqual(run["attempts"][0]["result_digest"], HASH)
		self.assertIsNone(slot.owner(self.store))

	def test_independent_operation_allowlist_for_every_stage(self):
		expected = {"intake": set(), "prepare": {"prepare_workspace", "check_environment"}, "script": set(),
			"voice": {"generate_voice", "transcribe", "analyze_beats", "probe_media"}, "scene_code": set(),
			"preview": {"render_preview", "capture_frames", "probe_media"},
			"final": {"render_final", "capture_frames", "probe_media"}, "deliver": {"copy_outputs"}}
		self.assertEqual(set().union(*expected.values()), {"prepare_workspace", "check_environment", "generate_voice",
			"transcribe", "analyze_beats", "render_preview", "render_final", "capture_frames", "probe_media", "copy_outputs"})
		for stage, allowed in expected.items():
			for kind in (*set().union(*expected.values()), "arbitrary"):
				row = {"attempt_id": "a-" + "a" * 32, "stage": stage, "kind": kind, "input_digest": HASH,
					"status": "intended", "external_id": None, "result_digest": None, "error_code": "",
					"created": schema.now(), "updated": schema.now(), "resolution": None}
				with self.subTest(stage=stage, kind=kind):
					if kind in allowed:
						schema.validate_attempt(row)
					else:
						with self.assertRaises(StoreError):
							schema.validate_attempt(row)
		self.prepare()
		for kind in ("arbitrary", "render_final", "generate_voice"):
			with self.assertRaises(StoreError):
				self.call(attempts.intend, kind, HASH)
		self.assertIsNone(slot.owner(self.store))
		self.assertEqual(self.read()["attempts"], [])

	def test_cancel_requires_request_then_confirmed_external_and_child_end(self):
		self.prepare()
		row = self.call(attempts.intend, "check_environment", HASH)
		self.call(attempts.mark_running, row["attempt_id"])
		with self.assertRaises(TransitionError):
			self.call(attempts.confirm_cancelled, confirmed_end=True)
		run = self.call(attempts.request_cancel)
		self.assertEqual(run["status"], "cancelling")
		with self.assertRaises(TransitionError):
			self.call(attempts.confirm_cancelled)
		self.assertIsNotNone(slot.owner(self.store))
		run = self.call(attempts.confirm_cancelled, confirmed_end=True)
		self.assertEqual(run["status"], "cancelled")
		self.assertEqual(run["attempts"][0]["status"], "cancelled")
		self.assertIsNone(slot.owner(self.store))
		with self.assertRaises(TransitionError):
			self.call(attempts.intend, "check_environment", HASH)

	def test_auto_fix_limit_survives_new_attempt_ids_and_stage_changes(self):
		self.prepare()
		self.call(attempts.consume_auto_fix, "scene-01:contrast")
		identities = []
		for _ in range(2):
			row = self.call(attempts.intend, "check_environment", HASH)
			identities.append(row["attempt_id"])
			self.call(attempts.mark_running, row["attempt_id"])
			self.call(attempts.finish, row["attempt_id"], "failed", confirmed_end=True, error_code="tool_failed")
			with self.assertRaises(Conflict):
				self.call(attempts.consume_auto_fix, "scene-01:contrast")
		self.assertNotEqual(*identities)
		self.call(approvals.complete_stage)
		with self.assertRaises(Conflict):
			self.call(attempts.consume_auto_fix, "scene-01:contrast")
		self.call(attempts.consume_auto_fix, "scene-02:contrast")
		self.assertEqual(self.read()["review_items"], {"scene-01:contrast": 1, "scene-02:contrast": 1})

	def test_strict_attempt_schema_rejects_raw_errors_bad_ids_and_resolution(self):
		self.prepare()
		row = self.call(attempts.intend, "check_environment", HASH)
		for change in ({"extra": "원문"}, {"attempt_id": "a-1"}, {"input_digest": "원문"},
				{"error_code": "도구 출력 원문"}, {"external_id": "a" * 201}, {"result_digest": "원문"},
				{"kind": "generate_voice"}, {"resolution": {"result": "unknown"}}):
			with self.subTest(keys=list(change)), self.assertRaises(StoreError):
				schema.validate_attempt({**copy.deepcopy(row), **change})
