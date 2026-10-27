"""실패·중단된 회의를 사용자 명령으로 다시 열 때 받은 자문을 다시 쓰는지 검사한다."""

import copy
import unittest
from unittest import mock

from agency import meeting, project
from orchestrator import providers

import test_agency_meeting


class MeetingRetryTest(unittest.TestCase):
	# 준비 코드만 빌린다. 시험 클래스를 모듈 이름으로 두면 기존 회의 시험이 한 번 더 돈다.
	setUp = test_agency_meeting.MeetingTest.setUp
	new = test_agency_meeting.MeetingTest.new
	call = test_agency_meeting.MeetingTest.call

	def solo(self, plan, **kwargs):
		# 다시 여는 회차에는 자문이 한 명뿐일 수 있어 동시 출발 장벽을 쓰지 않는다.
		self.calls.append((plan, kwargs))
		if "recommendation" in kwargs["schema"]["properties"]:
			return providers.Reply(copy.deepcopy(self.reply), .25, {})
		return providers.Reply(copy.deepcopy(self.decision), .5, {})

	def test_failed_advice_reopens_and_recalls_only_the_failed_consultant(self):
		identity = self.new()
		def partial(plan, **kwargs):
			self.barrier.wait(timeout=3)
			if plan.id == "one":
				raise providers.ProviderError("exit", "합성 오류")
			return self.solo(plan, **kwargs)
		self.provider.side_effect = partial
		with self.assertRaisesRegex(ValueError, "실패"):
			meeting.ensure(identity, self.department)
		self.assertEqual(meeting.retry(identity)["meeting"]["status"], "retry_advice")
		self.calls.clear()
		self.provider.side_effect = self.solo
		meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual([plan.id for plan, _ in self.calls], ["one", "pm"])
		self.assertEqual(row["used"]["calls"], 4)
		self.assertEqual(len(row["decisions"]), 1)

	def test_quota_failure_reopens_with_exhausted_plans_cleared(self):
		identity = self.new()
		self.provider.side_effect = providers.ProviderError("exit", "합성 종료 (한도 소진)")
		with self.assertRaisesRegex(ValueError, "소진"):
			meeting.ensure(identity, self.department)
		self.assertEqual(sorted(project.load(identity)["meeting"]["exhausted"]), ["one", "two"])
		self.assertEqual(meeting.retry(identity)["meeting"]["exhausted"], [])
		self.provider.side_effect = self.call
		meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual(row["used"]["calls"], 5)

	def test_failed_pm_reopens_without_repeating_advice_and_other_states_are_refused(self):
		identity = self.new()
		with self.assertRaisesRegex(ValueError, "실패하거나"):
			meeting.retry(identity)
		self.decision["verification"] = ""
		with self.assertRaises(ValueError):
			meeting.ensure(identity, self.department)
		self.assertEqual(meeting.retry(identity)["meeting"]["status"], "advised")
		self.decision["verification"] = "합성 시험을 실행한다"
		self.calls.clear()
		self.provider.side_effect = self.solo
		meeting.ensure(identity, self.department)
		self.assertEqual([plan.id for plan, _ in self.calls], ["pm"])
		self.assertEqual(project.load(identity)["checkpoint"]["step"], "meeting_done")
		with self.assertRaisesRegex(ValueError, "실패하거나"):
			meeting.retry(identity)

	def test_interrupted_call_reopens_with_reservation_still_charged(self):
		identity = self.new()
		with mock.patch.object(meeting, "_parallel", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, self.department)
		with self.assertRaisesRegex(ValueError, "중단"):
			meeting.ensure(identity, self.department)
		charged = project.load(identity)["used"]
		state = meeting.retry(identity)["meeting"]
		self.assertEqual(state["status"], "retry_advice")
		self.assertFalse({"pending", "reserved_seconds", "error"} & set(state))
		self.assertEqual(project.load(identity)["used"], charged)
		meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual(row["used"]["calls"], charged["calls"] + 3)


if __name__ == "__main__":
	unittest.main()
