"""확인 요청 명령줄과 유일한 수신자의 답 기록을 합성 홈·전송으로 검사한다."""

import contextlib
import copy
import io
import json
import traceback
from unittest import mock

from collector.redact_pii import host_pattern
from office import telegram_ask, telegram_bridge
from office.telegram_client import Client
from office.telegram_state import Store
from test_office_telegram_client import TelegramCase


class AskCase(TelegramCase):
	def setUp(self):
		super().setUp()
		self.config()
		(self.hub / "redaction").mkdir()
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "synthetic-corp", "kind": "회사명", "token": "company01"},
			{"value": "synthetic-person", "kind": "사람이름", "token": "person01"}]}), encoding="utf-8")
		patch = mock.patch("vaultfilter.public.getpass.getuser", return_value="synthetic-user")
		patch.start()
		self.addCleanup(patch.stop)
		host_pattern.cache_clear()
		self.addCleanup(host_pattern.cache_clear)
		self.client = Client(transport=self.transport)
		self.store = Store()
		self.engine = mock.Mock()
		self.bridge = telegram_bridge.Bridge(self.client, engine=self.engine, records=mock.Mock(), state=self.store)
		self.path = self.root / "question.txt"
		self.update_id = 0

	def cli(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(telegram_ask, "Client", return_value=self.client):
			code = telegram_ask.main(list(args))
		return code, out.getvalue(), err.getvalue()

	def send(self, value="다음 점검 범위를 확인해 주세요", options="승인,거절,보류"):
		self.path.write_text(value, encoding="utf-8")
		code, out, err = self.cli("ask", "--text-file", str(self.path), "--options", options)
		self.assertEqual((code, err), (0, ""))
		return out.strip()

	def local(self, identity):
		with self.store.locked() as state:
			return copy.deepcopy(state["asks"][identity])

	def poll(self, **update):
		self.update_id += 1
		self.transport.updates = [{"update_id": self.update_id, **update}]
		self.bridge.poll_once()

	def click(self, identity, option="1", *, chat=None, sender=None, kind="private", mid=None, data=None):
		self.poll(callback_query={"id": "callback-" + str(self.update_id),
			"data": data if data is not None else f"k|{identity}|{option}",
			"message": {"message_id": self.local(identity)["message_id"] if mid is None else mid,
				"chat": {"id": int(self.chat) if chat is None else chat, "type": kind}},
			"from": {"id": int(self.chat) if sender is None else sender}})

	def reply(self, identity, value, **changes):
		self.poll(message={"chat": {"id": int(self.chat), "type": "private"}, "from": {"id": int(self.chat)},
			"reply_to_message": {"message_id": self.local(identity)["message_id"]}, "text": value, **changes})

	def callback_text(self):
		return [body["text"] for method, body, _ in self.transport.calls if method == "answerCallbackQuery"][-1]

	def assert_private_output(self, *values):
		path = self.hub / "run/telegram-state.json"
		persisted = path.read_text(encoding="utf-8") if path.exists() else ""
		for value in (*values, persisted):
			self.assertNotIn(self.token, value)
			self.assertNotIn(self.chat, value)
			self.assertNotIn("api.telegram.org", value)


class TelegramAskCommandTest(AskCase):
	def test_send_id_buttons_and_state_roundtrip_with_no_receiver_or_question_copy(self):
		identity = self.send("가" * 3000, "가" * 20 + ",거절,보류,다시 확인")
		self.assertRegex(identity, r"^k-[a-f0-9]{8}$")
		row = self.local(identity)
		self.assertEqual(set(row), {"id", "message_id", "options", "created", "answer"})
		self.assertEqual(row["id"], identity)
		self.assertEqual(row["message_id"], self.transport.message_id)
		self.assertIsNone(row["answer"])
		self.assertEqual(self.transport.sent()[0]["text"], "가" * 3000)
		self.assertEqual(len(self.transport.calls), 1)
		for index, buttons in enumerate(self.transport.sent()[0]["reply_markup"]["inline_keyboard"], 1):
			self.assertEqual(buttons[0]["callback_data"], f"k|{identity}|{index}")
			self.assertLessEqual(len(buttons[0]["callback_data"].encode()), 64)
		self.assert_private_output(identity, json.dumps(row, ensure_ascii=False))
		self.assertNotIn("가" * 3000, (self.hub / "run/telegram-state.json").read_text())
		self.assertEqual(self.cli("ask-status", identity), (0, json.dumps({"id": identity, "answer": None}) + "\n", ""))

	def test_invalid_question_options_and_sensitive_fields_send_nothing_and_exit_two(self):
		unsafe = ("synthetic-corp의 점검", "synthetic-person의 점검", "password=" + "z" * 24,
			"ghp_" + "a" * 36, "x" + "@example.invalid", "/Users/" + "synthetic-user/work",
			"/home/other/work", "~" + "/work", self.token, self.chat)
		cases = [(value, "승인,거절") for value in ("", " ", "가" * 3001, "질문\x00", *unsafe)]
		cases += [("합성 질문", option) for option in ("승인", "하나,둘,셋,넷,다섯", "승인,", "가" * 21 + ",거절", *["승인," + v for v in unsafe])]
		for question, options in cases:
			with self.subTest(number=cases.index((question, options))):
				self.path.write_text(question, encoding="utf-8")
				code, out, err = self.cli("ask", "--text-file", str(self.path), "--options", options)
				self.assertEqual((code, out), (2, ""))
				self.assertTrue(err)
				self.assertEqual(self.transport.calls, [])
				self.assert_private_output(out, err)

	def test_missing_config_missing_dictionary_bad_arguments_and_provider_errors_are_safe(self):
		self.path.write_text("합성 질문", encoding="utf-8")
		args = ("ask", "--text-file", str(self.path), "--options", "승인,거절")
		(self.hub / "telegram.toml").unlink()
		self.client = Client(transport=self.transport)
		self.assertEqual(self.cli(*args)[0], 2)
		self.config()
		self.client = Client(transport=self.transport)
		dictionary = self.hub / "redaction/dictionary.json"
		before = dictionary.read_bytes()
		dictionary.unlink()
		self.assertEqual(self.cli(*args)[0], 2)
		self.assertEqual(self.transport.calls, [])
		dictionary.write_bytes(before)
		self.transport.error = RuntimeError("https://api.telegram.org/bot" + self.token + "/sendMessage " + self.chat)
		code, out, err = self.cli(*args)
		self.assertEqual(code, 2)
		self.assert_private_output(out, err)
		for values in ((self.token,), ("ask-status", self.token), ("ask-status", self.chat),
				("ask-wait", "k-1234abcd"), ("ask-wait", "k-1234abcd", "--timeout", self.token)):
			code, out, err = self.cli(*values)
			self.assertEqual(code, 2)
			self.assert_private_output(out, err)

	def test_wait_success_status_and_timeout_use_only_state_and_one_second_intervals(self):
		identity = self.send()
		clock, sleeps = [0], []
		def arrive(seconds):
			clock[0] += seconds
			sleeps.append(seconds)
			self.reply(identity, "다음 화면까지 점검해 주세요")
		with mock.patch.object(telegram_ask.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(telegram_ask.time, "sleep", side_effect=arrive):
			code, out, err = self.cli("ask-wait", identity, "--timeout", "5")
		self.assertEqual((code, err, sleeps), (0, "", [1]))
		answer = self.local(identity)["answer"]
		self.assertEqual(json.loads(out), {"id": identity, **answer})
		self.assertEqual(len(out.splitlines()), 1)
		self.assertEqual(json.loads(self.cli("ask-status", identity)[1]), {"id": identity, "answer": answer})
		self.assert_private_output(out, err)
		identity = self.send()
		before = len(self.transport.calls)
		clock[0], sleeps = 0, []
		def elapsed(seconds):
			clock[0] += seconds
			sleeps.append(seconds)
		with mock.patch.object(telegram_ask.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(telegram_ask.time, "sleep", side_effect=elapsed):
			self.assertEqual(self.cli("ask-wait", identity, "--timeout", "2"), (3, "", ""))
		self.assertEqual(sleeps, [1, 1])
		self.assertEqual(len(self.transport.calls), before)
		for value in ("0", "-1", "86401", "nan", "1.5"):
			self.assertEqual(self.cli("ask-wait", identity, "--timeout", value)[0], 2)
		with mock.patch.object(telegram_ask, "wait", return_value=3) as wait:
			code, out, err = self.cli("ask", "--text-file", str(self.path), "--options", "승인,거절", "--timeout", "86400")
		self.assertEqual((code, err), (3, ""))
		self.assertEqual(len(out.splitlines()), 1)
		wait.assert_called_once_with(mock.ANY, out.strip(), 86400, emit=False)


class TelegramAskAnswerTest(AskCase):
	def test_button_first_answer_wins_repeated_click_is_acknowledged_and_never_runs(self):
		identity = self.send()
		with mock.patch.object(telegram_bridge.subprocess, "Popen") as process:
			self.click(identity, "2")
			answer = self.local(identity)["answer"]
			self.assertEqual((answer["kind"], answer["value"]), ("option", "거절"))
			self.assertTrue(answer["at"])
			edits = [body for method, body, _ in self.transport.calls if method == "editMessageText"]
			self.assertEqual(edits[-1]["text"], "선택됨: 거절")
			self.assertEqual(edits[-1]["reply_markup"], {"inline_keyboard": []})
			self.assertEqual(self.callback_text(), "선택했습니다")
			self.click(identity, "1")
			self.assertEqual(self.callback_text(), "이미 답했습니다")
			self.reply(identity, "다른 합성 답변")
			self.assertEqual(self.local(identity)["answer"], answer)
			self.bridge.work_once()
			process.assert_not_called()
		self.assertEqual(len(self.bridge._queue), 0)
		self.engine.assert_not_called()
		self.assertEqual(self.engine.method_calls, [])
		self.assertEqual(len([method for method, _, _ in self.transport.calls if method == "editMessageText"]), 1)
		self.assert_private_output()

	def test_other_chat_sender_group_bad_index_wrong_message_and_unknown_id_are_ignored(self):
		identity = self.send()
		for changes in ({"chat": 12}, {"sender": 12}, {"kind": "group"}, {"kind": "supergroup"}):
			self.click(identity, **changes)
		self.assertFalse(any(method == "answerCallbackQuery" for method, _, _ in self.transport.calls))
		for option in ("0", "4", "-1", "99", "no", "1|1"):
			self.click(identity, option)
		self.click(identity, mid=999)
		for data in ("k|k-00000000|1", "k|bad|1", "k|" + "a" * 70):
			self.click(identity, data=data)
		self.reply(identity, "다른 대화방", chat={"id": 12, "type": "private"})
		self.reply(identity, "다른 사용자", **{"from": {"id": 12}})
		self.assertIsNone(self.local(identity)["answer"])
		self.assertFalse(any(method == "editMessageText" for method, _, _ in self.transport.calls))
		self.click(identity, "3")
		self.assertEqual(self.local(identity)["answer"]["value"], "보류")

	def test_reply_text_first_answer_wins_boundaries_and_general_messages_are_discarded(self):
		identity = self.send()
		for value in ("", " ", "가" * 1001, self.token, self.chat, "password=" + "z" * 24,
				"synthetic-corp", "x" + "@example.invalid", "~" + "/work", None):
			self.reply(identity, value)
			self.assertIsNone(self.local(identity)["answer"])
		for reply in ({}, None, [], {"message_id": 999}):
			self.reply(identity, "일반 메시지", reply_to_message=reply)
		self.assertIsNone(self.local(identity)["answer"])
		for value in ("가", "가" * 1000, "명령: apply push restart"):
			identity = self.send()
			self.reply(identity, value)
			answer = self.local(identity)["answer"]
			self.assertEqual((answer["kind"], answer["value"]), ("text", value))
			self.click(identity)
			self.assertEqual(self.callback_text(), "이미 답했습니다")
			self.assertEqual(self.local(identity)["answer"], answer)
		self.assertEqual(self.engine.method_calls, [])
		self.assertEqual(len(self.bridge._queue), 0)
		self.assert_private_output()

	def test_answer_survives_edit_failure_without_leaking_provider_exception_or_overwriting(self):
		identity = self.send()
		original = self.client.edit_message
		def fail(*args, **kwargs):
			self.transport.error = RuntimeError(self.token + self.chat)
			return original(*args, **kwargs)
		with mock.patch.object(self.client, "edit_message", side_effect=fail):
			try:
				self.click(identity)
			except Exception as exc:
				self.assert_private_output("".join(traceback.format_exception(exc)))
			else:
				self.fail("정제한 전송 오류가 필요합니다")
		self.transport.error = None
		answer = self.local(identity)["answer"]
		self.click(identity, "2")
		self.assertEqual(self.local(identity)["answer"], answer)
		self.assertEqual(self.callback_text(), "이미 답했습니다")
