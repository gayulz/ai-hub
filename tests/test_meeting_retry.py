"""재시도·불참 격리·거부 이유의 마스킹은 합성 호출로만 확인한다."""

import copy
import json
from unittest import mock

from orchestrator import meeting, meeting_prompts as prompts
from orchestrator.providers import ProviderError
from vaultfilter.public import Rules
from test_meeting import MeetingCase


class MeetingRetryTest(MeetingCase):
	def test_validation_retry_succeeds_with_reason_and_same_readonly_plan(self):
		original = self.ai.__call__
		def once(plan, **kwargs):
			reply = original(plan, **kwargs)
			if len(self.ai.calls) == 2:
				reply.data["method"] = ""
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=once):
			state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		self.assertEqual(len(state["calls"]), 7)
		first, retry = state["calls"][1:3]
		self.assertFalse(first["ok"])
		self.assertEqual(first["error"], "ValueError")
		self.assertIn("문자열 길이·형식", first["detail"])
		self.assertTrue(retry["ok"])
		self.assertTrue(retry["retry"])
		self.assertEqual(first["plan"], retry["plan"])
		self.assertEqual(first["model"], retry["model"])
		self.assertEqual(self.ai.calls[1][0], self.ai.calls[2][0])
		self.assertEqual(self.ai.calls[1][1]["schema"], self.ai.calls[2][1]["schema"])
		self.assertEqual(self.ai.calls[2][1]["prompt"], self.ai.calls[1][1]["prompt"]
			+ "\n직전 응답이 검사에서 거부됐다: " + first["detail"] + ". 같은 JSON 계약을 지켜 다시 답한다")
		self.assertTrue(all(call["writer"] is False for _, call in self.ai.calls))
		self.assertEqual(state["absent"], [])
		minutes = (self.hub / "meetings" / state["id"] / "minutes.md").read_text()
		self.assertIn("재시도: 1회", minutes)

	def test_rejection_detail_is_one_line_masked_and_at_most_160_characters(self):
		path = "/" + "Users/acme/private-notes"
		email = "xyz" + "@" + "example.invalid"
		secret = "ghp_" + "s" * 36
		detail = "응답 거부: " + path + "\n" + email + " " + secret + " " + "이유" * 100
		original = prompts.validate_reply
		for phase in ("analysis", "round1", "summary"):
			with self.subTest(phase=phase):
				def refused(value, current, *args):
					if current == phase:
						raise ValueError(detail)
					return original(value, current, *args)
				with mock.patch.object(prompts, "validate_reply", side_effect=refused):
					state = self.analyze()
					if phase != "analysis":
						state = self.start(state)
				failed = [c for c in state["calls"] if c["phase"] == phase and not c["ok"]]
				self.assertTrue(failed)
				for call in failed:
					self.assertEqual(len(call["detail"]), 160)
					self.assertNotIn("\n", call["detail"])
					self.assertIn("[보안값]", call["detail"])
				folder = self.hub / "meetings" / state["id"]
				persisted = (folder / "minutes.md").read_text() + (folder / "meeting.json").read_text()
				persisted += "\n".join(call["prompt"] for _, call in self.ai.calls)
				for value in (path, email, secret, "private-notes"):
					self.assertNotIn(value, persisted)

	def test_failed_participant_is_absent_but_other_speakers_and_summary_continue(self):
		self.ai.roles = ("backend", "frontend", "qa")
		original = self.ai.__call__
		for phase, field in (("round1", "method"), ("round2", "agree")):
			with self.subTest(phase=phase):
				def refused(plan, **kwargs):
					reply = original(plan, **kwargs)
					if field in kwargs["schema"]["properties"] and reply.data["role"] == "backend":
						if phase == "round1":
							reply.data["method"] = ""
						else:
							reply.data["agree"] = []
					return reply
				with mock.patch("orchestrator.providers.call", side_effect=refused):
					state = self.start(self.analyze())
				self.assertEqual(state["status"], "completed")
				self.assertEqual([p["role"] for p in state["participants"]], list(self.ai.roles))
				self.assertEqual(len(state["absent"]), 1)
				absent = state["absent"][0]
				self.assertEqual({k: absent[k] for k in ("role", "phase", "reason")},
					{"role": "backend", "phase": phase, "reason": "validation"})
				self.assertTrue(absent["detail"])
				self.assertEqual([row["role"] for row in state["rounds"][phase[-1]]], ["frontend", "qa"])
				self.assertIsNotNone(state["summary"])
				self.assertEqual(state["calls"][-1]["phase"], "summary")
				self.assertNotIn("r1:backend", prompts.sources(state))
				self.assertNotIn("r1:backend", json.dumps(state["summary"]))
				if phase == "round1":
					self.assertNotIn("backend", [r["role"] for r in state["rounds"]["2"]])
				minutes = (self.hub / "meetings" / state["id"] / "minutes.md").read_text()
				self.assertIn("불참: backend, " + phase + ", validation: " + absent["detail"], minutes)
				count = len(self.ai.calls)
				self.assertEqual(self.engine.resume(state["id"])["status"], "completed")
				self.assertEqual(len(self.ai.calls), count)

	def test_a_single_remaining_speaker_summarizes_without_fabricated_reactions(self):
		original = self.ai.__call__
		def refused(plan, **kwargs):
			reply = original(plan, **kwargs)
			if "method" in kwargs["schema"]["properties"] and reply.data["role"] == "backend":
				reply.data["method"] = ""
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=refused):
			state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		self.assertEqual([row["role"] for row in state["rounds"]["1"]], ["qa"])
		self.assertEqual(state["rounds"]["2"], [])
		self.assertEqual(state["summary"]["conclusion"][0]["refs"], ["r1:qa"])

	def test_all_absent_in_either_round_fails_with_last_reason_and_no_summary(self):
		original = self.ai.__call__
		for phase, field in (("round1", "method"), ("round2", "agree")):
			with self.subTest(phase=phase):
				def refused(plan, **kwargs):
					reply = original(plan, **kwargs)
					if field in kwargs["schema"]["properties"]:
						reply.data[field] = "" if phase == "round1" else []
					return reply
				with mock.patch("orchestrator.providers.call", side_effect=refused):
					state = self.start(self.analyze())
				self.assertEqual(state["status"], "failed")
				self.assertEqual(state["phase"], phase)
				self.assertIn("모든 참석자 불참", state["failure"])
				self.assertIn(state["absent"][-1]["detail"], state["failure"])
				self.assertEqual(len(state["absent"]), 2)
				self.assertIsNone(state["summary"])
				self.assertFalse(any(c["phase"] == "summary" for c in state["calls"]))

	def test_each_provider_error_retries_once_without_appending_exception_text(self):
		original = self.ai.__call__
		for kind in ("timeout", "exit", "empty", "json", "missing"):
			with self.subTest(kind=kind):
				state = self.analyze()
				start = len(self.ai.calls)
				def once(plan, **kwargs):
					reply = original(plan, **kwargs)
					if len(self.ai.calls) == start + 1:
						raise ProviderError(kind, "외부 원문 기록 금지")
					return reply
				with mock.patch("orchestrator.providers.call", side_effect=once):
					state = self.start(state)
				self.assertEqual(state["status"], "completed")
				self.assertEqual(len(state["calls"]), 7)
				self.assertEqual(state["calls"][1]["error"], kind)
				self.assertTrue(state["calls"][2]["retry"])
				first, retry = self.ai.calls[start:start + 2]
				self.assertEqual(first[0], retry[0])
				self.assertEqual({k: v for k, v in first[1].items() if k != "timeout"},
					{k: v for k, v in retry[1].items() if k != "timeout"})
				self.assertLessEqual(retry[1]["timeout"], first[1]["timeout"])
				self.assertNotIn("외부 원문", json.dumps(state, ensure_ascii=False))

	def test_provider_retry_failure_only_marks_that_participant_absent(self):
		original = self.ai.__call__
		def failed(plan, **kwargs):
			reply = original(plan, **kwargs)
			if "method" in kwargs["schema"]["properties"] and reply.data["role"] == "backend":
				raise ProviderError("timeout", "외부 원문 기록 금지")
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=failed):
			state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		self.assertEqual(state["absent"], [{"role": "backend", "phase": "round1",
			"reason": "provider", "detail": "timeout"}])
		self.assertEqual(len([c for c in state["calls"] if c["role"] == "backend"]), 2)
		self.assertNotIn("외부 원문", json.dumps(state, ensure_ascii=False))

	def test_bad_unicode_reply_records_only_the_engine_reason(self):
		original = self.ai.__call__
		raw = "기록하면 안 되는 합성 원문 " + chr(0xD800)
		def invalid(plan, **kwargs):
			reply = original(plan, **kwargs)
			if "method" in kwargs["schema"]["properties"] and reply.data["role"] == "backend":
				reply.data["method"] = raw
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=invalid):
			state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		self.assertEqual(state["absent"][0]["reason"], "validation")
		self.assertEqual(state["absent"][0]["detail"], "AI 응답 문자열 인코딩이 잘못됐습니다")
		folder = self.hub / "meetings" / state["id"]
		text = (folder / "meeting.json").read_text() + (folder / "minutes.md").read_text()
		text += "\n".join(call["prompt"] for _, call in self.ai.calls)
		self.assertNotIn("기록하면 안 되는 합성 원문", text)
		self.assertNotIn("can't encode", text)
		self.assertNotIn("\\ud800", text)

	def test_analysis_and_summary_retry_failure_are_fatal_with_specific_reason(self):
		original = self.ai.__call__
		for phase, marker in (("analysis", "participants"), ("summary", "conclusion")):
			for reason in ("validation", "provider"):
				with self.subTest(phase=phase, reason=reason):
					def failed(plan, **kwargs):
						reply = original(plan, **kwargs)
						if marker in kwargs["schema"]["properties"]:
							if reason == "provider":
								raise ProviderError("json", "외부 원문 기록 금지")
							reply.data[marker] = []
						return reply
					with mock.patch("orchestrator.providers.call", side_effect=failed):
						state = self.analyze()
						if phase == "summary":
							state = self.start(state)
					self.assertEqual(state["status"], "failed")
					self.assertEqual(state["phase"], phase)
					self.assertEqual(state["absent"], [])
					calls = [c for c in state["calls"] if c["phase"] == phase]
					self.assertEqual(len(calls), 2)
					self.assertTrue(calls[-1]["retry"])
					self.assertTrue(state["failure"].startswith(phase + ": "))
					self.assertIn("응답 배열" if reason == "validation" else "json", state["failure"])
					self.assertNotIn("외부 원문", json.dumps(state, ensure_ascii=False))

	def test_round_two_instruction_matches_minimum_reaction_and_duplicate_checks(self):
		state = self.analyze()
		text = prompts.prompt(state, "round2", "backend", {}, self.ai.roles)
		for instruction in ("최소 한 가지", "같은 참석자 의견에는 한 번만", "나머지 항목은 빈 배열"):
			self.assertIn(instruction, text)
		value = {"role": "backend", "agree": [], "disagree": [], "supplement": []}
		with self.assertRaisesRegex(ValueError, "반응이 필요"):
			prompts.validate_reply(value, "round2", self.ai.roles, "backend", state, Rules())
		value["agree"] = [{"role": "qa", "text": "점검한다"}]
		self.assertEqual(prompts.validate_reply(value, "round2", self.ai.roles, "backend", state, Rules()), value)
		duplicate = copy.deepcopy(value)
		duplicate["agree"] *= 2
		with self.assertRaisesRegex(ValueError, "한 번만"):
			prompts.validate_reply(duplicate, "round2", self.ai.roles, "backend", state, Rules())
