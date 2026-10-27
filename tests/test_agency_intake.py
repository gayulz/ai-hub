"""접수 되묻기와 CLI를 가짜 AI와 임시 홈에서 검사한다."""

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from agency import ai, intake, intake_cli, project, skills
from agency.__main__ import main
from agency.departments import load_departments
from collector.backfill_common import private_write
from collector.config import ConfigError

SUMMARY = "합성 자료를 정리합니다.\n로컬 결과를 전달합니다.\n외부 실행은 제외합니다."
REQUIRED = "어디에 납품하나요?"


class IntakeTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		patch = mock.patch.object(skills, "REPO", self.root / "repo")
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "xyz"
tool = "codex"
owner = "personal"
allow = ["personal"]
''', encoding="utf-8")
		(self.hub / "departments.toml").write_text('''[[department]]
id = "xyz"
name = "합성 부서"
scope = "합성 자료 정리"
repos = ["ai-hub"]
allow_plans = ["xyz"]
playbook = "xyz"
required_questions = ["어디에 납품하나요?"]
budget = { calls = 3, minutes = 2 }
''', encoding="utf-8")
		folder = self.hub / "skills" / "xyz"
		folder.mkdir(parents=True)
		(folder / "SKILL.md").write_text("---\nname: xyz\ndescription: 합성 플레이북\n---\n"
			"플레이북 본문: 결과는 로컬 자료로 남긴다.", encoding="utf-8")
		self.department = load_departments()["xyz"]
		self.spec = {"goal": "합성 자료 정리", "target": "로컬 자료", "out_of_scope": "외부 실행",
			"done_criteria": ["로컬 결과 전달"], "budget": {"calls": 4, "minutes": 5}}
		self.identity = project.new(self.department, "합성 요청", "로컬 자료를 정리하고 외부 실행은 제외해 주세요.")["id"]
		patch = mock.patch.object(ai, "ask")
		self.ask = patch.start()
		self.addCleanup(patch.stop)
		self.reply(questions=(REQUIRED,))

	def reply(self, spec=None, questions=(), summary=SUMMARY, seconds=1.25):
		self.ask.return_value = ai.Answer({"spec": copy.deepcopy(spec or {}), "questions": list(questions),
			"summary": summary}, "xyz", seconds)

	def ready(self):
		project.update_spec(self.identity, **self.spec)
		project.add_answer(self.identity, REQUIRED, "로컬 결과")

	def question_path(self):
		return self.hub / "projects" / self.identity / "questions.json"

	def stored_questions(self):
		return json.loads(self.question_path().read_text(encoding="utf-8"))

	def cli(self, *arguments, stdin="", entry=main):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
			try:
				code = entry(list(arguments))
			except SystemExit as exc:
				code = exc.code
		return code, out.getvalue(), err.getvalue()

	def test_prompt_contains_request_spec_answers_department_playbook_and_limits(self):
		project.update_spec(self.identity, goal="기존 목표")
		project.add_answer(self.identity, "기존 질문", "이전 답변 원문")
		intake.step(self.identity)
		self.ask.assert_called_once()
		args, kwargs = self.ask.call_args
		self.assertEqual(args[0], self.department)
		self.assertIs(args[2], intake.SCHEMA)
		self.assertEqual(kwargs, {"repo": None})
		for text in ("로컬 자료를 정리하고", "기존 목표", "이전 답변 원문", REQUIRED, "합성 부서",
				"합성 자료 정리", "플레이북 본문: 결과는 로컬 자료로 남긴다.", "1~1000", "1~20",
				"1~300", "1~200", "1~1440", "추측", "최대 3", "원문 그대로", "다시 묻지", "3~6줄"):
			with self.subTest(text=text):
				self.assertIn(text, args[1])

	def test_only_valid_spec_fields_are_merged_and_usage_is_counted(self):
		project.update_spec(self.identity, target="기존 대상")
		self.reply({"goal": "근거 있는 목표", "target": "가" * 1001, "out_of_scope": "",
			"done_criteria": ["근거 있는 기준"], "budget": {"calls": 8, "minutes": None}, "extra": "제외"})
		result = intake.step(self.identity)
		row = project.load(self.identity)
		self.assertEqual(row["spec"], {"goal": "근거 있는 목표", "target": "기존 대상", "out_of_scope": "",
			"done_criteria": ["근거 있는 기준"], "budget": {"calls": 8, "minutes": 2}})
		self.assertEqual(row["used"], {"calls": 1, "seconds": 1.25})
		self.assertEqual(result["plan"], "xyz")
		self.assertEqual(result["missing"], ["out_of_scope", REQUIRED])

	def test_invalid_spec_values_never_replace_existing_values(self):
		self.ready()
		cases = [{"goal": value} for value in (None, False, [], "", " ", "가" * 1001, "가\x00")]
		cases += [{key: "가" * 1001} for key in ("target", "out_of_scope")]
		cases += [{"done_criteria": value} for value in ("기준", [], [""], ["가" * 301], ["가"] * 21, [False])]
		cases += [{"budget": value} for value in (None, [], "", {"calls": True, "minutes": 1.5},
			{"calls": 0, "minutes": 1441}, {"calls": 201, "minutes": 0})]
		for value in cases:
			with self.subTest(value=value):
				self.reply(value, summary="")
				intake.step(self.identity)
				self.assertEqual(project.load(self.identity)["spec"], self.spec)

	def test_invalid_top_level_response_is_ignored_but_usage_is_saved(self):
		before = project.load(self.identity)["spec"]
		for data in (None, [], "형식 오류", {"spec": [], "questions": "질문", "summary": []}):
			with self.subTest(data=data):
				self.ask.return_value = ai.Answer(data, "xyz", 0.5)
				intake.step(self.identity)
				self.assertEqual(project.load(self.identity)["spec"], before)
		self.assertEqual(project.load(self.identity)["used"], {"calls": 4, "seconds": 2.0})

	def test_questions_are_limited_deduplicated_and_never_repeat_answered_questions(self):
		self.ready()
		self.reply(questions=("첫 질문?", "첫 질문?", "둘째 질문?", "셋째 질문?", "넷째 질문?"))
		first = intake.step(self.identity)["questions"]
		self.assertEqual([item["question"] for item in first], ["첫 질문?", "둘째 질문?", "셋째 질문?"])
		self.assertEqual([item["no"] for item in first], [1, 2, 3])
		self.assertEqual(intake.step(self.identity)["questions"], first)
		intake.answer(self.identity, 1, "첫 답")
		self.reply(questions=("첫 질문?", "둘째 질문?", "넷째 질문?"))
		result = intake.step(self.identity)
		self.assertEqual([item["no"] for item in result["questions"]], [2, 3, 4])
		self.assertNotIn("첫 질문?", [item["question"] for item in result["questions"]])
		self.assertEqual(self.stored_questions()["next_no"], 5)

	def test_invalid_question_strings_are_dropped(self):
		self.ready()
		self.reply(questions=(None, 4, "", " ", "가" * 301, "줄\n바꿈", "탭\t문자", "널\x00",
			"삭제\x7f", "제어\x85", "정상 질문?"))
		result = intake.step(self.identity)
		self.assertEqual([item["question"] for item in result["questions"]], ["정상 질문?"])

	def test_unanswered_required_questions_use_the_original_wording(self):
		result = intake.step(self.identity)
		self.assertEqual(result["questions"][0]["question"], REQUIRED)
		intake.answer(self.identity, result["questions"][0]["no"], "로컬 결과")
		self.reply(questions=(REQUIRED,))
		self.assertEqual(intake.step(self.identity)["questions"], [])

	def test_required_questions_open_even_when_ai_omits_them(self):
		self.reply(questions=("다른 질문?",))
		result = intake.step(self.identity)
		self.assertEqual([item["question"] for item in result["questions"]], [REQUIRED, "다른 질문?"])
		self.reply(questions=())
		again = intake.step(self.identity)
		self.assertEqual(again["questions"], result["questions"])
		self.assertIn(REQUIRED, again["missing"])

	def test_complete_spec_opens_only_one_summary_confirmation(self):
		self.ready()
		first = intake.step(self.identity)
		self.assertEqual(first["missing"], [])
		self.assertEqual(len(first["questions"]), 1)
		self.assertEqual(first["questions"][0]["question"], intake.CONFIRM_PREFIX + SUMMARY)
		self.assertEqual(intake.step(self.identity)["questions"], first["questions"])
		self.assertEqual(self.stored_questions()["next_no"], 2)

	def test_confirmation_opens_even_when_other_questions_are_still_open(self):
		self.ready()
		self.reply(questions=("첫 질문?", "둘째 질문?", "셋째 질문?"))
		intake.step(self.identity)
		self.reply()
		result = intake.step(self.identity)
		self.assertEqual([item["no"] for item in result["questions"]], [1, 2, 3, 4])
		self.assertEqual(result["questions"][-1]["question"], intake.CONFIRM_PREFIX + SUMMARY)

	def test_summary_must_be_valid_before_confirmation_is_opened(self):
		self.ready()
		for summary in (None, [], "", "가" * 1501, "가\x00\n나\n다", "가\x1b\n나\n다", "한 줄", "줄\n" * 7):
			with self.subTest(summary=summary):
				self.reply(summary=summary)
				self.assertEqual(intake.step(self.identity)["questions"], [])

	def test_confirmation_answers_finalize_spec_and_keep_answer_history(self):
		for text in ("맞아", "맞습니다", "네", "응", "ㅇㅇ"):
			with self.subTest(text=text):
				self.identity = project.new(self.department, "합성 요청", "합성 요청")["id"]
				self.ready()
				question = intake.step(self.identity)["questions"][0]
				row = intake.answer(self.identity, question["no"], " " + text + "\n")
				self.assertEqual(row["status"], "명세 확정")
				self.assertTrue(row["spec_hash"])
				self.assertEqual(row["answers"][-1]["question"], question["question"])
				self.assertEqual(row["answers"][-1]["answer"].strip(), text)
				self.assertEqual(self.stored_questions()["open"], [])

	def test_correction_stays_in_intake_and_is_sent_to_next_step(self):
		self.ready()
		question = intake.step(self.identity)["questions"][0]
		row = intake.answer(self.identity, question["no"], "대상은 다른 로컬 자료입니다.")
		self.assertEqual((row["status"], row["spec_hash"]), ("접수 중", ""))
		self.reply({"target": "다른 로컬 자료"})
		result = intake.step(self.identity)
		self.assertIn("대상은 다른 로컬 자료입니다.", self.ask.call_args.args[1])
		self.assertEqual(project.load(self.identity)["spec"]["target"], "다른 로컬 자료")
		self.assertEqual(result["questions"][0]["no"], 2)

	def test_confirm_spec_refuses_new_unanswered_required_question(self):
		self.ready()
		question = intake.step(self.identity)["questions"][0]
		path = self.hub / "departments.toml"
		path.write_text(path.read_text().replace('["어디에 납품하나요?"]',
			'["어디에 납품하나요?", "새 필수 질문?"]'), encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "필수 질문"):
			intake.answer(self.identity, question["no"], "맞아")
		self.assertEqual(project.load(self.identity)["status"], "접수 중")
		self.assertEqual(self.stored_questions()["open"], [])

	def test_answer_rejects_missing_numbers_and_invalid_text_without_changes(self):
		question = intake.step(self.identity)["questions"][0]
		before = self.question_path().read_bytes(), project.load(self.identity)
		for no, text in ((999, "답"), (True, "답"), ("1", "답"), (0, "답"),
				(question["no"], ""), (question["no"], " "), (question["no"], "가" * 4001),
				(question["no"], None), (question["no"], "널\x00")):
			with self.subTest(no=no, text_type=type(text).__name__), self.assertRaises(ValueError):
				intake.answer(self.identity, no, text)
			self.assertEqual((self.question_path().read_bytes(), project.load(self.identity)), before)
		intake.answer(self.identity, question["no"], "가" * 4000)
		with self.assertRaises(ValueError):
			intake.answer(self.identity, question["no"], "다시 답")

	def test_step_rejects_non_intake_projects_before_calling_ai(self):
		for status in ("명세 확정", "막힘", "취소"):
			with self.subTest(status=status):
				self.identity = project.new(self.department, "합성", "합성 요청")["id"]
				if status == "명세 확정":
					self.ready()
					project.confirm_spec(self.identity, self.department)
				else:
					project.transition(self.identity, status, reason="합성 이유")
				with self.assertRaisesRegex(ValueError, "접수 중"):
					intake.step(self.identity)
		self.ask.assert_not_called()

	def test_external_ai_forbidden_department_is_rejected(self):
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace('tool = "codex"', 'tool = "ollama"\nmodel = "xyz"'), encoding="utf-8")
		with (self.hub / "departments.toml").open("a", encoding="utf-8") as stream:
			stream.write("external_ai = false\n")
		with self.assertRaisesRegex(ValueError, "^외부 AI 금지 부서는 접수 되묻기를 쓸 수 없습니다$"):
			intake.step(self.identity)
		self.ask.assert_not_called()

	def test_missing_department_is_reported_before_ai(self):
		(self.hub / "departments.toml").write_text("", encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "부서"):
			intake.step(self.identity)
		self.ask.assert_not_called()

	def test_unreadable_playbook_is_reported_before_ai(self):
		with mock.patch.object(skills, "load", side_effect=ConfigError("스킬 파일이 없습니다")):
			with self.assertRaises(ConfigError):
				intake.step(self.identity)
		self.ask.assert_not_called()

	def test_failed_ask_blocks_project_with_classified_reason_and_reraises(self):
		error = RuntimeError("한도 소진")
		self.ask.side_effect = error
		with self.assertRaises(RuntimeError) as caught:
			intake.step(self.identity)
		self.assertIs(caught.exception, error)
		row = project.load(self.identity)
		self.assertEqual(row["status"], "막힘")
		self.assertEqual(row["blocked_from"], "접수 중")
		self.assertEqual(row["blocked_reason"], "접수 AI 호출 실패: 한도 소진")
		self.assertEqual(row["used"], {"calls": 0, "seconds": 0})

	def test_question_file_permissions_timestamps_and_safe_storage(self):
		result = intake.step(self.identity)
		self.assertEqual(stat.S_IMODE(self.question_path().stat().st_mode), 0o600)
		for item in result["questions"]:
			self.assertIsNotNone(datetime.fromisoformat(item["asked"]).tzinfo)
		self.question_path().chmod(0o644)
		intake.answer(self.identity, result["questions"][0]["no"], "로컬 결과")
		self.assertEqual(stat.S_IMODE(self.question_path().stat().st_mode), 0o600)

	def test_corrupt_or_linked_question_file_is_not_overwritten(self):
		outside = self.root / "outside.json"
		outside.write_text("합성 원본", encoding="utf-8")
		self.question_path().symlink_to(outside)
		with self.assertRaises(ValueError):
			intake.step(self.identity)
		self.assertEqual(outside.read_text(), "합성 원본")
		private_write(self.question_path(), "잘못된 JSON")
		with self.assertRaises(ValueError):
			intake.step(self.identity)
		self.assertEqual(self.question_path().read_text(), "잘못된 JSON")
		self.ask.assert_not_called()

	def test_invalid_question_record_is_rejected_without_changes(self):
		question = {"no": 1, "question": "합성 질문?", "asked": "2030-01-02T03:04:05+09:00"}
		cases = [{}, {"open": [], "next_no": True}, {"open": "오류", "next_no": 1},
			{"open": [question], "next_no": 1}, {"open": [question, question], "next_no": 2}]
		cases += [{"open": [{**question, key: value}], "next_no": 2} for key, value in
			(("no", True), ("question", "가" * 301), ("question", "줄\n바꿈"), ("asked", "잘못된 시각"))]
		for data in cases:
			with self.subTest(data=data):
				text = json.dumps(data, ensure_ascii=False)
				private_write(self.question_path(), text)
				with self.assertRaises(ValueError):
					intake.step(self.identity)
				self.assertEqual(self.question_path().read_text(), text)
		self.ask.assert_not_called()

	def test_cli_intake_answer_stdin_and_confirmation_output(self):
		code, out, err = self.cli("intake", self.identity)
		self.assertEqual((code, err), (0, ""))
		self.assertIn("[1] " + REQUIRED, out)
		code, out, err = self.cli("answer", self.identity, "1", "-", stdin="로컬 결과")
		self.assertEqual((code, out, err), (0, "", ""))
		self.reply(self.spec)
		code, out, err = self.cli("intake", self.identity)
		self.assertEqual((code, err), (0, ""))
		self.assertIn('[2] 이렇게 이해했습니다.', out)
		self.assertEqual(self.cli("answer", self.identity, "2", "맞아"), (0, "명세 확정\n", ""))

	def test_cli_waiting_message_and_argument_error_codes(self):
		with mock.patch.object(intake, "step", return_value={"questions": [], "missing": [], "plan": "xyz"}):
			self.assertEqual(self.cli("intake", self.identity), (0, "명세 확정 대기 없음\n", ""))
		for arguments in ((), ("unknown",), ("intake",), ("intake", self.identity, "extra"),
				("answer", self.identity), ("answer", self.identity, "x", "답"),
				("answer", self.identity, "1", "답", "extra")):
			with self.subTest(arguments=arguments):
				code, out, err = self.cli(*arguments, entry=intake_cli.main)
				self.assertEqual((code, out), (2, ""))
				self.assertIn("usage:", err)

	def test_cli_domain_errors_are_printed_to_stderr(self):
		for error in (ValueError("잘못된 값"), ConfigError("설정 오류"), RuntimeError("한도 소진")):
			with self.subTest(error=type(error).__name__), mock.patch.object(intake, "step", side_effect=error):
				self.assertEqual(self.cli("intake", self.identity), (1, "", f"오류: {error}\n"))
		self.assertEqual(self.cli("answer", self.identity, "999", "답")[0], 1)


if __name__ == "__main__":
	unittest.main()
