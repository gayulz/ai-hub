"""합성 회의·가짜 전송으로 답변 경계·재시작·알림·작업 감시를 확인한다."""

import copy
import json
import sys
from unittest import mock
from urllib.error import URLError

from collector.redact_pii import host_pattern
from office import telegram_bridge
from office.telegram_client import Client
from orchestrator import meeting_prompts, meeting_record, question_cards
from vaultfilter.public import Rules
from test_meeting import MeetingCase, question
from test_office_telegram_client import FakeTransport, TelegramCase


class BridgeCase(MeetingCase):
	def setUp(self):
		super().setUp()
		host_pattern.cache_clear()
		self.addCleanup(host_pattern.cache_clear)
		self.token = "804" + "2001" + ":" + "aB_9-" * 7
		self.chat = str(846000 + 271)
		TelegramCase.config(self)
		self.transport = FakeTransport()
		self.client = Client(transport=self.transport)
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.runs, self.update_id = [], 1
		patch = mock.patch.object(telegram_bridge, "read_runs", side_effect=lambda root: {"items": self.runs})
		patch.start()
		self.addCleanup(patch.stop)

	def pending(self, count=1, delegate=True):
		state = meeting_record.new("합성 화면 검토", "ai-hub")
		questions = [{**question("q" + str(i)), "allow_delegate": delegate} for i in range(1, count + 1)]
		card = question_cards.create(questions, 1, "analysis", Rules())
		state.update(status="questions_pending", phase="round1", cards=[card])
		self.save(state, create=True)
		return state, card

	def save(self, state, create=False):
		with self.engine.store.locked(state["id"], create=create) as record:
			record.save(state)

	def proposal(self, roles=("backend", "qa")):
		state = meeting_record.new("합성 화면 검토", "ai-hub")
		state.update(status="participants_pending", phase="round1",
			participants=[{"role": role, "reason": "서로 다른 관점의 점검"} for role in roles])
		self.save(state, create=True)
		return state

	def click_start(self, meeting, **changes):
		message = {"message_id": 101, "chat": {"id": int(self.chat), "type": "private"},
			"text": "참석자 제안이 있습니다\n합성 화면 검토"}
		self.poll(callback_query={"id": "callback-" + str(self.update_id), "data": "m|" + meeting["id"],
			"message": message, "from": {"id": int(self.chat)}, **changes})

	def local(self, card):
		with self.bridge.state.locked() as state:
			return copy.deepcopy(state["cards"][card["id"]])

	def poll(self, **update):
		self.transport.updates = [{"update_id": self.update_id, **update}]
		self.update_id += 1
		self.bridge.poll_once()

	def click(self, card, action="a", number=1, option="1", mid=None, **changes):
		local = self.local(card)
		data = f'{action}|{card["id"]}' + (f"|{number}|{option}" if action == "a" else "")
		if mid is None:
			mid = local["questions"]["q" + str(number)] if action == "a" else local["submit_message"]
		message = {"message_id": mid, "chat": {"id": int(self.chat), "type": "private"}}
		callback = {"id": "callback-" + str(self.update_id), "data": data,
			"message": message, "from": {"id": int(self.chat)}, **changes}
		self.poll(callback_query=callback)

	def reply(self, card, value, number=1):
		self.poll(message={"chat": {"id": int(self.chat), "type": "private"}, "from": {"id": int(self.chat)},
			"reply_to_message": {"message_id": self.local(card)["questions"]["q" + str(number)]}, "text": value})

	def callback_text(self):
		return [body["text"] for method, body, _ in self.transport.calls if method == "answerCallbackQuery"][-1]

	def submitted(self):
		state, card = self.pending()
		self.bridge.sync()
		self.click(card)
		self.click(card, "s")
		return state, card


class TelegramBridgeTest(BridgeCase):
	def test_questions_sent_once_after_restart_with_options_recommendation_and_short_callbacks(self):
		_, card = self.pending(3)
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 3)
		for sent in self.transport.sent():
			self.assertIn("어느 범위", sent["text"])
			self.assertIn("작게 (추천): 한 화면부터 점검", sent["text"])
			self.assertIn("추천 이유: 빠르게 확인", sent["text"])
			for row in sent["reply_markup"]["inline_keyboard"]:
				for button in row:
					self.assertLessEqual(len(button["callback_data"].encode()), 64)
		self.bridge.sync()
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 3)
		self.assertEqual(len(self.local(card)["questions"]), 3)
		self.assertEqual(len(self.ai.calls), 0)

	def test_partial_send_is_persisted_and_only_missing_question_is_retried(self):
		_, card = self.pending(2)
		original = self.client.send_message
		def send(text, buttons=None):
			if self.transport.message_id == 101:
				raise URLError("합성 후속 전송 실패")
			return original(text, buttons)
		with mock.patch.object(self.client, "send_message", side_effect=send), self.assertRaises(URLError):
			self.bridge.sync()
		self.assertEqual(len(self.local(card)["questions"]), 1)
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 2)

	def test_choice_reselection_reset_and_explicit_submission(self):
		state, card = self.pending(2)
		self.bridge.sync()
		self.click(card)
		self.assertEqual(len(self.transport.sent()), 2)
		self.click(card, option="2")
		self.assertEqual(self.local(card)["pending"]["q1"]["value"], "b")
		self.click(card, number=2)
		self.assertEqual(self.transport.sent()[-1]["text"], "모든 질문에 답했습니다. 제출하시겠습니까?")
		prompt = self.local(card)["submit_message"]
		self.click(card, "r")
		self.assertEqual(self.local(card)["pending"], {})
		self.assertIsNone(self.local(card)["submit_message"])
		self.click(card, "s", mid=prompt)
		self.assertEqual(self.engine.store.read(state["id"])["status"], "questions_pending")
		self.click(card)
		self.click(card, number=2, option="2")
		with mock.patch.object(self.engine, "answer", wraps=self.engine.answer) as answer:
			self.click(card, "s")
		answer.assert_called_once_with(state["id"], card["id"], [
			{"question_id": "q1", "kind": "choice", "value": "a"},
			{"question_id": "q2", "kind": "choice", "value": "b"}], source="telegram")
		self.assertIn("답변 요약", self.transport.sent()[-1]["text"])
		self.assertEqual(self.transport.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "확인")
		self.assertEqual(self.engine.store.read(state["id"])["cards"][-1]["status"], "answered")
		self.assertEqual(len(self.ai.calls), 0)
		before = self.local(card)
		self.click(card)
		self.assertEqual(self.callback_text(), "이미 답했습니다(텔레그램에서)")
		self.assertEqual(self.local(card), before)

	def test_direct_reply_and_delegate_keep_engine_validation(self):
		state, card = self.pending(2)
		self.bridge.sync()
		self.reply(card, "네")
		self.click(card, number=2, option="d")
		self.click(card, "s")
		self.assertIn("구체적인 답변", self.callback_text())
		self.assertEqual(self.engine.store.read(state["id"])["status"], "questions_pending")
		self.reply(card, "검색 화면부터 진행해 주세요")
		self.click(card, "s")
		answers = self.engine.store.read(state["id"])["cards"][-1]["answers"]
		self.assertEqual([(a["kind"], a["source"]) for a in answers], [("text", "telegram"), ("delegate", "telegram")])
		self.assertEqual(answers[1]["value"], "알아서")
		self.assertTrue(answers[1]["delegated"])

	def test_disallowed_delegate_and_unsafe_reply_never_enter_pending_state(self):
		_, card = self.pending(delegate=False)
		self.bridge.sync()
		self.assertNotIn("알아서 정해 줘", json.dumps(self.transport.sent(), ensure_ascii=False))
		self.click(card, option="d")
		self.assertEqual(self.local(card)["pending"], {})
		for value in (self.token, self.chat, "password=" + "z" * 24, "~" + "/private/file"):
			self.reply(card, value)
			self.assertEqual(self.local(card)["pending"], {})
		persisted = (self.hub / "run/telegram-state.json").read_text()
		self.assertNotIn(self.token, persisted)
		self.assertNotIn(self.chat, persisted)

	def test_private_chat_and_sender_must_both_match_and_offset_is_persisted(self):
		_, card = self.pending()
		self.bridge.sync()
		for chat, sender, kind in ((12, int(self.chat), "private"), (int(self.chat), 12, "private"),
				(int(self.chat), int(self.chat), "group"), (int(self.chat), int(self.chat), "supergroup")):
			message = {"message_id": 101, "chat": {"id": chat, "type": kind}}
			self.click(card, message=message, **{"from": {"id": sender}})
			self.poll(message={**message, "from": {"id": sender}, "text": "합성 답변", "reply_to_message": {"message_id": 101}})
		self.assertEqual(self.bridge.ignored_updates, 8)
		self.assertEqual(self.local(card)["pending"], {})
		self.assertFalse(any(m == "answerCallbackQuery" for m, _, _ in self.transport.calls))
		self.assertIsNotNone(self.bridge.health["last_poll"])
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.bridge.poll_once()
		self.assertEqual(self.transport.calls[-1][1]["offset"], 9)
		with self.bridge.state.locked() as persisted:
			self.assertEqual(persisted["offset"], 9)

	def test_office_or_cli_first_answer_wins_even_at_submit_race(self):
		for source in ("office", "cli"):
			state, card = self.pending()
			self.bridge.sync()
			self.click(card)
			original = self.engine.answer
			def race(mid, cid, answers, **kwargs):
				original(mid, cid, answers, source=source)
				return original(mid, cid, answers, **kwargs)
			with mock.patch.object(self.engine, "answer", side_effect=race):
				self.click(card, "s")
			self.assertEqual(self.callback_text(), "이미 답했습니다(사무실·명령줄에서)")
			before = self.local(card)
			self.click(card, option="2")
			self.reply(card, "다른 직접 답변")
			self.assertEqual(self.local(card), before)
			self.assertEqual(self.engine.store.read(state["id"])["cards"][-1]["answers"][0]["source"], source)

	def test_confirm_is_queued_once_and_only_worker_launches_fixed_resume_command(self):
		state, card = self.submitted()
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			popen.return_value.poll.return_value = None
			self.click(card, "c")
			self.click(card, "c")
			popen.assert_not_called()
			self.assertEqual(len(self.bridge._queue), 1)
			self.bridge.work_once()
			self.bridge.work_once()
			popen.assert_called_once()
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "meeting", "resume", state["id"],
				"--confirm-answers", card["id"], "--source", "telegram"])
			self.assertIs(popen.call_args.kwargs["shell"], False)
			self.assertEqual(popen.call_args.kwargs["stdout"], telegram_bridge.subprocess.DEVNULL)
			self.assertEqual(popen.call_args.kwargs["stderr"], telegram_bridge.subprocess.DEVNULL)
			self.assertEqual(popen.call_args.kwargs["env"]["AI_HUB_HOME"], str(self.hub))
		self.assertEqual(len(self.ai.calls), 0)

	def test_worker_timeout_and_ten_second_notifications_continue_during_resume(self):
		_, card = self.submitted()
		self.click(card, "c")
		clock, waits = [0], []
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen, mock.patch.object(telegram_bridge.time, "monotonic", side_effect=lambda: clock[0]):
			process = popen.return_value
			process.poll.return_value = None
			self.bridge.work_once()
			self.assertEqual(self.bridge._job["deadline"], 2100)
			def wait(seconds):
				waits.append(seconds)
				clock[0] += 10
			self.bridge.stop = mock.Mock(is_set=lambda: len(waits) >= 2, wait=wait)
			with mock.patch.object(self.bridge, "sync") as sync:
				self.bridge.worker()
				self.assertEqual(sync.call_count, 2)
			process.kill.assert_not_called()
			clock[0] = 2100
			self.bridge.work_once()
			process.kill.assert_called_once_with()
			process.wait.assert_called_once_with(timeout=1)
		self.assertTrue(self.transport.sent()[-1]["text"].endswith("시간 초과"))
		self.assertNotIn("reply_markup", self.transport.sent()[-1])

	def test_non_approval_runs_do_not_create_or_reuse_meeting_events(self):
		state = meeting_record.new("합성 안건", "ai-hub")
		state["status"] = "completed"
		expected = self.bridge.events([state])
		for status in ("완료", "작업 중"):
			self.runs = [{"id": "2026-10-24-001", "status": status}]
			for meetings, events in (([], []), ([state], expected)):
				with self.subTest(status=status, meetings=bool(meetings)):
					self.assertEqual(self.bridge.events(meetings), events)

	def test_baseline_new_statuses_approval_privacy_and_restart_deduplication(self):
		old = meeting_record.new("과거 안건", "ai-hub")
		old["status"] = "participants_pending"
		self.save(old, create=True)
		self.runs = [{"id": "2026-10-24-001", "status": "승인 필요", "public": True, "goal": "과거 목표"}]
		self.bridge.sync()
		self.assertEqual(self.transport.sent(), [])
		for status in ("participants_pending", "completed", "failed", "budget_exhausted", "questions_exhausted"):
			state = meeting_record.new("새 안건 /tmp/private-data", "ai-hub")
			state["status"] = status
			state["participants"] = [{"role": role, "reason": "합성 점검"} for role in ("backend", "qa")]
			state["summary"] = {"conclusion": [{"text": "결론 /private/report " + self.token + " " + "가" * 400}]}
			self.save(state, create=True)
		self.runs += [{"id": "2026-10-24-002", "status": "승인 필요", "public": True, "goal": "새 목표 /tmp/private-file"},
			{"id": "2026-10-24-003", "status": "승인 필요", "public": False, "goal": "전송 금지 합성 자료"}]
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 7)
		texts = "\n".join(body["text"] for body in self.transport.sent())
		self.assertIn("명단을 바꾸려면 사무실에서 하세요", texts)
		self.assertIn("새 목표", texts)
		for value in ("전송 금지 합성 자료", "/tmp/", "/private/", self.token):
			self.assertNotIn(value, texts)
		self.assertEqual(sum("reply_markup" in body for body in self.transport.sent()), 1)
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		self.bridge.sync()
		self.assertEqual(len(self.transport.sent()), 7)

	def test_only_proposal_button_can_start_and_silence_or_other_commands_cannot(self):
		state, card = self.pending()
		proposal = self.proposal()
		self.bridge.sync()
		for command in ("/start", "apply", "meeting start", "run", "push", "delete", "restart", "backend frontend", "m|" + proposal["id"]):
			self.poll(message={"chat": {"id": int(self.chat), "type": "private"}, "from": {"id": int(self.chat)}, "text": command})
		for data in ("apply|" + proposal["id"], "run|" + proposal["id"], "m|" + proposal["id"] + "|frontend"):
			self.click_start(proposal, data=data)
		self.bridge.sync()
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			self.bridge.work_once()
			popen.assert_not_called()
		self.assertEqual(len(self.bridge._queue), 0)
		self.assertEqual(self.local(card)["pending"], {})
		self.assertEqual(self.engine.store.read(state["id"])["status"], "questions_pending")
		self.assertEqual(self.engine.store.read(proposal["id"]), proposal)
		self.click_start(proposal)
		self.assertEqual(self.callback_text(), "시작합니다")
		self.assertEqual(len(self.bridge._queue), 1)
		self.assertEqual(len(self.ai.calls), 0)

	def test_poll_backoff_is_capped_and_logs_only_error_kind(self):
		self.transport.error = URLError("https://api.telegram.org/bot" + self.token + "/getUpdates")
		waits = []
		self.bridge.stop = mock.Mock(is_set=lambda: len(waits) >= 11, wait=waits.append)
		with self.assertLogs("office.telegram_bridge", level="WARNING") as captured:
			self.bridge.poll()
		self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300])
		self.assertTrue(all(line.endswith("URLError") for line in captured.output))
		self.assertNotIn(self.token, "\n".join(captured.output))
		self.assertNotIn(self.token, (self.hub / "run/telegram-state.json").read_text())
		self.assertEqual(self.bridge.health, {"status": "확인 불가", "last_poll": None})
		self.transport.error = None
		self.bridge.poll_once()
		self.assertEqual(self.bridge.health["status"], "켜짐")

	def test_paths_hide_only_urls_home_absolute_and_drive_paths(self):
		for value in ("확정/시작", "UI/UX", "24/7", "office/static/office.js", "src\\module.py"):
			with self.subTest(value=value):
				self.assertEqual(self.bridge.safe(value), value)
		for value in ("/Users/x/y", "~/.ai-hub/z", "https://example.invalid/a", "http://example.invalid/a",
				"/home/x/y", "/private/x/y", "/var/x/y", "/tmp/x/y", "C:\\Users\\x\\y", "D:/work/file"):
			with self.subTest(value=value):
				self.assertEqual(self.bridge.safe(value), "[경로 생략]")
		self.assertEqual(self.bridge.safe("목표: 확정/시작 경로: /Users/x/y"), "목표: 확정/시작 경로: [경로 생략]")
		for value in (self.token, self.chat, "ghp_" + "z" * 36, "x" + "@example.invalid", "password=" + "z" * 24):
			self.assertNotIn(value, self.bridge.safe(value))

	def test_previews_have_requested_limits_and_ellipsis_only_when_truncated(self):
		mid = "m-" + "1" * 32
		for extra in (0, 1):
			self.runs = [{"id": "2026-10-24-001", "status": "승인 필요", "public": True, "goal": "가" * (300 + extra)}]
			meetings = [{"id": mid, "status": "participants_pending", "agenda": "나" * (200 + extra), "confirmed": False,
				"participants": [{"role": role, "reason": "합성 점검"} for role in ("backend", "qa")]},
				{"id": "m-" + "2" * 32, "status": "completed", "summary": {"conclusion": [{"text": "다" * (1500 + extra)}]}}]
			events = {key: text for key, text, _ in self.bridge.events(meetings)}
			suffix = "…" if extra else ""
			self.assertEqual(events["run:2026-10-24-001"].split("\n", 1)[1], "가" * 300 + suffix)
			self.assertEqual(events[f"meeting:{mid}:participants_pending"].splitlines()[1], "나" * 200 + suffix)
			self.assertEqual(events["meeting:m-" + "2" * 32 + ":completed"].split("\n", 1)[1],
				"다" * 1500 + ("…\n전문은 사무실 회의 화면에서" if extra else ""))

	def test_proposal_notice_has_one_start_button_safe_participants_and_limits(self):
		self.bridge.sync()
		state = self.proposal()
		state["agenda"] = "안" * 201
		state["participants"][0]["reason"] = self.token + " /private/synthetic " + "이" * 100
		state["participants"][1]["reason"] = "짧은 이유\n한 줄로"
		self.save(state)
		self.bridge.sync()
		sent = self.transport.sent()[-1]
		lines = sent["text"].splitlines()
		self.assertEqual(lines[:2], ["참석자 제안이 있습니다", "안" * 200 + "…"])
		masked = "[보안값] [경로 생략] "
		self.assertEqual(lines[2], meeting_prompts.ROLES["backend"]["name"] + ": " + masked + "이" * (60 - len(masked)) + "…")
		self.assertEqual(lines[3], meeting_prompts.ROLES["qa"]["name"] + ": 짧은 이유 한 줄로")
		self.assertEqual(lines[-1], "[확정하고 회의 시작]을 누르면 이 명단 그대로 시작합니다(최대 30분, AI 호출 최대 14회). 명단을 바꾸려면 사무실에서 하세요")
		self.assertEqual(sent["reply_markup"]["inline_keyboard"], [[{"text": "확정하고 회의 시작", "callback_data": "m|" + state["id"]}]])
		self.assertLessEqual(len(("m|" + state["id"]).encode("utf-8")), 64)
		self.assertNotIn(self.token, sent["text"])
		self.assertNotIn("/private/synthetic", sent["text"])
		state["status"] = "completed"
		self.runs = [{"id": "2026-10-24-001", "status": "승인 필요", "public": True, "goal": "합성 목표"}]
		self.save(state)
		self.assertTrue(all(buttons is None for _, _, buttons in self.bridge.events([state])))
		self.bridge.sync()
		self.assertTrue(all("reply_markup" not in body for body in self.transport.sent()[1:]))

	def test_completed_notice_includes_at_most_eight_single_line_todos_from_engine_schema(self):
		state = {"id": "m-" + "3" * 32, "status": "completed", "summary": {
			"conclusion": [{"text": "첫 결론"}, {"text": "다음 결론"}],
			"todos": [{"text": "항목 " + str(i) + "\n계속 /private/synthetic"} for i in range(10)],
			"open_questions": [{"text": "전송 대상 아님"}]}}
		value = self.bridge.events([state])[0][1]
		self.assertIn("첫 결론\n다음 결론\n할 일\n", value)
		self.assertEqual(value.split("\n할 일\n")[1].splitlines(),
			["- 항목 " + str(i) + " 계속 [경로 생략]" for i in range(8)])
		self.assertNotIn("전송 대상 아님", value)
		self.assertNotIn("…", value)

	def test_long_question_and_edit_keep_all_text_and_track_buttons_last_message(self):
		state, card = self.pending(2)
		q = card["questions"][0]
		q.update(text="문" * 500, reason="이" * 500, options=[
			{"id": key, "label": label * 500, "difference": "차" * 500, "reply": key + "안"}
			for key, label in (("a", "가"), ("b", "나"), ("c", "다"))])
		self.save(state)
		with mock.patch("office.telegram_client.time.sleep"):
			self.bridge.sync()
			original, _ = self.bridge.question(card, 1)
			sent = self.transport.sent()[:-1]
			self.assertGreater(len(sent), 1)
			self.assertEqual("".join(body["text"].split("\n", 1)[1] for body in sent), original)
			self.assertEqual(self.local(card)["questions"]["q1"], self.transport.message_id - 1)
			before = len(self.transport.calls)
			self.click(card)
			calls = [body for method, body, _ in self.transport.calls[before:] if method in ("sendMessage", "editMessageText")]
			self.assertEqual("".join(body["text"].split("\n", 1)[1] for body in calls), original + "\n\n선택됨: " + "가" * 500)
			self.assertEqual(self.local(card)["questions"]["q1"], self.transport.message_id)
			self.click(card, option="2")
			self.assertEqual(self.local(card)["pending"]["q1"]["value"], "b")
			self.click(card, number=2)
			reset_mid = self.local(card)["questions"]["q1"]
			self.click(card, "r")
			self.assertEqual(self.local(card)["pending"], {})
			self.assertEqual(self.local(card)["questions"]["q1"], reset_mid)
