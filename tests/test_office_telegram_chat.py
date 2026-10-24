"""합성 홈·가짜 전송·가짜 엔진으로 PM 카드만 연결되는 경계를 확인한다."""

import copy
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from office import telegram_bridge, telegram_chat
from office.telegram_state import Store, checked, empty
from orchestrator import chat_record, question_cards
from test_meeting import question
from test_office_telegram_bridge import BridgeCase
from test_office_telegram_state import local_ask, local_card
from vaultfilter.public import Rules


class FakeChatEngine:
	def __init__(self):
		self.store, self.calls = chat_record.Store(), []

	def answer(self, chat_id, card_id, answers, *, source):
		self.calls.append((chat_id, card_id, copy.deepcopy(answers), source))
		with self.store.locked(chat_id) as record:
			state = record.load()
			if state["status"] != "idle":
				raise ValueError("현재 답할 수 없는 대화입니다")
			question_cards.accept(state["cards"][-1], card_id, answers, source, Rules())
			record.save(state)
			return state


class ChatCase(BridgeCase):
	def setUp(self):
		super().setUp()
		self.chat_engine = FakeChatEngine()
		self.bridge.chats.engine = self.chat_engine
		patch = mock.patch("orchestrator.providers.chat_call", side_effect=AssertionError("실제 AI 호출 금지"))
		self.chat_call = patch.start()
		self.addCleanup(patch.stop)
		self.private_text = "PM 답 전문과 이전 턴의 비공개 합성 내용"

	def pending_chat(self, count=1, delegate=True):
		state = chat_record.new("demo-repo", "p01", "codex")
		questions = [{**question("q" + str(i)), "allow_delegate": delegate} for i in range(1, count + 1)]
		card = question_cards.create(questions, 1, "pm", Rules())
		state.update(cards=[card], dev_draft=self.private_text, needs_user="사무실에서만 확인할 합성 내용",
			turns=[{"at": chat_record.now(), "role": "assistant", "text": self.private_text,
				"source": "office", "kind": "message"}])
		self.save_chat(state, create=True)
		return state, card

	def save_chat(self, state, create=False):
		with self.chat_engine.store.locked(state["id"], create=create) as record:
			record.save(state)

	def submitted_chat(self):
		state, card = self.pending_chat()
		self.bridge.sync()
		self.click(card)
		self.click(card, "s")
		return self.chat_engine.store.read(state["id"]), card

	def restart(self):
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.bridge.chats.engine = self.chat_engine

	def confirmed_chat(self, state):
		state = copy.deepcopy(state)
		state["cards"][-1].update(status="confirmed", confirmed_source="telegram")
		state["turns"].append({"at": chat_record.now(), "role": "assistant", "text": self.private_text,
			"source": "telegram", "kind": "message"})
		self.save_chat(state)


class TelegramChatTest(ChatCase):
	def test_cards_sent_once_across_restart_without_seen_keys_or_record_writes(self):
		state, card = self.pending_chat(3)
		folder = self.hub / "chats" / state["id"]
		before = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in folder.iterdir()}
		self.assertNotIn("cards", self.bridge.chats.records.list_states()[0])
		self.bridge.sync()
		self.bridge.sync()
		self.restart()
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 3)
		self.assertEqual(self.local(card)["kind"], "chat")
		self.assertEqual(self.local(card)["meeting"], state["id"])
		self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in folder.iterdir()})
		with Store().locked() as persisted:
			self.assertEqual(persisted["seen"], [])
		for index, body in enumerate(self.transport.sent()):
			self.assertEqual(body["text"].startswith("PM 대화 질문\n대상 저장소 ID: demo-repo\n"), index == 0)
			self.assertIn("작게 (추천): 한 화면부터 점검", body["text"])
			self.assertIn("추천 이유: 빠르게 확인", body["text"])
			self.assertIn("이 질문 메시지에 답장하세요", body["text"])
			for row in body["reply_markup"]["inline_keyboard"]:
				self.assertLessEqual(len(row[0]["callback_data"].encode()), 64)
		self.chat_call.assert_not_called()

	def test_partial_send_survives_restart_and_retries_only_missing_question(self):
		_, card = self.pending_chat(2)
		original = self.client.send_message
		def send(text, buttons=None):
			if self.transport.message_id == 101:
				raise RuntimeError("합성 후속 전송 실패")
			return original(text, buttons)
		with mock.patch.object(self.client, "send_message", side_effect=send), self.assertRaises(RuntimeError):
			self.bridge.sync()
		self.assertEqual(len(self.local(card)["questions"]), 1)
		self.restart()
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 2)
		self.assertEqual(sum(b["text"].startswith("PM 대화 질문") for b in self.transport.sent()), 1)

	def test_only_last_waiting_card_of_idle_chat_is_sent(self):
		for status, card_status in (("closed", "waiting"), ("busy", "waiting"), ("idle", "answered"), ("idle", "confirmed")):
			state, card = self.pending_chat()
			if card_status != "waiting":
				question_cards.accept(card, card["id"], [{"question_id": "q1", "kind": "choice", "value": "a"}], "office", Rules())
				card["status"] = card_status
			state["status"] = status
			self.save_chat(state)
		state, _ = self.pending_chat()
		state["cards"] = []
		self.save_chat(state)
		self.bridge.sync()
		self.assertEqual(self.transport.sent(), [])
		state, old = self.pending_chat()
		last = question_cards.create([question("q1")], 2, "pm", Rules())
		state["cards"].append(last)
		self.save_chat(state)
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 1)
		with Store().locked() as persisted:
			self.assertNotIn(old["id"], persisted["cards"])
			self.assertIn(last["id"], persisted["cards"])

	def test_questions_are_validated_before_transmission(self):
		for field, value in (("reason", "password=" + "z" * 24), ("reason", self.token), ("id", self.chat)):
			state, card = self.pending_chat()
			card["questions"][0][field] = value
			self.save_chat(state)
		with mock.patch.object(telegram_chat.question_cards, "validate_questions", wraps=question_cards.validate_questions) as validate:
			self.bridge.sync()
			self.assertEqual(validate.call_count, 3)
		self.assertEqual(self.transport.sent(), [])
		with Store().locked() as persisted:
			self.assertEqual(persisted["cards"], {})

	def test_choice_delegate_and_direct_reply_submit_only_to_chat_engine(self):
		state, card = self.pending_chat(3)
		self.bridge.sync()
		self.click(card)
		self.click(card, number=2, option="d")
		self.reply(card, "검색 화면부터 진행해 주세요", number=3)
		self.assertEqual(self.chat_engine.calls, [])
		self.assertIn("직접 답변", [b["text"] for m, b, _ in self.transport.calls if m == "editMessageText"][-1])
		with mock.patch.object(self.engine, "answer") as meeting_answer:
			self.click(card, "s")
			meeting_answer.assert_not_called()
		self.assertEqual(self.chat_engine.calls, [(state["id"], card["id"], [
			{"question_id": "q1", "kind": "choice", "value": "a"},
			{"question_id": "q2", "kind": "delegate", "value": "알아서"},
			{"question_id": "q3", "kind": "text", "value": "검색 화면부터 진행해 주세요"}], "telegram")])
		self.assertEqual(self.callback_text(), "제출했습니다. 요약을 확인해 주세요")
		self.assertTrue(self.transport.sent()[-1]["text"].startswith("답변 요약\n"))
		self.assertEqual(self.transport.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "c|" + card["id"])
		self.click(card, "s")
		self.assertEqual(self.callback_text(), "이미 답했습니다(텔레그램에서)")
		self.assertEqual(len(self.chat_engine.calls), 1)

	def test_reselection_and_reset_preserve_first_question_heading(self):
		_, card = self.pending_chat()
		self.bridge.sync()
		self.click(card, option="2")
		self.click(card, "r")
		self.assertEqual(self.local(card)["pending"], {})
		for method, body, _ in self.transport.calls:
			if method == "editMessageText" and body["message_id"] == self.local(card)["questions"]["q1"]:
				self.assertTrue(body["text"].startswith("PM 대화 질문\n대상 저장소 ID: demo-repo\n"))
		self.click(card)
		self.click(card, "s")
		self.assertEqual(self.chat_engine.calls[-1][2][0]["value"], "a")

	def test_unsafe_reply_and_disallowed_delegation_never_enter_pending(self):
		_, card = self.pending_chat(delegate=False)
		self.bridge.sync()
		self.click(card, option="d")
		for value in (self.token, self.chat, "password=" + "z" * 24, "~" + "/private/file"):
			self.reply(card, value)
			self.assertEqual(self.local(card)["pending"], {})
		self.assertNotIn("알아서 정해 줘", json.dumps(self.transport.sent(), ensure_ascii=False))

	def test_office_or_cli_answer_wins_before_submit_and_during_submit_race(self):
		for source, race in (("office", False), ("cli", False), ("office", True)):
			with self.subTest(source=source, race=race):
				state, card = self.pending_chat()
				self.bridge.sync()
				self.click(card)
				original = self.chat_engine.answer
				answers = [{"question_id": "q1", "kind": "choice", "value": "b"}]
				def answer(*args, **kwargs):
					original(state["id"], card["id"], answers, source=source)
					return original(*args, **kwargs)
				if not race:
					original(state["id"], card["id"], answers, source=source)
				with mock.patch.object(self.chat_engine, "answer", side_effect=answer):
					self.click(card, "s")
				self.assertEqual(self.callback_text(), "이미 답했습니다(사무실·명령줄에서)")
				self.reply(card, "다른 답변은 덮어쓰지 않습니다")
				stored = self.chat_engine.store.read(state["id"])["cards"][-1]["answers"][0]
				self.assertEqual((stored["source"], stored["value"]), (source, "b"))

	def test_summary_recovery_after_send_failure_never_answers_twice(self):
		_, card = self.pending_chat()
		self.bridge.sync()
		self.click(card)
		with mock.patch.object(self.client, "send_message", side_effect=RuntimeError("합성 전송 실패")), self.assertRaises(RuntimeError):
			self.click(card, "s")
		self.assertIsNone(self.local(card)["submit_message"])
		self.restart()
		self.bridge.sync()
		self.bridge.sync()
		self.assertEqual(len(self.chat_engine.calls), 1)
		self.assertEqual(sum(b["text"].startswith("답변 요약\n") for b in self.transport.sent()), 1)

	def test_summary_and_confirmation_require_every_answer_from_telegram(self):
		state, card = self.pending_chat(2)
		self.bridge.sync()
		answers = [{"question_id": "q" + str(i), "kind": "choice", "value": "a"} for i in (1, 2)]
		state = self.chat_engine.answer(state["id"], card["id"], answers, source="telegram")
		state["cards"][-1]["answers"][1]["source"] = "office"
		self.save_chat(state)
		before = len(self.transport.sent())
		self.bridge.sync()
		self.click(card, "c", mid=101)
		self.assertEqual(self.callback_text(), "이미 답했습니다(사무실·명령줄에서)")
		self.assertEqual(len(self.transport.sent()), before)
		self.assertEqual(len(self.bridge._queue), 0)

	def test_other_rooms_senders_and_groups_cannot_choose_reply_or_confirm(self):
		_, card = self.pending_chat()
		self.bridge.sync()
		for room, sender, kind in ((12, int(self.chat), "private"), (int(self.chat), 12, "private"),
				(int(self.chat), int(self.chat), "group")):
			message = {"message_id": 101, "chat": {"id": room, "type": kind}}
			for action in ("a", "s", "c"):
				self.click(card, action, message=message, **{"from": {"id": sender}})
			self.poll(message={**message, "from": {"id": sender}, "text": "합성 답변",
				"reply_to_message": {"message_id": 101}})
		self.assertEqual(self.bridge.ignored_updates, 12)
		self.assertEqual(self.local(card)["pending"], {})
		self.assertEqual(self.chat_engine.calls, [])
		self.assertEqual(len(self.bridge._queue), 0)

	def test_confirmation_request_answers_are_recorded_without_any_chat_action(self):
		_, card = self.pending_chat()
		self.bridge.sync()
		for number, direct in ((999, False), (1000, True)):
			ask = local_ask(number)
			with Store().locked() as state:
				state["asks"][ask["id"]] = ask
			message = {"message_id": number, "chat": {"id": int(self.chat), "type": "private"}}
			if direct:
				self.poll(message={**message, "from": {"id": int(self.chat)}, "reply_to_message": {"message_id": number}, "text": "합성 확인 답변"})
			else:
				self.poll(callback_query={"id": "ask-callback", "data": "k|" + ask["id"] + "|1", "message": message, "from": {"id": int(self.chat)}})
			with Store().locked() as state:
				self.assertEqual(state["asks"][ask["id"]]["answer"]["value"], "합성 확인 답변" if direct else "승인")
		self.assertEqual(self.chat_engine.calls, [])
		self.assertEqual(self.local(card)["pending"], {})
		self.assertEqual(len(self.bridge._queue), 0)

	def test_wrong_card_message_or_oversized_data_never_reads_or_submits(self):
		_, card = self.pending_chat()
		self.bridge.sync()
		for data in ("a|bad|1|1", "s|q-" + "f" * 32, "a|" + card["id"] + "|1|" + "가" * 10,
				"s|" + card["id"] + "|" + "a" * 50, None):
			with mock.patch.object(self.bridge.chats.records, "read") as read:
				self.click(card, data=data)
				read.assert_not_called()
		self.click(card, mid=999)
		self.assertEqual(self.callback_text(), "현재 질문의 버튼을 눌러 주세요")
		self.click(card, number=3, mid=101)
		self.assertEqual(self.callback_text(), "질문을 확인할 수 없습니다")
		self.assertEqual(self.local(card)["pending"], {})
		self.assertEqual(self.chat_engine.calls, [])

	def test_silence_plain_text_and_forbidden_actions_are_never_consent(self):
		state, card = self.pending_chat()
		self.bridge.sync()
		before = self.chat_engine.store.read(state["id"])
		for text in ("네", "동의", "알아서", "대화 글 보내기", "/start", "chat new", "chat close", "개발 지시", "apply", "push", "delete", "restart", "명단 변경"):
			self.poll(message={"chat": {"id": int(self.chat), "type": "private"}, "from": {"id": int(self.chat)}, "text": text})
		for action in ("send", "new", "close", "apply", "run", "push", "delete", "restart", "m"):
			self.click(card, data=action + "|" + state["id"])
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			self.bridge.sync()
			self.bridge.work_once()
			popen.assert_not_called()
		self.assertEqual(self.chat_engine.store.read(state["id"]), before)
		self.assertEqual(self.local(card)["pending"], {})
		self.assertEqual(self.chat_engine.calls, [])
		self.assertEqual(len(self.bridge._queue), 0)


class TelegramChatWorkerTest(ChatCase):
	def test_confirm_queues_once_and_runs_only_fixed_arguments_in_shared_slot(self):
		state, card = self.submitted_chat()
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen, \
				mock.patch.object(telegram_bridge.time, "monotonic", return_value=100):
			self.click(card, "c")
			self.click(card, "c")
			popen.assert_not_called()
			self.assertEqual(list(self.bridge._queue), [("chat_confirm", (state["id"], card["id"]), None)])
			process = popen.return_value
			process.poll.return_value = None
			self.bridge.work_once()
			self.bridge.work_once()
			popen.assert_called_once()
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "chat", "confirm", state["id"],
				"--card", card["id"], "--source", "telegram"])
			self.assertIs(popen.call_args.kwargs["shell"], False)
			for stream in ("stdin", "stdout", "stderr"):
				self.assertEqual(popen.call_args.kwargs[stream], telegram_bridge.subprocess.DEVNULL)
			self.assertEqual(popen.call_args.kwargs["env"]["AI_HUB_HOME"], str(self.hub))
			self.assertEqual(self.bridge._job["deadline"], 1600)
			before = len(self.transport.sent())
			self.confirmed_chat(state)
			process.poll.return_value = 0
			self.bridge.work_once()
			self.bridge.work_once()
		self.assertEqual([b["text"] for b in self.transport.sent()[before:]],
			["대화 " + state["id"][:8] + ": 답이 도착했습니다. 사무실 대화창에서 확인하세요"])
		self.assertIsNone(self.bridge._job)
		self.assertEqual(len(self.bridge._queued), 0)
		self.chat_call.assert_not_called()

	def test_confirm_rechecks_last_card_sources_and_chat_status_before_launch(self):
		for change in ("closed", "busy", "waiting", "confirmed", "office", "new_card", "missing"):
			with self.subTest(change=change):
				state, card = self.submitted_chat()
				self.click(card, "c")
				if change in ("closed", "busy"):
					state["status"] = change
				elif change == "waiting":
					state["cards"][-1].update(status="waiting", answers=[], summary="")
				elif change == "confirmed":
					state["cards"][-1]["status"] = "confirmed"
				elif change == "office":
					state["cards"][-1]["answers"][0]["source"] = "office"
				elif change == "new_card":
					state["cards"].append(question_cards.create([question("q1")], 2, "pm", Rules()))
				self.save_chat(state)
				before = len(self.transport.sent())
				with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
					if change == "missing":
						with mock.patch.object(self.bridge.chats.records, "read", return_value=None):
							self.bridge.work_once()
					else:
						self.bridge.work_once()
					popen.assert_not_called()
				self.assertEqual([b["text"] for b in self.transport.sent()[before:]], [telegram_chat.FAILED])
				self.assertEqual(len(self.bridge._queued), 0)

	def test_meeting_start_resume_and_chat_confirm_use_one_queue_and_slot(self):
		proposal = self.proposal()
		self.click_start(proposal)
		_, meeting_card = self.submitted()
		self.click(meeting_card, "c")
		_, chat_card = self.submitted_chat()
		self.click(chat_card, "c")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			process = popen.return_value
			for index, expected in enumerate((("meeting", "start"), ("meeting", "resume"), ("chat", "confirm")), 1):
				process.poll.return_value = None
				self.bridge.work_once()
				self.bridge.work_once()
				self.assertEqual(popen.call_count, index)
				self.assertEqual(tuple(popen.call_args.args[0][3:5]), expected)
				process.poll.return_value = 0
				self.bridge.work_once()
		self.assertEqual(len(self.bridge._queued), 0)

	def test_launch_failure_is_safe_and_allows_explicit_retry(self):
		_, card = self.submitted_chat()
		self.click(card, "c")
		output = io.StringIO()
		with redirect_stdout(output), redirect_stderr(output), \
				mock.patch.object(telegram_bridge.subprocess, "Popen", side_effect=OSError(self.token + self.chat)):
			self.bridge.work_once()
		self.assertEqual(self.transport.sent()[-1]["text"], telegram_chat.FAILED)
		self.assertEqual(output.getvalue(), "")
		self.assertIsNone(self.bridge._job)
		self.assertEqual(len(self.bridge._queued), 0)
		self.click(card, "c")
		self.assertEqual(len(self.bridge._queue), 1)

	def test_timeout_nonzero_exit_and_engine_failure_send_only_safe_notice(self):
		for kind in ("timeout", "exit", "failure", "unconfirmed"):
			with self.subTest(kind=kind):
				state, card = self.submitted_chat()
				self.click(card, "c")
				with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen, \
						mock.patch.object(telegram_bridge.time, "monotonic", return_value=0) as clock:
					process = popen.return_value
					process.poll.return_value = None
					self.bridge.work_once()
					before = len(self.transport.sent())
					if kind == "timeout":
						clock.return_value = 1500
					else:
						process.poll.return_value = 2 if kind == "exit" else 0
						if kind == "failure":
							state["failure"] = self.private_text
							self.save_chat(state)
					self.bridge.work_once()
					self.bridge.work_once()
					if kind == "timeout":
						process.kill.assert_called_once_with()
						process.wait.assert_called_once_with(timeout=1)
				self.assertEqual([b["text"] for b in self.transport.sent()[before:]], [telegram_chat.FAILED])
				self.assertEqual(len(self.bridge._queued), 0)

	def test_no_pm_turns_credentials_or_raw_errors_in_transmissions_state_or_logs(self):
		state, card = self.submitted_chat()
		self.click(card, "c")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			popen.return_value.poll.return_value = 0
			def launch(*args, **kwargs):
				self.confirmed_chat(state)
				return popen.return_value
			popen.side_effect = launch
			self.bridge.work_once()
		self.transport.error = RuntimeError(self.token + self.chat + self.private_text)
		with self.assertLogs("office.telegram_bridge") as logs:
			try:
				self.bridge.poll_once()
			except Exception as exc:
				telegram_bridge.failure(exc)
		texts = "\n".join(b.get("text", "") for m, b, _ in self.transport.calls if m != "getUpdates")
		persisted = (self.hub / "run/telegram-state.json").read_text()
		for value in (self.private_text, "사무실에서만 확인할 합성 내용", self.token, self.chat):
			self.assertNotIn(value, texts + persisted + "\n".join(logs.output))


class TelegramChatStateTest(ChatCase):
	def test_old_meeting_cards_and_explicit_kinds_survive_restart_without_resending(self):
		_, meeting_card = self.pending()
		_, chat_card = self.pending_chat()
		self.bridge.sync()
		self.assertNotIn("kind", self.local(meeting_card))
		before = len(self.transport.sent())
		self.restart()
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), before)
		self.assertFalse(self.transport.sent()[0]["text"].startswith("PM 대화 질문"))
		self.assertTrue(self.transport.sent()[1]["text"].startswith("PM 대화 질문"))
		with Store().locked() as state:
			state["cards"][meeting_card["id"]]["kind"] = "meeting"
		self.restart()
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), before)
		self.click(meeting_card)
		self.click(meeting_card, "s")
		self.assertEqual(self.local(chat_card)["pending"], {})
		self.assertEqual(self.chat_engine.calls, [])

	def test_kind_and_id_must_match_and_seen_keys_remain_meeting_only(self):
		for kind, prefix in ((None, "m-"), ("meeting", "m-"), ("chat", "c-")):
			row = local_card()
			row["meeting"] = prefix + "1" * 32
			if kind is not None:
				row["kind"] = kind
			state = {**empty(), "cards": {"q-" + "1" * 32: row}}
			self.assertEqual(checked(copy.deepcopy(state)), state)
		for fields in ({"kind": "chat"}, {"meeting": "c-" + "1" * 32}, {"kind": "other"},
				{"kind": None}, {"kind": "meeting", "meeting": "c-" + "1" * 32}, {"kind": "chat", "meeting": "c-bad"}):
			with self.subTest(fields=fields), self.assertRaises(ValueError):
				checked({**empty(), "cards": {"q-" + "1" * 32: {**local_card(), **fields}}})
		with self.assertRaises(ValueError):
			checked({**empty(), "seen": ["chat:c-" + "1" * 32 + ":waiting"]})
