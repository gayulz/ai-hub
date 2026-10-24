"""사무실 생명주기·오류 격리·메시지 복구를 실제 스레드·프로세스 없이 검사한다."""

import contextlib
import io
import sys
from unittest import mock

from office import telegram_bridge
from office.__main__ import main
from orchestrator import meeting_record
from test_office_telegram_bridge import BridgeCase
from test_office_telegram_client import TelegramCase


class TelegramServiceTest(TelegramCase):
	def setUp(self):
		super().setUp()
		patch = mock.patch.object(telegram_bridge, "_runtime", None)
		patch.start()
		self.addCleanup(patch.stop)

	def test_missing_or_bad_permissions_start_no_threads_and_only_export_safe_status(self):
		with mock.patch.object(telegram_bridge.threading, "Thread") as thread:
			telegram_bridge.start()
			self.assertEqual(telegram_bridge.status(), {"status": "설정 없음", "last_poll": None})
			path = self.config()
			path.chmod(0o644)
			telegram_bridge.start()
			self.assertEqual(telegram_bridge.status(), {"status": "권한 확인 필요", "last_poll": None})
			thread.assert_not_called()
		self.assertFalse((self.hub / "run").exists())

	def test_valid_config_starts_exactly_two_daemon_threads_once_without_network(self):
		self.config()
		with mock.patch.object(telegram_bridge.threading, "Thread") as thread:
			bridge = telegram_bridge.start()
			self.assertIs(telegram_bridge.start(), bridge)
			self.assertEqual(thread.call_count, 2)
			self.assertEqual([call.kwargs["name"] for call in thread.call_args_list], ["telegram-poll", "telegram-worker"])
			self.assertTrue(all(call.kwargs["daemon"] is True for call in thread.call_args_list))
			self.assertEqual(thread.return_value.start.call_count, 2)
		self.assertEqual(telegram_bridge.status(), {"status": "켜짐", "last_poll": None})

	def test_linked_state_folder_disables_bridge_but_never_exposes_exception_text(self):
		self.config()
		outside = self.root / "outside"
		outside.mkdir()
		(self.hub / "run").symlink_to(outside, target_is_directory=True)
		with mock.patch.object(telegram_bridge.threading, "Thread") as thread, self.assertLogs("office.telegram_bridge") as logs:
			self.assertIsNone(telegram_bridge.start())
			thread.assert_not_called()
		self.assertEqual(telegram_bridge.status()["status"], "확인 불가")
		self.assertNotIn(str(outside), "\n".join(logs.output))
		self.assertEqual(list(outside.iterdir()), [])

	def test_server_starts_after_bridge_even_if_start_raises_or_setup_is_missing(self):
		for error in (None, RuntimeError(self.token)):
			order = []
			def begin():
				order.append("telegram")
				if error:
					raise error
			with mock.patch("office.__main__.create_server") as create, mock.patch.object(telegram_bridge, "start") as start:
				start.side_effect = begin
				create.side_effect = lambda port: order.append("server") or mock.MagicMock()
				with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(telegram_bridge, "failure") as failed:
					self.assertEqual(main([]), 0)
				self.assertEqual(order, ["telegram", "server"])
				create.assert_called_once_with(8790)
				if error:
					failed.assert_called_once_with(error)


class TelegramRecoveryTest(BridgeCase):
	def test_multiple_confirmed_cards_run_one_at_a_time_and_report_final_status(self):
		first, first_card = self.submitted()
		self.click(first_card, "c")
		second, second_card = self.submitted()
		self.click(second_card, "c")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			popen.return_value.poll.return_value = None
			self.bridge.work_once()
			self.bridge.work_once()
			popen.assert_called_once()
			self.assertEqual(popen.call_args.args[0][5], first["id"])
			popen.return_value.poll.return_value = 0
			self.bridge.work_once()
			self.bridge.work_once()
			self.assertEqual(popen.call_count, 2)
			self.assertEqual(popen.call_args.args[0][5], second["id"])

	def test_long_summary_is_sent_in_full_with_confirmation_only_on_last_message(self):
		state, card = self.pending(3)
		self.bridge.sync()
		for number in range(1, 4):
			self.reply(card, "가" * 1990, number)
		before = len(self.transport.sent())
		self.click(card, "s")
		summary = self.engine.store.read(state["id"])["cards"][-1]["summary"]
		sent = self.transport.sent()[before:]
		self.assertEqual("".join(body["text"].split("\n", 1)[1] for body in sent), "답변 요약\n" + summary)
		self.assertGreater(len(sent), 1)
		self.assertTrue(all(len(body["text"]) <= 4096 for body in sent))
		self.assertTrue(all("reply_markup" not in body for body in sent[:-1]))
		self.assertEqual(sent[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "확인")

	def test_repeated_selection_is_acknowledged_without_editing_unchanged_message(self):
		_, card = self.pending(2)
		self.bridge.sync()
		self.click(card)
		before = len([method for method, _, _ in self.transport.calls if method == "editMessageText"])
		self.click(card)
		self.assertEqual(self.callback_text(), "선택했습니다")
		self.assertEqual(len([method for method, _, _ in self.transport.calls if method == "editMessageText"]), before)

	def test_worker_retries_both_notification_paths_with_capped_backoff(self):
		clock, attempts, notices = [0], [], []
		def wait(_):
			clock[0] += 1
		def fail_work():
			attempts.append(clock[0])
			raise RuntimeError(self.token)
		def fail_sync():
			notices.append(clock[0])
			raise RuntimeError(self.token)
		self.bridge.stop = mock.Mock(is_set=lambda: clock[0] > 1120, wait=wait)
		with mock.patch.object(telegram_bridge.time, "monotonic", side_effect=lambda: clock[0]), self.assertLogs("office.telegram_bridge") as logs:
			with mock.patch.object(self.bridge, "work_once", side_effect=fail_work), mock.patch.object(self.bridge, "sync", side_effect=fail_sync):
				self.bridge.worker()
		self.assertEqual(attempts, [0, 1, 3, 7, 15, 31, 63, 127, 255, 511, 811, 1111])
		self.assertEqual(notices, attempts)
		self.assertNotIn(self.token, "\n".join(logs.output))
		self.assertTrue(all(line.endswith("RuntimeError") for line in logs.output))

	def test_summary_delivery_failure_is_recovered_without_answering_twice(self):
		state, card = self.pending()
		self.bridge.sync()
		self.click(card)
		with mock.patch.object(self.client, "send_message", side_effect=RuntimeError("합성 전송 실패")):
			with self.assertRaises(RuntimeError):
				self.click(card, "s")
		self.assertEqual(self.engine.store.read(state["id"])["status"], "answers_pending")
		self.assertIsNone(self.local(card)["submit_message"])
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine)
		with mock.patch.object(self.engine, "answer", wraps=self.engine.answer) as answer:
			self.bridge.sync()
			answer.assert_not_called()
		self.assertIn("답변 요약", self.transport.sent()[-1]["text"])
		self.click(card, "c")
		self.assertEqual(len(self.bridge._queue), 1)

	def test_worker_final_notification_uses_record_status_and_discards_process_output(self):
		state, card = self.submitted()
		self.click(card, "c")
		state = self.engine.store.read(state["id"])
		state["cards"][-1]["status"] = "confirmed"
		state["status"] = "participants_pending"
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			popen.return_value.poll.return_value = 0
			def launch(*args, **kwargs):
				self.save(state)
				return popen.return_value
			popen.side_effect = launch
			self.bridge.work_once()
		self.assertTrue(self.transport.sent()[-1]["text"].endswith("참석자 확인 대기"))
		self.assertEqual(len(self.bridge._queued), 0)
		self.assertEqual(len(self.ai.calls), 0)

	def test_old_summary_confirmation_cannot_confirm_a_new_card(self):
		state, card = self.submitted()
		self.click(card, "c")
		self.engine.resume(state["id"], confirm_card=card["id"], source="office")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			self.bridge.work_once()
			popen.assert_not_called()
		self.click(card, "c")
		self.assertEqual(self.callback_text(), "이미 답했습니다(텔레그램에서)")


class TelegramMeetingStartTest(BridgeCase):
	def test_proposal_button_queues_each_meeting_once_and_removes_its_keyboard_safely(self):
		for count, roles in enumerate((("backend", "qa"), ("plan", "backend", "qa"), ("plan", "backend", "frontend", "qa")), 1):
			with self.subTest(roles=roles):
				state = self.proposal(roles)
				text = "참석자 제안이 있습니다\n" + self.token + " " + self.chat + " /private/synthetic"
				message = {"message_id": 101, "chat": {"id": int(self.chat), "type": "private"}, "text": text}
				with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
					self.click_start(state, message=message)
					self.assertEqual(self.callback_text(), "시작합니다")
					self.click_start(state, message=message)
					self.assertEqual(self.callback_text(), "이미 시작을 요청했습니다")
					popen.assert_not_called()
				self.assertEqual(len(self.bridge._queue), count)
				self.assertEqual(len(self.bridge._queued), count)
				edits = [body for method, body, _ in self.transport.calls if method == "editMessageText"]
				self.assertEqual(len(edits), count)
				self.assertEqual(edits[-1]["text"], self.bridge.safe(text) + "\n\n선택됨: 확정하고 회의 시작")
				self.assertEqual(edits[-1]["reply_markup"], {"inline_keyboard": []})
				self.assertEqual(self.engine.store.read(state["id"]), state)
		self.assertEqual(len(self.ai.calls), 0)

	def test_only_pending_unconfirmed_meetings_can_start(self):
		state = self.proposal()
		for status in meeting_record.STATUSES:
			if status == "participants_pending":
				continue
			with self.subTest(status=status):
				state["status"] = status
				self.save(state)
				self.click_start(state)
				self.assertEqual(self.callback_text(), "이미 시작했거나 끝난 회의입니다")
		state.update(status="participants_pending", confirmed=True)
		self.save(state)
		self.click_start(state)
		self.assertEqual(self.callback_text(), "이미 시작했거나 끝난 회의입니다")
		self.click_start({"id": "m-" + "f" * 32})
		self.assertEqual(len(self.bridge._queue), 0)

	def test_bad_ids_extra_participants_and_oversized_callbacks_never_read_or_queue(self):
		state = self.proposal()
		for data in ("m|bad", "m|../invalid", "m|" + "A" * 32, "m|" + "a" * 33,
				"m|" + state["id"] + "|frontend", "m|" + "가" * 21, "m|" + "a" * 70, None):
			with self.subTest(data=data), mock.patch.object(self.bridge.records, "read") as read:
				self.click_start(state, data=data)
				self.assertEqual(self.callback_text(), "허용하지 않는 응답입니다")
				read.assert_not_called()
		self.assertEqual(len(self.bridge._queue), 0)

	def test_other_chats_senders_and_groups_cannot_request_start(self):
		state = self.proposal()
		for chat, sender, kind in ((12, int(self.chat), "private"), (int(self.chat), 12, "private"),
				(int(self.chat), int(self.chat), "group"), (int(self.chat), int(self.chat), "supergroup")):
			self.click_start(state, message={"message_id": 101, "chat": {"id": chat, "type": kind}}, **{"from": {"id": sender}})
		self.assertEqual(self.bridge.ignored_updates, 4)
		self.assertEqual(len(self.bridge._queue), 0)
		self.assertFalse(any(method in ("answerCallbackQuery", "editMessageText") for method, _, _ in self.transport.calls))

	def test_invalid_proposals_cannot_queue_or_offer_a_start_button(self):
		for roles in (("backend",), ("plan", "backend", "frontend", "qa", "design"),
				("backend", "unknown"), ("qa", "qa"), ("backend", {}), ("backend", None), ()):
			with self.subTest(roles=roles):
				state = self.proposal(roles)
				self.click_start(state)
				self.assertEqual(self.callback_text(), "참석자 제안을 확인할 수 없습니다")
				self.assertIsNone(self.bridge.events([state])[0][2])
		self.assertEqual(len(self.bridge._queue), 0)
		self.assertEqual(len(self.ai.calls), 0)

	def test_start_worker_uses_record_roles_fixed_command_one_slot_and_one_completion_notice(self):
		state = self.proposal(("qa", "backend"))
		self.click_start(state)
		before = len(self.transport.sent())
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen, \
				mock.patch.object(telegram_bridge.time, "monotonic", return_value=100):
			process = popen.return_value
			process.poll.return_value = None
			self.bridge.work_once()
			self.bridge.work_once()
			popen.assert_called_once()
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "meeting", "start", state["id"],
				"--participants", "qa", "backend", "--source", "telegram"])
			self.assertIs(popen.call_args.kwargs["shell"], False)
			for stream in ("stdin", "stdout", "stderr"):
				self.assertEqual(popen.call_args.kwargs[stream], telegram_bridge.subprocess.DEVNULL)
			self.assertEqual(popen.call_args.kwargs["env"]["AI_HUB_HOME"], str(self.hub))
			self.assertEqual(self.bridge._job["deadline"], 2200)
			state.update(status="completed", confirmed=True)
			self.save(state)
			process.poll.return_value = 0
			self.bridge.work_once()
			self.bridge.work_once()
		self.assertEqual([body["text"] for body in self.transport.sent()[before:]], ["회의 " + state["id"] + ": 완료"])
		self.assertEqual(len(self.bridge._queued), 0)
		self.assertIsNone(self.bridge._job)
		self.assertEqual(len(self.ai.calls), 0)

	def test_worker_rechecks_status_confirmation_and_exact_proposed_role_order(self):
		for change in ({"status": "running"}, {"confirmed": True}, {"participants": []},
				{"participants": [{"role": "qa"}, {"role": "backend"}]},
				{"participants": [{"role": "frontend"}, {"role": "qa"}]}):
			with self.subTest(change=change):
				state = self.proposal()
				self.click_start(state)
				state.update(change)
				self.save(state)
				before = len(self.transport.sent())
				with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
					self.bridge.work_once()
					popen.assert_not_called()
				self.assertEqual([body["text"] for body in self.transport.sent()[before:]],
					["회의 시작: 실행하지 못했습니다. 사무실에서 시작하세요"])
				self.assertEqual(len(self.bridge._queue), 0)
				self.assertEqual(len(self.bridge._queued), 0)

	def test_edit_failure_does_not_cancel_start_or_leak_exception_text(self):
		state = self.proposal()
		with mock.patch.object(self.client, "edit_message", side_effect=RuntimeError(self.token)), \
				self.assertLogs("office.telegram_bridge") as logs:
			self.click_start(state)
		self.assertNotIn(self.token, "\n".join(logs.output))
		self.assertEqual(self.callback_text(), "시작합니다")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			popen.return_value.poll.return_value = 0
			self.bridge.work_once()
			popen.assert_called_once()

	def test_unreadable_record_before_execution_is_discarded_with_start_failure_notice(self):
		state = self.proposal()
		self.click_start(state)
		with mock.patch.object(self.bridge.records, "read", return_value=None), \
				mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			self.bridge.work_once()
			popen.assert_not_called()
		self.assertEqual(self.transport.sent()[-1]["text"], "회의 시작: 실행하지 못했습니다. 사무실에서 시작하세요")
		self.assertEqual(len(self.bridge._queued), 0)

	def test_launch_failure_sends_only_safe_failure_and_allows_retry(self):
		state = self.proposal()
		self.click_start(state)
		before = len(self.transport.sent())
		with mock.patch.object(telegram_bridge.subprocess, "Popen", side_effect=OSError(self.token)):
			self.bridge.work_once()
		self.assertEqual([body["text"] for body in self.transport.sent()[before:]],
			["회의 시작: 실행하지 못했습니다. 사무실에서 시작하세요"])
		self.assertEqual(len(self.bridge._queued), 0)
		self.assertIsNone(self.bridge._job)
		self.click_start(state)
		self.assertEqual(self.callback_text(), "시작합니다")

	def test_start_and_resume_share_one_worker_slot(self):
		first = self.proposal()
		self.click_start(first)
		second, card = self.submitted()
		self.click(card, "c")
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as popen:
			process = popen.return_value
			process.poll.return_value = None
			self.bridge.work_once()
			self.bridge.work_once()
			self.assertEqual(popen.call_args.args[0][4:6], ["start", first["id"]])
			popen.assert_called_once()
			process.poll.return_value = 0
			self.bridge.work_once()
			process.poll.return_value = None
			self.bridge.work_once()
			self.assertEqual(popen.call_count, 2)
			self.assertEqual(popen.call_args.args[0][4:6], ["resume", second["id"]])
