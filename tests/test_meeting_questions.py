"""질문 카드의 공통 계약과 회의의 답변·확인 경계."""

import copy
from concurrent.futures import ThreadPoolExecutor

from orchestrator import question_cards as cards
from vaultfilter.public import Rules
from test_meeting import MeetingCase, question


def answer(identity="scope", kind="choice", value="a"):
	return {"question_id": identity, "kind": kind, "value": value}


class MeetingQuestionsTest(MeetingCase):
	def pending(self):
		self.ai.analysis_questions = [question()]
		return self.analyze()

	def test_confirmation_source_is_validated_before_changing_a_card(self):
		state = self.pending()
		card = state["cards"][-1]
		state = self.engine.answer(state["id"], card["id"], [answer()], source="telegram")
		with self.assertRaises(ValueError):
			self.engine.resume(state["id"], confirm_card=card["id"], source="unknown")
		self.assertEqual(self.engine.store.read(state["id"]), state)
		state = self.engine.resume(state["id"], confirm_card=card["id"], source="telegram")
		self.assertEqual(state["cards"][-1]["confirmed_source"], "telegram")
		self.assertEqual(state["status"], "participants_pending")

	def test_common_card_choice_direct_delegate_and_summary_confirmation(self):
		for reply in (answer(), answer(kind="text", value="검색 화면부터 진행"),
				answer(kind="delegate", value="범위 선택을 명시적으로 위임"), answer(kind="delegate", value="알아서")):
			state = self.pending()
			card = state["cards"][-1]
			state = self.engine.answer(state["id"], card["id"], [reply], source="cli")
			self.assertEqual(state["status"], "answers_pending")
			self.assertEqual(state["cards"][-1]["answers"][0]["delegated"], reply["kind"] == "delegate")
			if reply["value"] == "알아서":
				self.assertIn("명시 위임: 알아서", state["cards"][-1]["summary"])
				self.assertEqual(state["cards"][-1]["answers"][0]["source"], "cli")
			self.assertEqual(len(state["calls"]), 1)
			state = self.engine.resume(state["id"])
			self.assertEqual(state["status"], "answers_pending")
			state = self.engine.resume(state["id"], confirm_card=card["id"])
			self.assertEqual(state["status"], "participants_pending")
			self.assertFalse(state["confirmed"])
			state = self.start(state)
			self.assertEqual(state["status"], "completed")
			self.assertEqual(state["cards"][-1]["status"], "confirmed")

	def test_alaseo_requires_explicit_delegate_and_permission_without_implicit_consent(self):
		rules = Rules()
		for allowed in (True, False):
			q = {**question(), "allow_delegate": allowed}
			for kind in ("text", "delegate"):
				for value in ("", " ", "네", "예", "동의", "ok", "yes", "알아서"):
					with self.subTest(allowed=allowed, kind=kind, value=value):
						card = cards.create([q], 1, "analysis", rules)
						before = copy.deepcopy(card)
						if allowed and kind == "delegate" and value == "알아서":
							cards.accept(card, card["id"], [answer(kind=kind, value=value)], "cli", rules)
							self.assertEqual(card["status"], "answered")
							self.assertTrue(card["answers"][0]["delegated"])
						else:
							with self.assertRaises(ValueError):
								cards.accept(card, card["id"], [answer(kind=kind, value=value)], "cli", rules)
							self.assertEqual(card, before)

	def test_missing_unsafe_acknowledgment_and_wrong_card_never_progress(self):
		state = self.pending()
		card_id = state["cards"][-1]["id"]
		for replies in ([], [answer(kind="text", value="네")], [answer(value="missing")],
				[answer(kind="text", value="synthetic-corp 정보")], [answer(), answer()],
				[answer(kind="text", value="~" + "/Obsidian/notes")],
				[answer(kind="delegate", value="~" + "/workspace/project")],
				[answer(kind="text", value="password=" + "s" * 24)],
				[answer(kind="text", value="user" + "@" + "example.invalid")]):
			with self.assertRaises(ValueError):
				self.engine.answer(state["id"], card_id, replies, source="cli")
		with self.assertRaises(ValueError):
			self.engine.answer(state["id"], "wrong", [answer()], source="cli")
		self.assertEqual(self.engine.resume(state["id"])["status"], "questions_pending")
		self.assertEqual(len(self.ai.calls), 1)

	def test_first_valid_concurrent_answer_wins_and_late_retries_cannot_overwrite(self):
		state = self.pending()
		card_id = state["cards"][-1]["id"]
		def send(value):
			try:
				return self.engine.answer(state["id"], card_id, [answer(value=value)], source="cli")
			except (ValueError, RuntimeError):
				return None
		with ThreadPoolExecutor(2) as executor:
			list(executor.map(send, ("a", "b")))
		winner = self.engine.status(state["id"])["cards"][-1]["answers"]
		with self.assertRaises(ValueError):
			self.engine.answer(state["id"], card_id, [answer(value="b")], source="cli")
		self.engine.resume(state["id"], confirm_card=card_id)
		with self.assertRaises(ValueError):
			self.engine.answer(state["id"], card_id, [answer()], source="cli")
		self.assertEqual(self.engine.status(state["id"])["cards"][-1]["answers"], winner)

	def test_round_one_questions_pause_before_round_two(self):
		self.ai.round_questions = {"qa": [question()]}
		state = self.start(self.analyze())
		self.assertEqual(state["status"], "questions_pending")
		self.assertEqual(len(state["rounds"]["2"]), 0)
		card = state["cards"][-1]
		self.assertEqual(card["source"], "round1")
		state = self.engine.answer(state["id"], card["id"], [answer()], source="cli")
		self.assertIn("작게", state["cards"][-1]["summary"])
		state = self.engine.resume(state["id"], confirm_card=card["id"])
		self.assertEqual(state["status"], "completed")

	def test_common_contract_one_to_three_questions_and_five_turns_without_consent(self):
		rules = Rules()
		for count in (0, 4):
			with self.assertRaises(ValueError):
				cards.create([question(str(i)) for i in range(count)], 1, "analysis", rules)
		for turn in range(1, 6):
			card = cards.create([question()], turn, "analysis", rules)
			self.assertEqual(card["status"], "waiting")
		with self.assertRaises(ValueError):
			cards.create([question()], 6, "analysis", rules)
		bad = question()
		bad["allow_delegate"] = False
		card = cards.create([bad], 1, "analysis", rules)
		with self.assertRaises(ValueError):
			cards.accept(card, card["id"], [answer(kind="delegate", value="위임합니다")], "cli", rules)
		self.assertEqual(card["status"], "waiting")

	def test_recommended_written_as_label_is_mapped_to_the_option_id_only_when_unique(self):
		rules = Rules()
		for written in ("작게", " 작게 ", "A"):
			card = cards.create([{**question(), "recommended": written}], 1, "analysis", rules)
			self.assertEqual(card["questions"][0]["recommended"], "a", written)
		prefixed = question()
		prefixed["options"][0]["reply"], prefixed["options"][1]["reply"] = "A안: 작게 진행", "B안: 넓게 진행"
		for written, expected in (("A안", "a"), ("B안", "b")):
			card = cards.create([{**prefixed, "recommended": written}], 1, "analysis", rules)
			self.assertEqual(card["questions"][0]["recommended"], expected)
		ambiguous = question()
		for option in ambiguous["options"]:
			option["reply"] = "안: 같은 시작"
		with self.assertRaises(ValueError):  # 회신 문구 첫머리가 두 선택지에 걸리면 고르지 않는다
			cards.create([{**ambiguous, "recommended": "안"}], 1, "analysis", rules)
		same = question()
		for option in same["options"]:
			option["label"] = "같은 라벨"
		for written in ("같은 라벨", "없는 값", ""):
			with self.assertRaises(ValueError):
				cards.create([{**same, "recommended": written}], 1, "analysis", rules)
		with self.assertRaises(ValueError):
			cards.create([{**question(), "recommended": ["a"]}], 1, "analysis", rules)

	def test_invalid_option_fields_and_incomplete_bundle_are_atomic(self):
		rules = Rules()
		for change in ({"reason": ""}, {"recommended": "absent"}, {"allow_delegate": "yes"}):
			with self.assertRaises(ValueError):
				cards.create([{**question(), **change}], 1, "analysis", rules)
		card = cards.create([question("scope"), question("time")], 1, "analysis", rules)
		before = copy.deepcopy(card)
		with self.assertRaises(ValueError):
			cards.accept(card, card["id"], [answer()], "cli", rules)
		self.assertEqual(card, before)

	def test_five_question_turns_require_five_explicit_answers_and_confirmations(self):
		self.ai.roles = ("plan", "backend", "frontend", "qa")
		self.ai.analysis_questions = [question("scope" + str(i)) for i in range(3)]
		self.ai.round_questions = {role: [question("scope" + str(i)) for i in range(3)] for role in self.ai.roles}
		state = self.analyze()
		for turn in range(1, 6):
			self.assertEqual(state["status"], "questions_pending")
			card = state["cards"][-1]
			self.assertEqual(card["turn"], turn)
			state = self.engine.answer(state["id"], card["id"], [answer(q["id"]) for q in card["questions"]], source="cli")
			self.assertEqual(self.engine.resume(state["id"])["status"], "answers_pending")
			state = self.engine.resume(state["id"], confirm_card=card["id"])
			if turn == 1:
				state = self.start(state)
		self.assertEqual(state["status"], "completed")
		self.assertEqual(len(state["cards"]), 5)
		self.assertEqual(len(state["calls"]), 10)

	def test_sixth_turn_stops_with_open_questions_without_automatic_consent(self):
		state = self.pending()
		card = state["cards"][-1]
		self.engine.answer(state["id"], card["id"], [answer()], source="cli")
		with self.engine.store.locked(state["id"]) as record:
			state = record.load()
			state["cards"].extend(copy.deepcopy(state["cards"]) * 4)
			state["question_queue"] = [{"question": question("extra"), "ref": "analysis"}]
			record.save(state)
		state = self.engine.resume(state["id"], confirm_card=card["id"])
		self.assertEqual(state["status"], "questions_exhausted")
		self.assertTrue(state["open_questions"])
		self.assertFalse(state["confirmed"])
		self.assertEqual(len(self.ai.calls), 1)

	def test_duplicate_question_ids_across_roles_keep_unique_card_ids_and_origins(self):
		self.ai.round_questions = {"backend": [question("scope-2"), question("scope")], "qa": [question("scope")]}
		state = self.start(self.analyze())
		self.assertEqual(state["status"], "questions_pending")
		card = state["cards"][-1]
		self.assertEqual(len({q["id"] for q in card["questions"]}), 3)
		self.assertEqual(set(card["origins"].values()), {"r1:backend", "r1:qa"})
