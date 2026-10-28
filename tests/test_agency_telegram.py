"""임시 원장·실제 질문 저장소와 가짜 봇으로 전송·답 반영·알림을 검사한다."""

import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import agency
from agency import departments, hooks, intake, project, telegram_cli
from agency.__main__ import main
from agency.departments import Department
from collector.config import ConfigError
from office import telegram_ask

SUMMARY = "합성 자료를 정리합니다.\n로컬 결과를 전달합니다.\n외부 실행은 제외합니다."


class FakeClient:
	def __init__(self):
		self.status = "켜짐"
		self.sent, self.edited, self.secrets = [], [], []

	def sensitive(self, value):
		return any(secret in value for secret in self.secrets)

	def send_message(self, text, buttons=None):
		self.sent.append((text, buttons))
		return {"message_id": len(self.sent)}

	def edit_message(self, identity, text, buttons=None):
		self.edited.append((identity, text, buttons))
		return {"message_id": identity}


class AgencyTelegramTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve() / "hub"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		for name in ("_before", "_changed"):
			patch = mock.patch.object(hooks, name, [])
			patch.start()
			self.addCleanup(patch.stop)
		patch = mock.patch("socket.socket.connect", side_effect=AssertionError("실제 네트워크 금지"))
		patch.start()
		self.addCleanup(patch.stop)
		self.department = Department("xyz", "합성 부서", "합성 시험", ("ai-hub",), ("local",),
			True, "xyz", (), "", (), (), {"calls": 5, "minutes": 5})
		self.company = replace(self.department, id="local", external_ai=False)
		patch = mock.patch.object(departments, "load_departments", return_value={
			"xyz": self.department, "local": self.company})
		patch.start()
		self.addCleanup(patch.stop)
		folder = self.hub / "redaction"
		folder.mkdir()
		(folder / "dictionary.json").write_text(json.dumps({"entries": [
			{"value": "합성차단단어", "kind": "회사명", "token": "company01"}]}), encoding="utf-8")
		self.identity = project.new(self.department, "합성 요청", "로컬 자료를 정리해 주세요")["id"]
		self.client = FakeClient()
		name = "agency.telegram_link"
		previous = sys.modules.pop(name, None)
		previous_attribute = agency.__dict__.pop("telegram_link", None)

		def restore():
			sys.modules.pop(name, None)
			if previous is not None:
				sys.modules[name] = previous
			agency.__dict__.pop("telegram_link", None)
			if previous_attribute is not None:
				agency.telegram_link = previous_attribute

		self.addCleanup(restore)
		with mock.patch("office.telegram_client.Client", return_value=self.client):
			self.link = importlib.import_module(name)

	def questions(self, *values):
		opened = [{"no": no, "question": value, "asked": datetime.now().astimezone().isoformat()}
			for no, value in enumerate(values, 1)]
		intake._save_questions(self.identity, {"open": opened, "next_no": len(opened) + 1})

	def state(self):
		return self.link.read_state(self.identity)

	def reply(self, number, value, *, button=False):
		ask_id = next(key for key, ask in self.state()["asks"].items() if ask["no"] == number)
		with self.link.store.locked() as state:
			ask = state["asks"][ask_id]
			if button:
				index = ask["options"].index(value) + 1
				result = telegram_ask.answer_option(self.client, {"data": f"k|{ask_id}|{index}",
					"message": {"message_id": ask["message_id"]}}, state)
				self.assertEqual(result, "선택했습니다")
			else:
				self.assertTrue(telegram_ask.answer_text(self.client,
					{"reply_to_message": {"message_id": ask["message_id"]}, "text": value}, state))

	def ready(self):
		project.update_spec(self.identity, goal="합성 정리", target="로컬 자료", out_of_scope="외부 실행",
			done_criteria=["로컬 결과 전달"], budget={"calls": 5, "minutes": 5})

	def cli(self, *argv, entry=main):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = entry(list(argv))
		return code, out.getvalue(), err.getvalue()

	def test_import_registers_only_enabled_bot_and_disabled_functions_do_nothing(self):
		self.assertEqual(hooks._before, [self.link.sync])
		self.assertEqual(hooks._changed, [self.link.notify])
		hooks.clear()
		self.client.status = "꺼짐"
		with mock.patch("office.telegram_client.Client", return_value=self.client):
			importlib.reload(self.link)
		self.assertEqual(hooks._before, [])
		self.assertEqual(hooks._changed, [])
		with mock.patch.object(project, "list_projects") as listing:
			self.link.sync()
			self.link.notify(self.identity, "접수 중", "막힘")
		listing.assert_not_called()
		self.assertEqual(self.client.sent, [])

	def test_questions_use_exact_text_buttons_private_temporary_files_and_survive_reload(self):
		title = "제" * 45
		with project._edit(self.identity, "시험") as row:
			row["title"] = title
		values = ("납품 위치는 어디인가요?", intake.CONFIRM_PREFIX + SUMMARY)
		self.questions(*values)
		paths = []
		original = telegram_ask.ask

		def inspect(client, store, path, options):
			self.assertEqual(path.parent, project._folder(self.identity))
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			paths.append(path)
			return original(client, store, path, options)

		with mock.patch.object(telegram_ask, "ask", side_effect=inspect):
			self.link.sync()
		for number, (question, options) in enumerate(zip(values,
				(("알아서 정해 줘", "사무실에서 답할게"), ("맞아", "사무실에서 고칠게"))), 1):
			text, buttons = self.client.sent[number - 1]
			self.assertEqual(text, "[사무실 질문] " + title[:40] + f"\n질문 {number}. " + question
				+ "\n\n이 메시지에 답장으로 적거나 버튼을 눌러 주세요.")
			self.assertEqual([row[0]["text"] for row in buttons], list(options))
		self.assertTrue(all(not path.exists() for path in paths))
		self.assertEqual(stat.S_IMODE((project._folder(self.identity) / "telegram.json").stat().st_mode), 0o600)
		with mock.patch("office.telegram_client.Client", return_value=self.client):
			importlib.reload(self.link)
		self.link.sync()
		self.assertEqual(len(self.client.sent), 2)
		self.assertEqual(sorted(self.state()["asks"].values(), key=lambda row: row["no"]),
			[{"no": 1, "done": False}, {"no": 2, "done": False}])

	def test_delegate_button_and_text_reply_are_applied_once_without_ai(self):
		self.questions("누가 정하나요?", "납품 위치는 어디인가요?")
		self.link.sync()
		self.reply(1, "알아서 정해 줘", button=True)
		self.reply(2, "로컬 폴더로 전달해 주세요")
		with mock.patch("agency.ai.ask", side_effect=AssertionError("AI 호출 금지")):
			self.link.sync()
			self.link.sync()
		self.assertEqual([answer["answer"] for answer in project.load(self.identity)["answers"]],
			["알아서 정해 줘(PM 이 판단해 명세에 적는다)", "로컬 폴더로 전달해 주세요"])
		self.assertEqual(self.state()["answered"], 2)
		self.assertTrue(all(ask["done"] for ask in self.state()["asks"].values()))

	def test_confirmation_button_finalizes_spec(self):
		self.ready()
		self.questions(intake.CONFIRM_PREFIX + SUMMARY)
		self.link.sync()
		self.reply(1, "맞아", button=True)
		self.link.sync()
		self.assertEqual(project.load(self.identity)["status"], "명세 확정")
		self.assertEqual(project.load(self.identity)["answers"][-1]["answer"], "맞아")
		self.assertEqual(self.state()["answered"], 1)

	def test_office_buttons_leave_questions_open_without_increasing_answer_count(self):
		self.questions("납품 위치는 어디인가요?", intake.CONFIRM_PREFIX + SUMMARY)
		self.link.sync()
		self.reply(1, "사무실에서 답할게", button=True)
		self.reply(2, "사무실에서 고칠게", button=True)
		self.link.sync()
		self.link.sync()
		self.assertEqual(project.load(self.identity)["answers"], [])
		self.assertEqual(len(intake._questions(self.identity)["open"]), 2)
		self.assertEqual(self.state()["answered"], 0)
		self.assertTrue(all(ask["done"] for ask in self.state()["asks"].values()))
		self.assertEqual(len(self.client.sent), 2)

	def test_office_first_answer_and_answer_validation_error_only_finish_their_ask(self):
		self.questions("첫 질문?", "둘째 질문?", "셋째 질문?")
		self.link.sync()
		self.reply(1, "늦은 답장")
		self.reply(2, "거부될 답장")
		self.reply(3, "반영할 답장")
		intake.answer(self.identity, 1, "사무실에서 먼저 답함")
		original = intake.answer

		def answer(identity, number, value):
			if number == 2:
				raise ValueError("합성 답 거부")
			return original(identity, number, value)

		with mock.patch.object(intake, "answer", side_effect=answer) as answering:
			self.link.sync()
		self.assertEqual([call.args[1] for call in answering.call_args_list], [2, 3])
		self.assertEqual([answer["answer"] for answer in project.load(self.identity)["answers"]],
			["사무실에서 먼저 답함", "반영할 답장"])
		self.assertTrue(all(ask["done"] for ask in self.state()["asks"].values()))
		self.assertEqual(self.state()["answered"], 1)

	def test_sensitive_or_invalid_send_is_not_retried_and_temporary_files_are_deleted(self):
		self.questions("가려야 할 값이 있는 질문?")
		self.client.secrets = ["가려야 할 값"]
		self.link.sync()
		self.link.sync()
		self.assertEqual(self.client.sent, [])
		self.assertEqual(list(self.state()["asks"].values()), [{"no": 1, "done": True}])
		self.assertEqual(list(project._folder(self.identity).glob(".telegram-*")), [])

	def test_partial_send_is_saved_before_transport_failure_and_only_missing_question_retries(self):
		self.questions("첫 질문?", "둘째 질문?")
		original = self.client.send_message

		def send(value, buttons=None):
			if self.client.sent:
				raise RuntimeError("합성 전송 실패")
			return original(value, buttons)

		with mock.patch.object(self.client, "send_message", side_effect=send), self.assertRaises(RuntimeError):
			self.link.sync()
		self.assertEqual(len(self.state()["asks"]), 1)
		self.assertEqual(list(project._folder(self.identity).glob(".telegram-*")), [])
		self.link.sync()
		self.assertEqual(len(self.client.sent), 2)

	def test_company_intake_never_reads_or_sends_questions_and_unknown_department_is_skipped(self):
		with project._edit(self.identity, "시험") as row:
			row["department"] = self.company.id
		with mock.patch.object(intake, "_questions", side_effect=AssertionError("회사 질문 접근 금지")):
			self.link.sync()
		self.assertEqual(self.client.sent, [])
		self.assertFalse((project._folder(self.identity) / "telegram.json").exists())
		with project._edit(self.identity, "시험") as row:
			row["department"] = "unknown"
		self.link.sync()
		self.link.notify(self.identity, "접수 중", "막힘")
		self.assertEqual(self.client.sent, [])

	def test_notify_has_exact_three_lines_and_deduplicates_each_status_across_reload(self):
		with project._edit(self.identity, "시험") as row:
			row.update(title="제" * 45, blocked_reason="이" * 125)
		for after, label, detail in (("막힘", "[막힘]", "이유: " + "이" * 120),
				("납품 대기", "[납품 승인 필요]", "할 일: 사무실 화면에서 납품 묶음을 확인하고 승인하세요"),
				("완료", "[완료]", "결과: 본 가지 반영 완료")):
			self.link.notify(self.identity, "실행 중", after)
			self.link.notify(self.identity, "실행 중", after)
			self.assertEqual(self.client.sent[-1],
				(label + " " + "제" * 40 + "\n프로젝트: " + self.identity + "\n" + detail, None))
		self.assertEqual(len(self.client.sent), 3)
		with mock.patch("office.telegram_client.Client", return_value=self.client):
			importlib.reload(self.link)
		self.link.notify(self.identity, "실행 중", "막힘")
		self.assertEqual(len(self.client.sent), 3)
		with mock.patch.object(project, "now", return_value=datetime.now().astimezone() + timedelta(seconds=2)):
			with project._edit(self.identity, "시험") as row:
				row["blocked_reason"] = "새 이유"
		self.link.notify(self.identity, "실행 중", "막힘")
		self.assertEqual(len(self.client.sent), 4)
		self.link.notify(self.identity, "접수 중", "계획")
		self.assertEqual(len(self.client.sent), 4)

	def test_company_status_notice_contains_only_fixed_text(self):
		with project._edit(self.identity, "시험") as row:
			row.update(department=self.company.id, title="보내면 안 되는 합성 제목", blocked_reason="보내면 안 되는 합성 이유")
		for after in ("막힘", "납품 대기", "완료"):
			self.link.notify(self.identity, "실행 중", after)
		self.assertEqual(self.client.sent, [("회사 프로젝트 1건 상태 변경, 사무실에서 확인", None)] * 3)

	def test_document_completion_uses_same_notice_without_claiming_a_merge(self):
		department = replace(self.department, deliverable="document", repos=())
		with mock.patch.object(departments, "load_departments", return_value={"xyz": department}):
			self.link.notify(self.identity, "검수", "납품 대기")
			self.link.notify(self.identity, "납품 중", "완료")
		self.assertIn("[납품 승인 필요]", self.client.sent[0][0])
		self.assertIn("결과: 문서 납품 완료", self.client.sent[1][0])
		self.assertNotIn("본 가지", self.client.sent[1][0])

	def test_sensitive_reason_drops_third_line_and_failed_notice_can_retry(self):
		with project._edit(self.identity, "시험") as row:
			row["blocked_reason"] = "민감한 이유"
		self.client.secrets = ["민감한 이유"]
		with mock.patch.object(self.client, "send_message", side_effect=RuntimeError("합성 전송 실패")):
			with self.assertRaises(RuntimeError):
				self.link.notify(self.identity, "접수 중", "막힘")
		self.assertEqual(self.state()["notified"], [])
		self.link.notify(self.identity, "접수 중", "막힘")
		self.assertEqual(self.client.sent, [("[막힘] 합성 요청\n프로젝트: " + self.identity, None)])

	def test_state_symlink_is_rejected_without_modifying_target(self):
		target = self.hub / "outside.json"
		target.write_text("보존", encoding="utf-8")
		(project._folder(self.identity) / "telegram.json").symlink_to(target)
		with self.assertRaises(ValueError):
			self.link.sync()
		self.assertEqual(target.read_text(encoding="utf-8"), "보존")

	def test_cli_sync_status_counts_applied_answers_and_disabled_and_invalid_arguments(self):
		self.questions("첫 질문?", "둘째 질문?")
		self.assertEqual(self.cli("telegram", "sync")[0], 0)
		self.reply(1, "알아서 정해 줘", button=True)
		self.reply(2, "사무실에서 답할게", button=True)
		self.assertEqual(self.cli("telegram", "sync")[0], 0)
		code, out, err = self.cli("telegram", "status")
		self.assertEqual((code, err), (0, ""))
		self.assertEqual(out, "텔레그램 연동: 켜짐\n" + self.identity + ": 보낸 질문 2개 · 답 반영 1개\n")
		self.client.status = "꺼짐"
		self.assertEqual(self.cli("telegram", "sync"), (0, "텔레그램 연동이 꺼져 있습니다\n", ""))
		self.assertIn("꺼짐", self.cli("telegram", "status")[1])
		for argv in ((), ("telegram",), ("telegram", "bad"), ("telegram", "sync", "extra"), ("sync",)):
			self.assertEqual(self.cli(*argv, entry=telegram_cli.main)[0], 2)

	def test_cli_failures_have_no_exception_details(self):
		for failure in (ConfigError("합성 비공개 설정"), RuntimeError("합성 비공개 응답")):
			with mock.patch.object(self.link, "sync", side_effect=failure):
				code, out, err = self.cli("telegram", "sync")
			self.assertEqual((code, out), (2, ""))
			self.assertIn("설정과 기록을 확인하세요", err)
			self.assertNotIn(str(failure), err)


if __name__ == "__main__":
	unittest.main()
