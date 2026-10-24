"""PM 대화는 합성 설정·임시 저장소·AI 대역만 사용한다."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import chat, chat_prompts, chat_record, targets
from orchestrator.providers import ChatReply, ProviderError
from vaultfilter.public import Rules


def question(identity="scope"):
	return {"id": identity, "text": "어느 범위로 진행할까요?", "options": [
		{"id": "a", "label": "작게", "difference": "한 화면부터 점검", "reply": "A안으로 가"},
		{"id": "b", "label": "넓게", "difference": "전체 화면을 점검", "reply": "B안으로 가"}],
		"recommended": "a", "reason": "빠르게 확인할 수 있음", "allow_delegate": True}


def answer(kind="choice", value="a"):
	return {"question_id": "scope", "kind": kind, "value": value}


def response(**changes):
	return {"reply": "개인 화면의 점검 범위를 정리했습니다", "questions": [], "dev_draft": "", "needs_user": "", **changes}


def context(prompt):
	return json.loads(prompt.split("대화 자료(JSON):\n", 1)[1])


class FakeAI:
	def __init__(self):
		self.calls, self.replies = [], []

	def __call__(self, plan, **kwargs):
		self.calls.append((plan, kwargs))
		value = self.replies.pop(0) if self.replies else response()
		if isinstance(value, Exception):
			raise value
		if isinstance(value, ChatReply):
			return value
		return ChatReply(copy.deepcopy(value), .25, {"usage_only": "기록하지 않을 사용량"}, "thread-01")


class ChatCase(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.hub, self.repo = self.root / "hub", self.root / "repo"
		(self.hub / "redaction").mkdir(parents=True)
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "company01"},
			{"value": "xyz", "kind": "서비스명", "token": "service01"}]}))
		self.repo.mkdir()
		subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
		(self.repo / "AGENTS.md").write_text("개인 프로젝트. 읽기 전용으로 점검한다.")
		(self.repo / "RULES.md").write_text("원천과 Git은 변경하지 않는다.")
		self.plan_text = '[[plans]]\nid="p01"\ntool="codex"\nowner="personal"\nallow=["personal"]\n'
		self.write_plans()
		self.ai = FakeAI()
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch("vaultfilter.public._allowed", return_value=set()), mock.patch.object(targets, "REPO", self.repo),
				mock.patch("orchestrator.providers.call", side_effect=AssertionError("기존 호출 금지")),
				mock.patch("subprocess.run", side_effect=AssertionError("실제 도구 실행 금지")),
				mock.patch("socket.create_connection", side_effect=AssertionError("네트워크 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		patch = mock.patch("orchestrator.providers.chat_call", side_effect=self.ai)
		self.mock_call = patch.start()
		self.addCleanup(patch.stop)
		self.engine = chat.Engine()

	def write_plans(self, text=None):
		(self.hub / "plans.toml").write_text(self.plan_text if text is None else text)

	def new(self):
		return self.engine.new("ai-hub", "p01")

	def pending(self):
		state = self.new()
		self.ai.replies = [response(questions=[question()], needs_user="진행 범위를 선택해야 합니다")]
		return self.engine.send(state["id"], "화면을 점검하고 싶습니다")


class ChatFlowTest(ChatCase):
	def test_new_send_questions_answer_confirm_send_and_draft(self):
		state = self.pending()
		self.assertEqual((state["status"], state["calls"], state["session"]), ("idle", 1, "thread-01"))
		card = state["cards"][-1]
		with self.assertRaises(chat.ChatError):
			self.engine.send(state["id"], "그대로 진행")
		state = self.engine.answer(state["id"], card["id"], [answer()], source="office")
		self.assertEqual(len(self.ai.calls), 1)
		self.assertEqual(state["cards"][-1]["status"], "answered")
		self.assertEqual(state["cards"][-1]["answers"][0]["source"], "office")
		with self.assertRaises(chat.ChatError):
			self.engine.send(state["id"], "아직 확인하지 않음")
		self.ai.replies = [response(dev_draft="개인 화면의 오류 표시를 점검하고 테스트를 추가해 주세요")]
		state = self.engine.confirm(state["id"], card["id"], source="telegram")
		self.assertEqual(state["cards"][-1]["status"], "confirmed")
		self.assertEqual(state["cards"][-1]["confirmed_source"], "telegram")
		self.assertEqual(state["turns"][-2]["kind"], "answer_summary")
		self.assertEqual(state["turns"][-2]["source"], "telegram")
		self.assertIn("A안으로 가", state["turns"][-2]["text"])
		self.assertTrue(state["dev_draft"])
		self.assertEqual(self.ai.calls[-1][1]["prompt"], state["turns"][-2]["text"])
		state = self.engine.send(state["id"], "위험도도 설명해 주세요", source="cli")
		self.assertEqual((len(state["turns"]), state["calls"], state["seconds"]), (6, 3, .75))
		self.assertEqual(self.ai.calls[0][1]["cwd"], self.repo)
		for _, call in self.ai.calls:
			self.assertEqual(call["timeout"], 600)
			self.assertNotIn("writer", call)

	def test_new_instructions_then_only_user_text_and_six_turn_fallback(self):
		state = self.new()
		self.assertEqual(self.ai.calls, [])
		for text in ("첫 번째 글", "두 번째 글", "세 번째 글", "네 번째 글"):
			state = self.engine.send(state["id"], text)
		first = self.ai.calls[0][1]
		self.assertIsNone(first["session"])
		self.assertIn(chat_prompts.INSTRUCTION, first["prompt"])
		self.assertIn("AGENTS.md", first["prompt"])
		self.assertIn("RULES.md", first["prompt"])
		self.assertEqual(self.ai.calls[1][1]["prompt"], "두 번째 글")
		with self.engine.store.locked(state["id"]) as record:
			state["session"] = None
			record.save(state)
		before = state["turns"][-6:]
		state = self.engine.send(state["id"], "다섯 번째 글")
		data = context(self.ai.calls[-1][1]["prompt"])
		self.assertEqual(data["previous_turns"], [{k: t[k] for k in ("role", "text", "kind")} for t in before])
		self.assertNotIn("첫 번째 글", self.ai.calls[-1][1]["prompt"])
		self.assertEqual(data["user_text"], "다섯 번째 글")

	def test_failed_new_or_resume_retries_once_with_context_and_safe_reason(self):
		for existing in (False, True):
			with self.subTest(existing=existing):
				state = self.new()
				if existing:
					state = self.engine.send(state["id"], "앞선 질문")
				before = state["turns"][-6:]
				self.ai.replies = [ProviderError("exit", "유출되면 안 되는 외부 예외"), response()]
				start = len(self.ai.calls)
				state = self.engine.send(state["id"], "이어서 묻습니다")
				calls = self.ai.calls[start:]
				self.assertEqual(len(calls), 2)
				self.assertEqual(calls[0][1]["session"], "thread-01" if existing else None)
				self.assertIsNone(calls[1][1]["session"])
				data = context(calls[1][1]["prompt"])
				self.assertEqual(len(data["previous_turns"]), len(before))
				self.assertIn("실패", data["retry_reason"])
				self.assertNotIn("유출되면", calls[1][1]["prompt"])
				self.assertEqual(state["failure"], "")
				self.assertEqual(sum(t["text"] == "이어서 묻습니다" for t in state["turns"]), 1)

	def test_validation_retry_omits_rejected_raw_data_and_second_failure_is_recorded(self):
		state = self.new()
		self.ai.replies = [response(reply="acme 자료"), response(dev_draft="오류 표시를 점검해 주세요")]
		state = self.engine.send(state["id"], "작업을 부탁합니다")
		self.assertEqual((state["calls"], state["failure"]), (2, ""))
		self.assertEqual(state["dev_draft"], "오류 표시를 점검해 주세요")
		self.assertIn("retry_reason", context(self.ai.calls[-1][1]["prompt"]))
		self.assertNotIn("acme", self.ai.calls[-1][1]["prompt"])
		self.ai.replies = [response(extra="원응답의 추가 필드"), response(reply="xyz 자료")]
		state = self.engine.send(state["id"], "추가로 설명해 주세요")
		self.assertEqual((state["status"], state["calls"]), ("idle", 4))
		self.assertEqual(state["failure"], chat.REJECTED)
		self.assertIsNone(state["session"])
		self.assertEqual([t["role"] for t in state["turns"]], ["user", "assistant", "user"])
		self.assertNotIn("xyz", json.dumps(state, ensure_ascii=False))
		self.assertNotIn("원응답", json.dumps(state, ensure_ascii=False))

	def test_input_rejection_costs_zero_calls_and_saves_nothing(self):
		state = self.new()
		for text in ("", " ", "글" * 2001, "acme 자료", "xyz 서비스", "ghp_" + "a" * 36,
				"password=" + "s" * 24, "p01" + "@" + "example.invalid", "/" + "home/p01/notes", "~" + "/notes", "잘못\x00된 글", "잘못\ud800된 글"):
			with self.subTest(text=text[:10]), self.assertRaises((ValueError, UnicodeError)):
				self.engine.send(state["id"], text)
			self.assertEqual(self.engine.store.read(state["id"]), state)
		self.mock_call.assert_not_called()

	def test_answer_validation_is_atomic_and_never_implies_consent(self):
		state = self.pending()
		card = state["cards"][-1]
		for replies in ([], [answer("text", "네")], [answer("text", "acme 자료")],
				[answer("text", "~" + "/notes")], [answer(value="missing")], [answer(), answer()]):
			with self.assertRaises(chat.ChatError):
				self.engine.answer(state["id"], card["id"], replies)
			self.assertEqual(self.engine.store.read(state["id"]), state)
		with self.assertRaises(chat.ChatError):
			self.engine.confirm(state["id"], card["id"])
		state = self.engine.answer(state["id"], card["id"], [answer("delegate", "알아서")])
		self.assertIn("명시 위임", state["cards"][-1]["summary"])
		self.assertEqual(len(self.ai.calls), 1)

	def test_fifty_user_turns_and_closed_chats_refuse_more_calls(self):
		state = self.new()
		for number in range(50):
			state = self.engine.send(state["id"], "합성 질문 " + str(number))
		self.assertEqual((state["calls"], len(state["turns"])), (50, 100))
		with self.assertRaisesRegex(chat.ChatError, "50"):
			self.engine.send(state["id"], "추가 질문")
		self.assertEqual(len(self.ai.calls), 50)
		state = self.engine.close(state["id"])
		self.assertEqual(state["status"], "closed")
		self.assertEqual(self.engine.status(state["id"]), state)
		self.assertEqual(self.engine.close(state["id"]), state)
		with self.assertRaisesRegex(chat.ChatError, "닫힌"):
			self.engine.send(state["id"], "다시 질문")

	def test_private_allowed_plans_only_and_removal_rechecked_before_send(self):
		rows = [self.plan_text]
		for identity, tool, owner, allow in (("p02", "claude", "personal", "personal"), ("p03", "agy", "personal", "personal"),
				("p04", "codex", "company", "personal"), ("p05", "claude", "personal", "study"), ("p06", "ollama", "personal", "personal")):
			rows.append(f'[[plans]]\nid="{identity}"\ntool="{tool}"\nowner="{owner}"\nallow=["{allow}"]\nmodel="model-01"\n')
		self.write_plans("\n".join(rows))
		self.assertEqual(self.engine.plans(), [{"id": "p01", "tool": "codex"}, {"id": "p02", "tool": "claude"}])
		state = self.new()
		for plan in ("p04", "p05", "p06", "missing"):
			with self.assertRaises(chat.ChatError):
				self.engine.new("ai-hub", plan)
		self.write_plans("")
		self.assertEqual(self.engine.plans(), [])
		self.assertEqual(self.engine.list()[0]["id"], state["id"])
		with self.assertRaises(chat.ChatError):
			self.engine.send(state["id"], "요금제가 사라진 뒤 전송")
		self.assertEqual(self.engine.store.read(state["id"]), state)
		self.mock_call.assert_not_called()

	def test_target_registry_and_selected_plan_control_cwd_and_tool(self):
		other = self.root / "other"
		other.mkdir()
		(self.hub / "repos.toml").write_text('[[repo]]\nid="p01"\npath=' + json.dumps(str(other)) + '\ntest_command="python3 -m unittest"\n')
		with mock.patch("orchestrator.checks.git", return_value=str(other)):
			state = self.engine.new("p01", "p01")
			state = self.engine.send(state["id"], "개인 저장소를 점검해 주세요")
			with self.assertRaises(ValueError):
				self.engine.new("missing", "p01")
		self.assertEqual((state["target"], self.ai.calls[-1][1]["cwd"]), ("p01", other))
		self.assertNotIn(str(other), self.ai.calls[-1][1]["prompt"])
		self.write_plans(self.plan_text.replace('tool="codex"', 'tool="agy"'))
		with self.assertRaises(chat.ChatError):
			self.engine.send(state["id"], "도구 변경 후 전송")
		self.assertEqual(len(self.ai.calls), 1)

	def test_response_schema_security_and_nested_question_checks(self):
		rules = Rules()
		self.assertEqual(chat_prompts.validate_reply(response(), rules), response())
		for change in ({"reply": ""}, {"reply": "글" * 6001}, {"reply": "\x01제어"}, {"questions": [question()] * 4},
				{"dev_draft": "글" * 2001}, {"needs_user": "글" * 201}, {"needs_user": "~" + "/notes"},
				{"dev_draft": "p01" + "@" + "example.invalid"}, {"questions": [{**question(), "allow_delegate": 1}]},
				{"questions": [{**question(), "reason": "acme 자료"}]}, {"questions": [{**question(), "recommended": "missing"}]},
				{"questions": [{**question(), "options": [question()["options"][0]] * 2}]}):
			with self.subTest(change=list(change)), self.assertRaises(ValueError):
				chat_prompts.validate_reply(response(**change), rules)
		for schema in (chat_prompts.SCHEMA, chat_prompts.SCHEMA["properties"]["questions"]["items"],
				chat_prompts.SCHEMA["properties"]["questions"]["items"]["properties"]["options"]["items"]):
			self.assertIs(schema["additionalProperties"], False)

	def test_only_validated_fields_are_stored_without_usage_raw_response_or_session_paths(self):
		state = self.new()
		self.ai.replies = [ChatReply(response(), .5, {"raw": "사용량 전용 원문"}, str(self.root / "session.json"))]
		state = self.engine.send(state["id"], "정리해 주세요")
		self.assertIsNone(state["session"])
		stored = (self.hub / "chats" / state["id"] / "chat.json").read_text()
		for text in ("사용량 전용 원문", "session.json", str(self.root), '"usage"', '"raw"', "structured_output"):
			self.assertNotIn(text, stored)
		self.assertEqual(set(state), set(chat_record.FIELDS))
