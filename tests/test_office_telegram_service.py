"""사무실 생명주기·오류 격리·메시지 복구를 실제 스레드·프로세스 없이 검사한다."""

import contextlib
import io
from unittest import mock

from office import telegram_bridge
from office.__main__ import main
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
