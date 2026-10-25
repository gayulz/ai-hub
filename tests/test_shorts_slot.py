"""여러 접수의 단일 슬롯과 소유자 상실 시 보수적인 복구."""

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from shorts import Conflict, Store, StoreError, TransitionError, approvals, attempts, slot
from test_shorts_support import HASH, OTHER, SID, ShortsCase


class SlotTest(ShortsCase):
	def test_one_disk_owner_safe_conflict_and_no_timeout_takeover(self):
		value = slot.acquire(self.store, SID, 1, "a-" + "a" * 32)
		self.assertEqual(stat.S_IMODE((self.hub / "shorts/.slot").stat().st_mode), 0o600)
		self.assertEqual(set(value), {"intake_id", "round", "attempt_id", "pid", "started"})
		with mock.patch("shorts.schema.now", return_value="2099-01-01T12:00:00+09:00"):
			with self.assertRaises(Conflict) as caught:
				slot.acquire(Store(), OTHER, 1, "a-" + "b" * 32)
		self.assertEqual(caught.exception.owner, value)
		self.assertEqual(slot.owner(Store()), value)
		for confirmed in (False, None, 1):
			with self.assertRaises(TransitionError):
				slot.release(self.store, value, confirmed_end=confirmed)
		self.assertEqual(slot.owner(self.store), value)
		slot.release(self.store, value, confirmed_end=True)
		self.assertIsNone(slot.owner(self.store))

	def test_concurrent_intakes_cannot_both_intend_external_work(self):
		self.make_intake(OTHER)
		self.store.new(OTHER)
		for identity in (SID, OTHER):
			approvals.start_stage(self.store, identity, 1)
			approvals.complete_stage(self.store, identity, 1)
			approvals.start_stage(self.store, identity, 1)
		gate = threading.Barrier(2)
		def intend(identity):
			gate.wait(timeout=5)
			try:
				return attempts.intend(Store(), identity, 1, "check_environment", HASH)
			except Conflict as error:
				return error
		with ThreadPoolExecutor(max_workers=2) as pool:
			results = list(pool.map(intend, (SID, OTHER)))
		self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1)
		self.assertEqual(sum(len(self.store.read(identity, 1)["attempts"]) for identity in (SID, OTHER)), 1)
		self.assertEqual(next(result for result in results if isinstance(result, Conflict)).owner, slot.owner(self.store))

	def test_stale_release_does_not_delete_a_new_owner(self):
		first = slot.acquire(self.store, SID, 1, "a-" + "a" * 32)
		slot.release(self.store, first, confirmed_end=True)
		second = slot.acquire(self.store, OTHER, 1, "a-" + "b" * 32)
		with self.assertRaises(Conflict):
			slot.release(self.store, first, confirmed_end=True)
		self.assertEqual(slot.owner(self.store), second)

	def test_missing_process_marks_needs_check_and_keeps_slot_until_resolved(self):
		self.to_stage("prepare")
		self.call(approvals.start_stage)
		row = self.call(attempts.intend, "check_environment", HASH)
		value = slot.owner(self.store)
		with mock.patch.object(attempts.os, "kill", side_effect=ProcessLookupError):
			self.assertEqual(attempts.check_owner(Store()), value)
		self.assertEqual(self.read()["status"], "needs_check")
		self.assertEqual(self.read()["attempts"][0]["error_code"], "owner_lost")
		self.assertEqual(slot.owner(self.store), value)
		with self.assertRaises(Conflict):
			slot.acquire(self.store, OTHER, 1, "a-" + "b" * 32)
		self.call(attempts.resolve, row["attempt_id"], "ended_failed", "합성 외부 작업 종료를 대조함")
		self.assertIsNone(slot.owner(self.store))

	def test_slot_reserved_before_intent_save_needs_explicit_not_started_resolution(self):
		value = slot.acquire(self.store, SID, 1, "a-" + "a" * 32)
		run = self.call(attempts.recover)
		self.assertEqual((run["status"], run["blocked_reason"]), ("needs_check", "unknown_attempt"))
		self.assertEqual(slot.owner(self.store), value)
		with self.assertRaises(Conflict):
			self.call(attempts.resolve, value["attempt_id"], "ended_ok", "합성 대조")
		self.call(attempts.resolve, value["attempt_id"], "not_started", "호출 전 중단을 확인함")
		self.assertEqual(self.read()["status"], "waiting")
		self.assertIsNone(slot.owner(self.store))

	def test_awaiting_user_requires_settled_attempt_and_releases_a_retained_slot(self):
		self.to_stage("preview")
		self.call(approvals.start_stage)
		self.add("preview.txt", "preview")
		row = self.call(attempts.intend, "render_preview", HASH)
		self.call(attempts.mark_running, row["attempt_id"])
		with self.assertRaises(TransitionError):
			self.call(approvals.complete_stage)
		self.assertIsNotNone(slot.owner(self.store))
		with mock.patch("shorts.attempts.slot.release"):
			self.call(attempts.finish, row["attempt_id"], "succeeded", confirmed_end=True, result_digest=HASH)
		self.assertIsNotNone(slot.owner(self.store))
		run = self.call(approvals.complete_stage)
		self.assertEqual(run["status"], "awaiting_user")
		self.assertIsNone(slot.owner(self.store))

	def test_linked_slot_or_lock_and_corrupt_owner_never_get_reclaimed(self):
		target = self.root / "target"
		target.write_bytes(b"original")
		target.chmod(0o600)
		for name in (".slot", ".slot.lock"):
			path = self.hub / "shorts" / name
			if path.exists():
				path.unlink()
			path.symlink_to(target)
			try:
				with self.assertRaises(StoreError):
					slot.acquire(self.store, SID, 1, "a-" + "a" * 32)
			finally:
				path.unlink()
		self.assertEqual(target.read_bytes(), b"original")
		path = self.hub / "shorts/.slot"
		path.write_text("{}", encoding="utf-8")
		path.chmod(0o600)
		with self.assertRaises(StoreError):
			slot.acquire(self.store, SID, 1, "a-" + "a" * 32)
		self.assertEqual(path.read_text(), "{}")
