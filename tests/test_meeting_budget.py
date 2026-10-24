"""실패·재개와 사람 대기 시간을 포함한 회의 예산."""

from unittest import mock

from test_meeting import MeetingCase, question


class MeetingBudgetTest(MeetingCase):
	def test_failure_is_recorded_and_resume_keeps_spent_calls(self):
		state = self.analyze()
		self.ai.fail_at = 2
		state = self.start(state)
		self.assertEqual(state["status"], "failed")
		self.assertEqual(len(state["calls"]), 2)
		self.assertFalse(state["calls"][-1]["ok"])
		self.assertNotIn("외부 원문", str(state["failure"]))
		self.ai.fail_at = None
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "completed")
		self.assertEqual(len(state["calls"]), 7)

	def test_four_people_failure_reserves_summary_and_skips_second_round(self):
		self.ai.roles = ("plan", "backend", "frontend", "qa")
		state = self.analyze()
		self.ai.fail_at = 2
		state = self.start(state)
		self.ai.fail_at = None
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "budget_exhausted")
		self.assertIsNotNone(state["summary"])
		self.assertEqual(len(state["rounds"]["1"]), 4)
		self.assertEqual(state["rounds"]["2"], [])
		self.assertLessEqual(len(state["calls"]), 10)
		count = len(self.ai.calls)
		self.engine.resume(state["id"])
		self.assertEqual(len(self.ai.calls), count)

	def test_repeated_failure_and_resume_never_exceeds_ten_calls(self):
		state = self.analyze()
		for _ in range(15):
			self.ai.fail_at = len(self.ai.calls) + 1
			if not state["confirmed"]:
				state = self.start(state)
			else:
				state = self.engine.resume(state["id"])
		self.assertLessEqual(len(self.ai.calls), 10)
		self.assertNotEqual(state["status"], "completed")

	def test_waiting_time_is_excluded_and_timeout_never_uses_provider_floor(self):
		self.ai.analysis_questions = [question()]
		clock = [0.0]
		with mock.patch("orchestrator.meeting.time.monotonic", side_effect=lambda: clock[0]):
			state = self.analyze()
			clock[0] += 5000
			self.assertLess(self.engine.resume(state["id"])["active_seconds"], 1)
		self.assertTrue(all(0 < call[1]["timeout"] <= 1200 for call in self.ai.calls))

	def test_time_exhausted_and_interrupted_call_reservation_survive_resume(self):
		state = self.analyze()
		with self.engine.store.locked(state["id"]) as record:
			state = record.load()
			state["active_seconds"] = 1190
			record.save(state)
		count = len(self.ai.calls)
		state = self.start(state)
		self.assertEqual(state["status"], "budget_exhausted")
		self.assertEqual(len(self.ai.calls), count)
		state = self.analyze()
		with self.engine.store.locked(state["id"]) as record:
			state = record.load()
			state["calls"].append({"phase": "round1", "role": "backend", "plan": "p01", "ok": None, "timeout": 1170})
			state["active_seconds"] = 30
			record.save(state)
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "budget_exhausted")
		self.assertFalse(state["calls"][-1]["ok"])

	def test_status_detects_an_interrupted_attempt_without_calling_ai(self):
		state = self.analyze()
		with self.engine.store.locked(state["id"]) as record:
			state.update(status="running", confirmed=True)
			state["calls"].append({"phase": "round1", "role": "backend", "plan": "p01", "ok": None, "timeout": 50})
			record.save(state)
		state = self.engine.status(state["id"])
		self.assertEqual(state["status"], "failed")
		self.assertFalse(state["calls"][-1]["ok"])
		self.assertGreaterEqual(state["active_seconds"], 50)
		self.assertEqual(len(self.ai.calls), 1)

	def test_summary_failure_can_resume_without_repeating_rounds(self):
		state = self.analyze()
		self.ai.fail_at = 6
		state = self.start(state)
		self.assertEqual(state["status"], "failed")
		self.assertEqual(state["phase"], "summary")
		self.ai.fail_at = None
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "completed")
		self.assertEqual([c["phase"] for c in state["calls"]].count("summary"), 2)
		self.assertEqual(len(state["calls"]), 7)

	def test_call_timeout_includes_checkpoint_time_and_a_slow_call_is_not_complete(self):
		state = self.analyze()
		clock = [0.0]
		original = self.ai.__call__
		def slow(plan, **kwargs):
			clock[0] += 1201
			return original(plan, **kwargs)
		with mock.patch("orchestrator.meeting.time.monotonic", side_effect=lambda: clock[0]), \
				mock.patch("orchestrator.providers.call", side_effect=slow):
			state = self.start(state)
		self.assertEqual(state["status"], "budget_exhausted")
		self.assertIsNone(state["summary"])
		self.assertEqual(len(state["calls"]), 2)
		self.assertIn("response", state["calls"][-1])
