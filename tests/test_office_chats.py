"""합성 홈·가짜 명령으로 대화 쓰기의 계약과 원문 없는 기록을 검사한다."""

import copy
import fcntl
import json
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

from office import actions, chats
from office.common import action_id
from orchestrator import chat_record, question_cards
from test_orchestrator_draft import DraftCase

CHAT = "c-" + "a" * 32
CARD = "q-" + "b" * 32


def chat_state(status="idle", identity=CHAT, card=None):
	state = chat_record.new("ai-hub", "p01", "codex")
	state.update(id=identity, status=status, created="2026-10-25T10:00:00+09:00", session="hidden-session-01")
	if card:
		state["cards"] = [{"id": CARD, "turn": 1, "status": card, "source": "pm", "answers": [], "summary": "",
			"questions": [{"id": "scope", "text": "합성 질문 원문", "reason": "합성 추천 이유", "recommended": "a", "allow_delegate": True,
				"options": [{"id": "a", "label": "A안", "difference": "합성 작은 범위", "reply": "A안으로 가"},
					{"id": "b", "label": "B안", "difference": "합성 넓은 범위", "reply": "B안으로 가"}]}]}]
		if card != "waiting":
			state["cards"][0].update(summary="합성 카드 요약 원문", answers=[
				{"question_id": "scope", "kind": "text", "value": "합성 구체적인 답", "delegated": False, "source": "office"}])
	return state


def write_chat(root, state):
	folder = root / "chats" / state["id"]
	folder.mkdir(mode=0o700, parents=True, exist_ok=True)
	path = folder / "chat.json"
	path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
	path.chmod(0o600)
	return path


class ChatCase(DraftCase):
	def snapshot(self):
		return {str(p.relative_to(self.hub)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
			for p in self.hub.rglob("*") if p.is_file() and not p.is_symlink()}

	def setUp(self):
		super().setUp()
		self.new = {"action": "chat_new", "id": "ai-hub", "plan": "p01"}
		self.send = {"action": "chat_send", "id": CHAT, "text": "합성 사용자 전송 원문"}
		self.answer = {"action": "chat_answer", "id": CHAT, "card": CARD,
			"answers": [{"question_id": "scope", "kind": "text", "value": "합성 사용자 답변 원문"}]}
		self.confirm = {"action": "chat_confirm", "id": CHAT, "card": CARD}
		self.close = {"action": "chat_close", "id": CHAT}
		for patch in (mock.patch.object(chats.subprocess, "run", side_effect=AssertionError("실제 명령 금지")),
				mock.patch.object(chats.subprocess, "Popen", side_effect=AssertionError("실제 명령 금지")),
				mock.patch.object(chat_record.Store, "locked", side_effect=AssertionError("서버의 엔진 잠금 금지")),
				mock.patch("orchestrator.providers.chat_call", side_effect=AssertionError("실제 AI 금지")),
				mock.patch("socket.create_connection", side_effect=AssertionError("실제 연결 금지"))):
			patch.start()
			self.addCleanup(patch.stop)


class OfficeChatsTest(ChatCase):
	def test_exact_keys_shapes_ids_and_lengths_refuse_without_commands(self):
		bad = [None, [], {}, {"action": "chat_execute", "id": CHAT}]
		for payload in (self.new, self.send, self.answer, self.confirm, self.close):
			bad += [{**payload, "extra": "합성"}]
			bad += [{k: v for k, v in payload.items() if k != key} for key in payload]
			bad += [{**payload, "id": value} for value in (None, [], {}, 1, "", "../other", CHAT.upper(), CHAT + "/")]
		bad += [{**self.new, "id": value} for value in ("missing", "Wrong", "a" * 32)]
		bad += [{**self.new, "plan": value} for value in (None, [], "", "a" * 65, "p.01", "../p01", "p 01")]
		bad += [{**self.send, "text": value} for value in (None, [], "", " ", "a" * 2001, "a\x00b", "a\x1bb", "\ud800")]
		for payload in (self.answer, self.confirm):
			bad += [{**payload, "card": value} for value in (None, [], CHAT, CARD.upper(), CARD + "/", "../other")]
		bad += [{**self.answer, "answers": value} for value in (None, {}, [], [{}] * 4,
			[{"question_id": "scope", "kind": "text", "value": "a" * 2001}],
			[{"question_id": "../other", "kind": "text", "value": "합성"}],
			[{"question_id": "scope", "kind": "unknown", "value": "합성"}],
			[{"question_id": "scope", "kind": "text", "value": "합성", "source": "telegram"}])]
		with mock.patch.object(chats.subprocess, "run") as run, mock.patch.object(chats.subprocess, "Popen") as popen:
			for payload in bad:
				with self.subTest(payload=repr(payload)[:120]):
					self.assertEqual(actions.apply(payload)[0], 400)
					with self.assertRaises(ValueError):
						chats.validate(payload)
			run.assert_not_called()
			popen.assert_not_called()
		self.assertFalse((self.hub / "logs").exists())
		for action in chats.ACTIONS[1:]:
			self.assertEqual(action_id(action, CHAT), CHAT)
		self.assertEqual(action_id("chat_new", "ai-hub"), "ai-hub")
		self.assertEqual(action_id("chat_card", CARD), CARD)

	def test_sensitive_send_and_answers_never_reach_engine(self):
		write_chat(self.hub, chat_state(card="waiting"))
		values = ("acme 안건", "xyz 안건", "토큰 " + "ghp_" + "a" * 36,
			"연락 " + "p01" + "@" + "acme.invalid", "/" + "home/p02/private", "/" + "Users/p02/private",
			"~" + "/private", "C:" + "\\Users\\p02\\private")
		with mock.patch.object(chats.subprocess, "run") as run, mock.patch.object(chats.subprocess, "Popen") as popen:
			for text in values:
				for payload in ({**self.send, "text": text}, {**self.answer,
						"answers": [{"question_id": "scope", "kind": "text", "value": text}]}):
					status, value = actions.apply(payload)
					self.assertEqual(status, 400, value)
					self.assertIn("회사·서비스 이름", value["message"])
					self.assertNotIn(text, json.dumps(value, ensure_ascii=False))
			run.assert_not_called()
			popen.assert_not_called()
		self.assertFalse((self.hub / "run").exists())

	def test_new_is_synchronous_with_only_a_public_summary_and_safe_audit(self):
		state = chat_state()
		state.update(dev_draft="합성 숨길 초안", needs_user="acme 확인")
		with mock.patch.object(chats.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(state), "합성 숨길 오류")) as run:
			status, value = actions.apply(self.new)
		self.assertEqual(status, 200, value)
		self.assertEqual(set(value["draft"]), {"id", "target", "plan", "tool", "status", "needs_user", "calls", "user_turns"})
		self.assertEqual(value["draft"]["id"], CHAT)
		self.assertEqual(value["draft"]["calls"], 0)
		self.assertNotIn("acme", value["draft"]["needs_user"])
		self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "orchestrator", "chat", "new", "--target", "ai-hub", "--plan", "p01"])
		options = run.call_args.kwargs
		self.assertIs(options["shell"], False)
		self.assertEqual(options["timeout"], 60)
		self.assertEqual(options["stdout"], subprocess.PIPE)
		self.assertEqual(options["stderr"], subprocess.PIPE)
		self.assertEqual(options["cwd"], actions.REPO)
		self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
		self.assertEqual(options["env"]["PYTHONPATH"], str(actions.REPO))
		audit = self.hub / "logs/office-actions.jsonl"
		self.assertEqual(stat.S_IMODE(audit.stat().st_mode), 0o600)
		row = json.loads(audit.read_text())
		self.assertEqual(set(row), {"at", "action", "id", "exit_code", "seconds", "ok", "message"})
		self.assertEqual((row["action"], row["exit_code"]), ("chat_new", 0))
		for raw in (state["session"], state["dev_draft"], "합성 숨길 오류"):
			self.assertNotIn(raw, json.dumps(value, ensure_ascii=False) + audit.read_text())

	def test_answer_uses_private_json_and_returns_only_masked_summary_without_server_writes(self):
		original = chat_state(card="waiting")
		path = write_chat(self.hub, original)
		before, inputs = path.read_bytes(), []
		def run(argv, **options):
			file = Path(argv[-3])
			inputs.append(file)
			self.assertEqual(argv, [sys.executable, "-m", "orchestrator", "chat", "answer", CHAT,
				"--card", CARD, "--answers-file", str(file), "--source", "office"])
			self.assertEqual(file.parent, self.hub / "run/office-chats")
			self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
			self.assertEqual(json.loads(file.read_text()), self.answer["answers"])
			self.assertIs(options["shell"], False)
			self.assertEqual(options["timeout"], 60)
			state = copy.deepcopy(original)
			question_cards.accept(state["cards"][0], CARD, self.answer["answers"], "office", chats.Rules())
			state["cards"][0]["summary"] = "acme /tmp/private " + "p01" + "@" + "acme.invalid"
			return subprocess.CompletedProcess(argv, 0, json.dumps(state), "")
		with mock.patch.object(chats.subprocess, "run", side_effect=run):
			status, value = actions.apply(self.answer)
		self.assertEqual(status, 200, value)
		self.assertEqual(set(value["draft"]), {"id", "card", "summary"})
		self.assertEqual((value["draft"]["id"], value["draft"]["card"]), (CHAT, CARD))
		self.assertNotRegex(value["draft"]["summary"], "acme|private|@")
		self.assertFalse(inputs[0].exists())
		self.assertEqual(path.read_bytes(), before)
		for log in (self.hub / "logs").rglob("*"):
			if log.is_file():
				self.assertNotIn(self.answer["answers"][0]["value"], log.read_text())
				self.assertNotIn(original["cards"][0]["questions"][0]["text"], log.read_text())

	def test_answer_rechecks_choice_count_delegate_ids_and_summary_length_before_command(self):
		state = chat_state(card="waiting")
		state["cards"][0]["questions"][0]["allow_delegate"] = False
		write_chat(self.hub, state)
		answers = ([{"question_id": "other", "kind": "text", "value": "합성 답"}], self.answer["answers"] * 2,
			[{"question_id": "scope", "kind": "choice", "value": "missing"}],
			[{"question_id": "scope", "kind": "delegate", "value": "알아서"}],
			[{"question_id": "scope", "kind": "text", "value": "네"}],
			[{"question_id": "scope", "kind": "text", "value": "가" * 2000}])
		with mock.patch.object(chats.subprocess, "run") as run:
			for value in answers:
				self.assertEqual(actions.apply({**self.answer, "answers": value})[0], 400)
			run.assert_not_called()

	def test_close_command_and_fixed_errors_remove_temporary_files(self):
		write_chat(self.hub, chat_state())
		with mock.patch.object(chats.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(chat_state("closed")), "")) as run:
			self.assertEqual(actions.apply(self.close)[0], 200)
		self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "orchestrator", "chat", "close", CHAT])
		write_chat(self.hub, chat_state(card="waiting"))
		for failure, expected in ((subprocess.CompletedProcess([], 2, "합성 숨길 원문", "합성 숨길 원문"), 500),
				(subprocess.TimeoutExpired([], 60, output="합성 숨길 원문"), 504), (OSError("합성 숨길 원문"), 409),
				(subprocess.CompletedProcess([], 0, "{", ""), 409)):
			inputs = []
			def run(argv, **options):
				inputs.append(Path(argv[-3]))
				if isinstance(failure, Exception):
					raise failure
				return failure
			with mock.patch.object(chats.subprocess, "run", side_effect=run):
				status, value = actions.apply(self.answer)
			self.assertEqual(status, expected, value)
			self.assertFalse(inputs[0].exists())
			self.assertNotIn("합성 숨길 원문", json.dumps(value, ensure_ascii=False))
		self.assertNotIn("합성 숨길 원문", (self.hub / "logs/office-actions.jsonl").read_text())

	def test_choice_reply_and_explicit_delegate_use_engine_answer_rules(self):
		for kind, value in (("choice", "a"), ("choice", "B안으로 가"), ("delegate", "알아서")):
			state = chat_state(card="waiting")
			write_chat(self.hub, state)
			payload = {**self.answer, "answers": [{"question_id": "scope", "kind": kind, "value": value}]}
			question_cards.accept(state["cards"][0], CARD, payload["answers"], "office", chats.Rules())
			with mock.patch.object(chats.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(state), "")):
				code, result = actions.apply(payload)
			self.assertEqual(code, 200, result)
			self.assertEqual(result["draft"]["summary"], state["cards"][0]["summary"])

	def test_answer_missing_from_successful_engine_state_is_a_fixed_error(self):
		write_chat(self.hub, chat_state(card="waiting"))
		with mock.patch.object(chats.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(chat_state()), "")):
			code, result = actions.apply(self.answer)
		self.assertEqual(code, 409, result)
		self.assertFalse(result["ok"])

	def test_answer_changed_between_recheck_and_command_returns_conflict_without_raw_output(self):
		state = chat_state(card="waiting")
		write_chat(self.hub, state)
		def race(*args, **kwargs):
			changed = chat_state(card="answered")
			changed["cards"][0]["answers"][0]["source"] = "telegram"
			write_chat(self.hub, changed)
			return subprocess.CompletedProcess([], 2, "합성 숨길 답", "합성 숨길 오류")
		with mock.patch.object(chats.subprocess, "run", side_effect=race):
			code, result = actions.apply(self.answer)
		self.assertEqual((code, result["message"]), (409, "이미 답했습니다(텔레그램에서)"))
		self.assertNotIn("합성 숨길", json.dumps(result, ensure_ascii=False))

	def test_only_five_chat_actions_and_no_development_execution_or_extra_options(self):
		self.assertEqual(chats.ACTIONS, ('chat_new', 'chat_send', 'chat_answer', 'chat_confirm', 'chat_close'))
		for action in ("apply", "run", "push", "delete", "restart", "command", "dev_draft"):
			self.assertEqual(actions.apply({"action": "chat_" + action, "id": CHAT})[0], 400)
		for payload in (self.new, self.send, self.answer, self.confirm, self.close):
			for key in ("command", "shell", "dev_draft", "target", "source"):
				self.assertEqual(actions.apply({**payload, key: "합성"})[0], 400)

	def test_global_lock_and_unsafe_audit_prevent_commands(self):
		with actions.private_file(self.hub, "run", "office-action.lock") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			for payload in (self.new, self.send, self.answer, self.confirm, self.close):
				self.assertEqual(actions.apply(payload)[0], 409)
		(self.hub / "logs").mkdir(exist_ok=True)
		(self.hub / "logs/office-actions.jsonl").symlink_to(self.root / "outside")
		self.assertEqual(actions.apply(self.new)[0], 409)
		self.assertFalse((self.root / "outside").exists())
