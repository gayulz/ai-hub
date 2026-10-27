"""합성 자료와 가짜 공급자로 회의 발췌 계약을 검사한다."""

import copy
import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from agency import meeting, project
from orchestrator import providers

import test_agency_meeting


class GroundingTest(unittest.TestCase):
	def setUp(self):
		self.rules = mock.Mock()
		self.rules.kinds.return_value = []
		self.data = {"agenda": "공통 문구", "spec": {
			"goal": "명세 속 공통 문구와 합성 기능을 검사한다",
			"target": "합성 결과", "out_of_scope": "외부 실행",
			"done_criteria": ["합성 시험 통과"], "budget": {"calls": 6, "minutes": 5}}}
		self.members = [("backend", SimpleNamespace(id="one")), ("qa", SimpleNamespace(id="two"))]
		self.network = mock.patch("socket.socket.connect", side_effect=AssertionError("네트워크 금지"))
		self.network.start()
		self.addCleanup(self.network.stop)

	def parallel(self, data, reply):
		calls, barrier = [], threading.Barrier(2)
		def fake(plan, **kwargs):
			calls.append((plan.id, kwargs))
			barrier.wait(timeout=3)
			return providers.Reply(reply(plan, kwargs), .1, {})
		with mock.patch.object(providers, "call", side_effect=fake):
			results, _ = meeting._parallel(self.members, data, self.rules, 30)
		return results, calls

	def advice(self, source, text):
		return {"recommendation": "작게 진행", "reason": "검사가 쉽다", "counterexample": "누락 가능",
			"facts": [{"source": source, "text": text}]}

	def test_parallel_uses_identical_source_specific_schema_and_choices(self):
		def reply(plan, kwargs):
			payload = json.loads(kwargs["prompt"].rsplit("\n", 1)[1])
			source = "agenda" if plan.id == "one" else "spec"
			return self.advice(source, payload["발췌 선택지"][source][0])
		results, calls = self.parallel(self.data, reply)
		self.assertTrue(all(results))
		self.assertEqual(len(calls), 2)
		first, second = (call[1] for call in calls)
		self.assertEqual(first["prompt"], second["prompt"])
		self.assertEqual(first["schema"], second["schema"])
		self.assertNotEqual(first["cwd"], second["cwd"])
		payload = json.loads(first["prompt"].rsplit("\n", 1)[1])
		self.assertEqual(payload["자료"], self.data)
		choices = payload["발췌 선택지"]
		self.assertEqual(choices["agenda"], [self.data["agenda"]])
		self.assertEqual(choices["spec"], [self.data["spec"][key]
			for key in ("goal", "target", "out_of_scope")] + self.data["spec"]["done_criteria"])
		branches = first["schema"]["properties"]["facts"]["items"]["anyOf"]
		for branch in branches:
			fields = branch["properties"]
			source = fields["source"]["enum"][0]
			self.assertEqual(fields["text"]["enum"], choices[source])
		self.assertEqual({result["facts"][0]["source"] for result in results}, {"agenda", "spec"})

	def test_wrong_source_fabrication_and_unlisted_substring_are_rejected(self):
		cases = [("unknown", "공통 문구"), ("agenda", "근거 없는 사실"),
			("agenda", "명세 속 공통 문구와 합성 기능을 검사한다"), ("spec", "공통 문구"),
			("spec", "합성 기능")]
		for source, text in cases:
			with self.subTest(source=source, text=text):
				results, calls = self.parallel(self.data, lambda plan, kwargs: self.advice(source, text))
				self.assertEqual(results, [None, None])
				self.assertEqual(len(calls), 2)

	def test_long_and_blank_chunks_are_exact_nonempty_safe_excerpts(self):
		data = copy.deepcopy(self.data)
		data["spec"]["goal"] = " " * 500 + "긴문장" * 166 + "끝끝"
		data["spec"]["target"] = "첫 문장 " * 125 + " 마지막"
		def reply(plan, kwargs):
			payload = json.loads(kwargs["prompt"].rsplit("\n", 1)[1])
			return self.advice("spec", payload["발췌 선택지"]["spec"][0])
		results, calls = self.parallel(data, reply)
		self.assertTrue(all(results))
		payload = json.loads(calls[0][1]["prompt"].rsplit("\n", 1)[1])
		choices = payload["발췌 선택지"]
		sources = {"agenda": [data["agenda"]], "spec": [data["spec"][key]
			for key in ("goal", "target", "out_of_scope")] + data["spec"]["done_criteria"]}
		for source, excerpts in choices.items():
			self.assertTrue(excerpts)
			for excerpt in excerpts:
				self.assertTrue(excerpt.strip())
				self.assertLessEqual(len(excerpt), 500)
				self.assertTrue(any(excerpt in original for original in sources[source]))
		self.assertTrue(any(excerpt in data["spec"]["goal"] for excerpt in choices["spec"]))
		self.assertTrue(any(excerpt in data["spec"]["target"] for excerpt in choices["spec"]))

	def test_empty_input_is_rejected_before_provider_call(self):
		for field in ("agenda", "goal"):
			for empty in ("", "   "):
				with self.subTest(field=field, empty=empty):
					data = copy.deepcopy(self.data)
					if field == "agenda":
						data["agenda"] = empty
					else:
						data["spec"]["goal"] = empty
					fingerprint = hashlib.sha256(json.dumps(data["spec"], ensure_ascii=False,
						sort_keys=True).encode()).hexdigest()
					row = {"checkpoint": {"detail": data["agenda"]}, "spec": data["spec"], "spec_hash": fingerprint}
					with mock.patch.object(providers, "call") as provider, self.assertRaises(ValueError):
						meeting._inputs(row, self.rules)
					provider.assert_not_called()

	def test_unsafe_split_is_skipped_and_safe_untrimmed_text_is_preserved(self):
		data = copy.deepcopy(self.data)
		data["agenda"] = " 앞뒤 합성 "
		unsafe = "가" * 500
		data["spec"]["goal"] = unsafe + "안전한 나머지"
		self.rules.kinds.side_effect = lambda value: ["거부"] if value in ("앞뒤 합성", unsafe) else []
		choices = meeting._excerpts(data, self.rules)
		self.assertEqual(choices["agenda"], [data["agenda"]])
		self.assertNotIn(unsafe, choices["spec"])
		self.assertIn("안전한 나머지", choices["spec"])

	def test_existing_valid_saved_substring_remains_resumable(self):
		saved = self.advice("spec", "합성 기능")
		self.assertEqual(meeting._advice(saved, self.data, self.rules), saved)

	def test_unlisted_fact_blocks_pm_and_does_not_repeat_on_restart(self):
		fixture = test_agency_meeting.MeetingTest()
		fixture.setUp()
		self.addCleanup(fixture.doCleanups)
		identity = fixture.new()
		fixture.reply["facts"][0]["text"] = "합성"
		with self.assertRaisesRegex(ValueError, "자문.*실패"):
			meeting.ensure(identity, fixture.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "failed")
		self.assertEqual(row["checkpoint"]["step"], "meeting")
		self.assertEqual(row["decisions"], [])
		self.assertEqual(fixture.provider.call_count, 2)
		with self.assertRaisesRegex(ValueError, "실패"):
			meeting.ensure(identity, fixture.department)
		self.assertEqual(fixture.provider.call_count, 2)

	def test_advised_legacy_substrings_resume_without_consulting_again(self):
		fixture = test_agency_meeting.MeetingTest()
		fixture.setUp()
		self.addCleanup(fixture.doCleanups)
		identity = fixture.new()
		with mock.patch.object(meeting, "_decide", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, fixture.department)
		with project._edit(identity, "synthetic_legacy_advice") as row:
			state = row["meeting"]
			for opinion in state["opinions"] + list(state["responses"].values()):
				opinion["facts"][0]["text"] = "합성"
		meeting.ensure(identity, fixture.department)
		self.assertEqual(project.load(identity)["meeting"]["status"], "completed")
		self.assertEqual(fixture.provider.call_count, 3)


if __name__ == "__main__":
	unittest.main()
