"""대화 CLI의 한 줄 JSON, 안전한 실패 사유, 종료 코드를 확인한다."""

import contextlib
import io
import json
import os
from unittest import mock

from orchestrator import __main__ as cli, chat, chat_cli
from orchestrator.providers import ProviderError
from test_orchestrator_chat import ChatCase, answer, response


class ChatCliTest(ChatCase):
	def command(self, *args):
		with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
			code = cli.main(["chat", *args])
		return code, output.getvalue(), errors.getvalue()

	def success(self, *args):
		code, output, errors = self.command(*args)
		self.assertEqual((code, errors), (0, ""))
		self.assertEqual(len(output.splitlines()), 1)
		return json.loads(output)

	def test_plans_new_send_status_list_close(self):
		self.assertEqual(self.success("plans"), [{"id": "p01", "tool": "codex"}])
		state = self.success("new", "--target", "ai-hub", "--plan", "p01")
		path = self.root / "input.txt"
		path.write_text("첫째 줄\n둘째 줄")
		state = self.success("send", state["id"], "--text-file", str(path), "--source", "office")
		self.assertEqual(state["turns"][0]["source"], "office")
		self.assertEqual(state["turns"][0]["text"], "첫째 줄\n둘째 줄")
		self.assertEqual(self.success("status", state["id"]), state)
		self.assertEqual(self.success("list")[0]["id"], state["id"])
		self.assertEqual(self.success("close", state["id"])["status"], "closed")
		code, output, errors = self.command("send", state["id"], "--text-file", str(path))
		self.assertEqual((code, output), (2, ""))
		self.assertEqual(errors, "대화 처리 실패: 닫힌 대화에는 보낼 수 없습니다\n")

	def test_answer_file_and_confirm_are_distinct_and_preserve_sources(self):
		state = self.pending()
		card_id = state["cards"][-1]["id"]
		path = self.root / "answers.json"
		path.write_text(json.dumps([answer()]))
		state = self.success("answer", state["id"], "--card", card_id, "--answers-file", str(path), "--source", "telegram")
		self.assertEqual(state["cards"][-1]["status"], "answered")
		self.assertEqual(len(self.ai.calls), 1)
		state = self.success("confirm", state["id"], "--card", card_id, "--source", "office")
		self.assertEqual(state["cards"][-1]["confirmed_source"], "office")
		self.assertEqual(state["turns"][-2]["source"], "office")
		self.assertEqual(len(self.ai.calls), 2)

	def test_provider_and_validation_failure_print_state_reason_and_exit_two(self):
		path = self.root / "input.txt"
		path.write_text("유효한 질문")
		for replies in ([ProviderError("timeout", "acme 비공개 원문")] * 2,
				[response(reply="acme 원문"), response(extra="xyz 원문")]):
			with self.subTest(replies=len(replies)):
				state = self.new()
				self.ai.replies = list(replies)
				code, output, errors = self.command("send", state["id"], "--text-file", str(path))
				self.assertEqual(code, 2)
				self.assertEqual(len(output.splitlines()), 1)
				state = json.loads(output)
				self.assertEqual(errors, "대화 처리 실패: " + state["failure"] + "\n")
				self.assertEqual(state["calls"], 2)
				self.assertNotIn("acme", output + errors)
				self.assertNotIn("xyz", output + errors)
				self.assertNotIn(str(self.root), output + errors)

	def test_parse_config_and_input_errors_never_echo_values_paths_or_tokens(self):
		state = self.new()
		path = self.root / "input.txt"
		path.write_text("acme 내부 자료")
		for args in (("send", state["id"], "--text-file", str(path)), ("status", "../acme"),
				("new", "--target", "ai-hub", "--plan", "xyz"),
				("send", state["id"], "--text-file", str(path), "--source", "acme"),
				("send", state["id"]), ("acme",), ("send", state["id"], "--text-file", str(self.root / "missing"))):
			with self.subTest(command=args[0]):
				code, output, errors = self.command(*args)
				self.assertEqual((code, output), (2, ""))
				self.assertTrue(errors.startswith(chat_cli.FAILURE))
				self.assertEqual(len(errors.splitlines()), 1)
				for value in ("acme", "xyz", str(self.root), "Traceback"):
					self.assertNotIn(value, errors)
		self.write_plans('[[plans]]\nid="acme"\nowner="company"\n')
		code, output, errors = self.command("plans")
		self.assertEqual((code, output), (2, ""))
		self.assertNotIn("acme", errors)
		self.assertNotIn(str(self.root), errors)
		self.mock_call.assert_not_called()

	def test_input_links_special_files_oversize_and_invalid_json_are_rejected(self):
		state = self.new()
		path = self.root / "input.txt"
		path.write_text("유효한 질문")
		for hard in (False, True):
			alias = self.root / ("hard.txt" if hard else "link.txt")
			if hard:
				os.link(path, alias)
			else:
				alias.symlink_to(path)
			self.assertEqual(self.command("send", state["id"], "--text-file", str(alias))[0], 2)
			alias.unlink()
		path.write_text("글" * 2001)
		self.assertEqual(self.command("send", state["id"], "--text-file", str(path))[0], 2)
		fifo = self.root / "pipe"
		os.mkfifo(fifo)
		self.assertEqual(self.command("send", state["id"], "--text-file", str(fifo))[0], 2)
		path.write_text("JSON이 아닌 입력")
		self.assertEqual(self.command("answer", state["id"], "--card", "q01", "--answers-file", str(path))[0], 2)
		self.mock_call.assert_not_called()

	def test_full_slots_and_busy_errors_use_required_korean_reasons(self):
		state = self.new()
		path = self.root / "input.txt"
		path.write_text("유효한 질문")
		for reason in ("이미 답을 쓰는 중입니다", "대화 호출이 가득 찼습니다"):
			with mock.patch.object(chat.Engine, "send", side_effect=chat.ChatError(reason)):
				code, output, errors = self.command("send", state["id"], "--text-file", str(path))
			self.assertEqual((code, output, errors), (2, "", "대화 처리 실패: " + reason + "\n"))

	def test_legacy_cli_dispatch_remains_in_place(self):
		for command, module in (("meeting", "meeting_cli"), ("draft", "draft")):
			with mock.patch("orchestrator." + module + ".main", return_value=17) as handler:
				self.assertEqual(cli.main([command, "합성 인자"]), 17)
				handler.assert_called_once_with(["합성 인자"])
