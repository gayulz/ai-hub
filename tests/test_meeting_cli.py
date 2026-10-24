"""새 회의 명령과 기존 실행기 분기 보존."""

import contextlib
import io
import json
from unittest import mock

from orchestrator import __main__ as cli
from test_meeting import MeetingCase, question
from test_meeting_questions import answer


class MeetingCliTest(MeetingCase):
	def command(self, *args):
		with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
			code = cli.main(["meeting", *args])
		return code, output.getvalue(), errors.getvalue()

	def test_analyze_status_confirm_start_and_resume_json(self):
		path = self.root / "agenda.txt"
		path.write_text("합성 안건")
		code, output, errors = self.command("analyze", "--target", "ai-hub", "--text-file", str(path))
		self.assertEqual((code, errors), (0, ""))
		state = json.loads(output)
		self.assertEqual(self.command("status", state["id"])[0], 0)
		self.assertEqual(len(self.ai.calls), 1)
		code, output, errors = self.command("start", state["id"], "--participants", "backend", "qa")
		self.assertEqual((code, errors), (0, ""))
		self.assertEqual(json.loads(output)["status"], "completed")
		self.assertEqual(json.loads(output)["participant_confirmation"]["source"], "cli")
		self.assertEqual(self.command("resume", state["id"])[0], 0)

	def test_cli_start_records_explicit_source_and_rejects_unknown_source(self):
		for source in ("telegram", "office"):
			with self.subTest(source=source):
				state = self.analyze()
				code, output, errors = self.command("start", state["id"], "--participants", *self.ai.roles, "--source", source)
				self.assertEqual((code, errors), (0, ""))
				self.assertEqual(json.loads(output)["participant_confirmation"]["source"], source)
		state = self.analyze()
		with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
			self.command("start", state["id"], "--participants", *self.ai.roles, "--source", "unknown")
		self.assertEqual(error.exception.code, 2)
		self.assertEqual(self.engine.store.read(state["id"]), state)

	def test_cli_accepts_a_configured_participant_added_to_the_proposal(self):
		state = self.analyze()
		self.ai.roles = ("backend", "frontend", "qa")
		code, output, errors = self.command("start", state["id"], "--participants", *self.ai.roles)
		self.assertEqual((code, errors), (0, ""))
		state = json.loads(output)
		self.assertEqual(state["status"], "completed")
		self.assertEqual(state["participant_confirmation"]["roles"], list(self.ai.roles))
		self.assertEqual([p["role"] for p in state["analysis"]["participants"]], ["backend", "qa"])

	def test_question_answers_file_and_explicit_summary_confirmation(self):
		self.ai.analysis_questions = [question()]
		state = self.analyze()
		card = state["cards"][-1]
		path = self.root / "answers.json"
		path.write_text(json.dumps([answer()]))
		code, output, errors = self.command("answer", state["id"], "--card", card["id"], "--answers-file", str(path))
		self.assertEqual((code, errors), (0, ""))
		self.assertEqual(json.loads(output)["status"], "answers_pending")
		code, output, errors = self.command("resume", state["id"], "--confirm-answers", card["id"])
		self.assertEqual((code, errors), (0, ""))
		self.assertEqual(json.loads(output)["status"], "participants_pending")
		self.assertEqual(json.loads(output)["cards"][-1]["confirmed_source"], "cli")

	def test_source_options_preserve_answers_and_confirmation_sources(self):
		for source in ("office", "telegram"):
			self.ai.analysis_questions = [question()]
			state = self.analyze()
			card = state["cards"][-1]
			path = self.root / "answers.json"
			path.write_text(json.dumps([answer()]))
			code, output, errors = self.command("answer", state["id"], "--card", card["id"], "--answers-file", str(path), "--source", source)
			self.assertEqual((code, errors), (0, ""))
			self.assertEqual(json.loads(output)["cards"][-1]["answers"][0]["source"], source)
			code, output, errors = self.command("resume", state["id"], "--confirm-answers", card["id"], "--source", source)
			self.assertEqual((code, errors), (0, ""))
			self.assertEqual(json.loads(output)["cards"][-1]["confirmed_source"], source)
			self.assertEqual(len(state["calls"]), 1)
		with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
			cli.main(["meeting", "resume", state["id"], "--source", "unknown"])

	def test_safe_error_and_legacy_dispatch(self):
		code, output, errors = self.command("status", "../invalid")
		self.assertEqual((code, output), (2, ""))
		self.assertIn("회의 처리 실패", errors)
		for command, name, arguments in (("run", "cmd_run", ["file"]), ("resume", "cmd_resume", ["id"]),
				("apply", "cmd_apply", ["id"])):
			with mock.patch.object(cli, name, return_value=17) as handler:
				self.assertEqual(cli.main([command, *arguments]), 17)
				handler.assert_called_once()

	def test_cli_error_does_not_expose_input_or_follow_a_link(self):
		path = self.root / "unsafe.txt"
		path.write_text("synthetic-corp 비공개 합성 안건")
		code, output, errors = self.command("analyze", "--target", "ai-hub", "--text-file", str(path))
		self.assertEqual((code, output), (2, ""))
		self.assertNotIn("synthetic-corp", errors)
		self.assertNotIn(str(self.root), errors)
		alias = path.with_name("linked.txt")
		alias.symlink_to(path)
		self.assertEqual(self.command("analyze", "--target", "ai-hub", "--text-file", str(alias))[0], 2)
		self.assertEqual(len(self.ai.calls), 0)
