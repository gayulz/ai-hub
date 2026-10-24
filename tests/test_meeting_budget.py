"""실패·재개와 사람 대기 시간을 포함한 회의 예산."""

import copy
from unittest import mock

from orchestrator import meeting
from orchestrator.providers import ProviderError
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

	def test_four_people_can_resume_a_failure_and_finish_both_rounds_within_budget(self):
		self.ai.roles = ("plan", "backend", "frontend", "qa")
		state = self.analyze()
		self.ai.fail_at = 2
		state = self.start(state)
		self.ai.fail_at = None
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "completed")
		self.assertIsNotNone(state["summary"])
		self.assertEqual(len(state["rounds"]["1"]), 4)
		self.assertEqual(len(state["rounds"]["2"]), 4)
		self.assertEqual(len(state["calls"]), 11)
		count = len(self.ai.calls)
		self.engine.resume(state["id"])
		self.assertEqual(len(self.ai.calls), count)

	def test_repeated_failure_and_resume_never_exceeds_fourteen_calls(self):
		state = self.analyze()
		for _ in range(15):
			self.ai.fail_at = len(self.ai.calls) + 1
			if not state["confirmed"]:
				state = self.start(state)
			else:
				state = self.engine.resume(state["id"])
		self.assertLessEqual(len(self.ai.calls), 14)
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

	def test_retries_reach_fourteen_calls_and_keep_one_summary_call(self):
		self.ai.roles = ("plan", "backend", "frontend", "qa")
		original = self.ai.__call__
		attempts = {}
		def rejected_once(plan, **kwargs):
			reply = original(plan, **kwargs)
			fields = kwargs["schema"]["properties"]
			field = "method" if "method" in fields else "agree" if "agree" in fields else None
			if field:
				key = (field, reply.data["role"])
				attempts[key] = attempts.get(key, 0) + 1
				if attempts[key] == 1:
					reply.data[field] = "" if field == "method" else []
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=rejected_once):
			state = self.start(self.analyze())
		self.assertEqual(meeting.MAX_CALLS, 14)
		self.assertEqual(meeting.MAX_SECONDS, 1200)
		self.assertEqual(state["status"], "budget_exhausted")
		self.assertIsNotNone(state["summary"])
		self.assertEqual(len(state["calls"]), 14)
		self.assertEqual(len(self.ai.calls), 14)
		self.assertEqual(state["calls"][-1]["phase"], "summary")
		self.assertEqual(sum(c["phase"] == "summary" for c in state["calls"]), 1)
		self.assertEqual(self.engine.status(state["id"]), state)
		self.assertEqual(self.engine.store.read(state["id"]), state)
		self.assertIn(state, self.engine.store.list_states())
		self.assertEqual(self.engine.resume(state["id"]), state)
		self.assertEqual(len(self.ai.calls), 14)

	def test_round_budget_boundary_reserves_summary_at_fourteen(self):
		state = self.analyze()
		with self.engine.store.locked(state["id"]) as record:
			for pending in (2, 4):
				for available, allowed in ((pending + 1, True), (pending, False)):
					with self.subTest(pending=pending, available=available):
						current = copy.deepcopy(state)
						current["calls"] *= meeting.MAX_CALLS - available
						session = meeting.Session(record, current, None)
						self.assertEqual(session.budget_for_round(pending), allowed)
						if not allowed:
							self.assertEqual(current["phase"], "summary")

	def test_no_call_budget_for_retry_records_absence_and_preserves_summary(self):
		state = self.analyze()
		with self.engine.store.locked(state["id"]) as record:
			state["calls"] *= meeting.MAX_CALLS - 2
			state.update(status="running", phase="round1")
			state["rounds"]["1"] = [{"role": "qa", "method": "작은 범위부터 점검한다",
				"risks": [], "changes": [], "questions": []}]
			session = meeting.Session(record, state, meeting.Rules())
			with mock.patch("orchestrator.providers.call", side_effect=ProviderError("exit", "합성 실패")) as call:
				self.assertIsNone(session.ask("round1", "backend"))
				self.assertEqual(call.call_count, 1)
			self.assertEqual(state["absent"][0]["detail"], "exit")
			self.assertFalse(any(c.get("retry") for c in state["calls"]))
			session.summarize()
		self.assertIsNotNone(state["summary"])
		self.assertEqual(len(state["calls"]), 14)
		self.assertEqual(state["calls"][-1]["phase"], "summary")

	def test_retry_time_counts_and_summary_time_is_reserved(self):
		state = self.analyze()
		clock = [0.0]
		def timeout(*args, **kwargs):
			clock[0] += 2
			raise ProviderError("timeout", "합성 실패")
		with mock.patch("orchestrator.meeting.time.monotonic", side_effect=lambda: clock[0]), \
				self.engine.store.locked(state["id"]) as record:
			state["active_seconds"] = meeting.MAX_SECONDS - meeting.SUMMARY_SECONDS - 31
			state.update(status="running", phase="round1")
			state["rounds"]["1"] = [{"role": "qa", "method": "작은 범위부터 점검한다",
				"risks": [], "changes": [], "questions": []}]
			session = meeting.Session(record, state, meeting.Rules())
			with mock.patch("orchestrator.providers.call", side_effect=timeout) as call:
				self.assertIsNone(session.ask("round1", "backend"))
				self.assertEqual(call.call_count, 1)
			self.assertEqual(state["active_seconds"], meeting.MAX_SECONDS - meeting.SUMMARY_SECONDS - 29)
			self.assertFalse(any(c.get("retry") for c in state["calls"]))
			session.summarize()
		self.assertIsNotNone(state["summary"])
		self.assertLessEqual(self.ai.calls[-1][1]["timeout"], meeting.SUMMARY_SECONDS + 29)

	def test_successful_retry_also_counts_its_active_time(self):
		clock = [0.0]
		original = self.ai.__call__
		def once(plan, **kwargs):
			clock[0] += 2
			reply = original(plan, **kwargs)
			if len(self.ai.calls) == 2:
				raise ProviderError("empty", "합성 실패")
			return reply
		with mock.patch("orchestrator.meeting.time.monotonic", side_effect=lambda: clock[0]), \
				mock.patch("orchestrator.providers.call", side_effect=once):
			state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		self.assertEqual(len(state["calls"]), 7)
		self.assertEqual(state["active_seconds"], 14)
		self.assertEqual(self.ai.calls[2][1]["timeout"], self.ai.calls[1][1]["timeout"] - 2)

	def test_retry_checkpoint_loses_time_budget_but_keeps_the_absence_reason(self):
		state = self.analyze()
		clock = [0.0]
		with mock.patch("orchestrator.meeting.time.monotonic", side_effect=lambda: clock[0]), \
				self.engine.store.locked(state["id"]) as record:
			state.update(status="running", active_seconds=meeting.MAX_SECONDS - meeting.SUMMARY_SECONDS - 31)
			original = record.save
			def checkpoint(current):
				original(current)
				if current["calls"][-1].get("retry") and current["calls"][-1]["ok"] is None:
					clock[0] += 2
			with mock.patch.object(record, "save", side_effect=checkpoint), \
					mock.patch("orchestrator.providers.call", side_effect=ProviderError("exit", "합성 실패")) as call:
				session = meeting.Session(record, state, meeting.Rules())
				self.assertIsNone(session.ask("round1", "backend"))
			self.assertEqual(call.call_count, 1)
		self.assertEqual(state["absent"], [{"role": "backend", "phase": "round1",
			"reason": "provider", "detail": "exit"}])
		self.assertEqual(state["calls"][-1]["error"], "budget_before_call")
