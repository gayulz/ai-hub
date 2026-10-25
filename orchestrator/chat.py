"""읽기 전용 PM 대화. 실행·반영은 기존 개발 지시와 사용자 확인 절차를 따른다.

대화 호출은 세션을 남기므로 Codex·Claude·AGY의 세션 파일이 생긴다. 수집기는
이 대화를 사람의 작업 기록으로 수집한다. 이 엔진은 수집기나 훅을 변경하지 않는다.
기록에는 검사한 필드만 두며 AI 원응답·사용량·세션 파일 경로를 남기지 않는다.
"""

from __future__ import annotations

import math
import re
import time

from collector.config import load_plans
from vaultfilter.public import Rules

from . import chat_prompts as prompts, chat_record as records, providers, question_cards as cards, targets
from .draft import safe_text, user_text

ChatError = records.ChatError
MAX_TURNS, TIMEOUT = 50, 600
REJECTED = "응답이 JSON 계약·질문·보안 검사를 통과하지 못했습니다"
PROVIDER_FAILURES = {"timeout": "대화 호출 시간이 초과됐습니다", "exit": "대화 도구 호출이 실패했습니다",
	"empty": "대화 응답이 비어 있습니다", "json": "대화 응답 JSON을 읽지 못했습니다", "missing": "대화 도구를 실행하지 못했습니다"}


def _source(value):
	if value not in records.SOURCES:
		raise ChatError("대화 출처는 office·telegram·cli 중 하나여야 합니다")
	return value


def _user(value, rules):
	try:
		user_text(value, rules)
		return cards.text(value, "사용자 글", 2000, rules=rules)
	except (ValueError, TypeError):
		raise ChatError("사용자 글의 길이·형식 또는 회사·비밀값·이메일·홈 경로를 확인하세요") from None


def _plans():
	return {key: plan for key, plan in load_plans().plans.items()
		if plan.owner == "personal" and "personal" in plan.allow and plan.tool in records.TOOLS}


def _plan(identity, rules):
	if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
		raise ChatError("대화 요금제 ID 형식이 잘못됐습니다")
	plan = _plans().get(identity)
	if plan is None:
		raise ChatError("현재 대화에 허용된 개인 요금제가 아닙니다")
	cards.text(plan.id, "요금제 ID", 64, rules=rules)
	return plan


def _session(value, rules):
	if isinstance(value, str) and records.SESSION.fullmatch(value):
		try:
			safe_text(value, rules)
		except ValueError:
			return None
		return value
	return None


class Engine:
	def __init__(self):
		self.store = records.Store()

	def plans(self):
		rules = Rules()
		result = []
		for plan in _plans().values():
			_plan(plan.id, rules)
			result.append({"id": plan.id, "tool": plan.tool})
		return result

	def new(self, target_id, plan_id):
		rules = Rules()
		selected = targets.target(target_id)
		cards.text(selected.id, "대상 ID", 31, rules=rules)
		plan = _plan(plan_id, rules)
		state = records.new(selected.id, plan.id, plan.tool)
		with self.store.locked(state["id"], create=True) as record:
			record.save(state)
		return state

	def _load(self, record):
		state = record.load()
		if state["status"] == "busy" and time.time() - record.modified() >= TIMEOUT:
			state.update(status="idle", session=None, failure="중단된 대화 호출을 대기 상태로 복구했습니다")
			record.save(state)
		return state

	def status(self, chat_id):
		try:
			with self.store.locked(chat_id) as record:
				return self._load(record)
		except ChatError as exc:
			if str(exc) != "이미 답을 쓰는 중입니다":
				raise
			state = self.store.read(chat_id)
			if state is None:
				raise ChatError("대화 기록을 읽지 못했습니다") from None
			return state

	def list(self):
		result = []
		for item in self.store.list_states():
			try:
				result.append(records.summary(self.status(item["id"])))
			except (OSError, ValueError, TypeError, KeyError, RecursionError):
				continue
		return result

	def _ready(self, state):
		if state["status"] == "closed":
			raise ChatError("닫힌 대화에는 보낼 수 없습니다")
		if state["status"] == "busy":
			raise ChatError("이미 답을 쓰는 중입니다")

	def send(self, chat_id, text, *, source="cli"):
		_source(source)
		rules = Rules()
		text = _user(text, rules)
		with self.store.locked(chat_id) as record:
			state = self._load(record)
			self._ready(state)
			if state["cards"] and state["cards"][-1]["status"] != "confirmed":
				raise ChatError("질문 카드에 답하고 요약을 먼저 확인하세요")
			return self._send(record, state, text, source, rules)

	def answer(self, chat_id, card_id, answers, *, source="cli"):
		_source(source)
		with self.store.locked(chat_id) as record:
			state = self._load(record)
			self._ready(state)
			if not state["cards"]:
				raise ChatError("현재 답변 대기 중인 질문 카드가 없습니다")
			rules = Rules()
			try:
				cards.accept(state["cards"][-1], card_id, answers, source, rules)
				_user(state["cards"][-1]["summary"], rules)
			except (ValueError, TypeError, KeyError):
				raise ChatError("현재 질문 카드의 구체적인 답변·명시 위임과 요약 길이를 확인하세요") from None
			record.save(state)
			return state

	def confirm(self, chat_id, card_id, *, source="cli"):
		_source(source)
		with self.store.locked(chat_id) as record:
			state = self._load(record)
			self._ready(state)
			if not state["cards"] or state["cards"][-1]["id"] != card_id or state["cards"][-1]["status"] != "answered":
				raise ChatError("답변 요약 확인이 필요한 현재 카드가 아닙니다")
			rules = Rules()
			text = _user(state["cards"][-1]["summary"], rules)
			return self._send(record, state, text, source, rules, confirm_card=card_id)

	def close(self, chat_id):
		with self.store.locked(chat_id) as record:
			state = self._load(record)
			if state["status"] == "busy":
				raise ChatError("이미 답을 쓰는 중입니다")
			state.update(status="closed", session=None)
			record.save(state)
			return state

	def _send(self, record, state, text, source, rules, *, confirm_card=None):
		if sum(t["role"] == "user" for t in state["turns"]) >= MAX_TURNS:
			raise ChatError("한 대화의 사용자 턴은 최대 50개입니다")
		plan = _plan(state["plan"], rules)
		if plan.tool != state["tool"]:
			raise ChatError("대화 요금제의 도구가 바뀌어 새 대화가 필요합니다")
		selected = targets.target(state["target"])
		history = state["turns"][-6:]
		for turn in history:
			cards.text(turn["text"], "이전 대화", 6000, rules=rules)
		with records.slot():
			if confirm_card is not None:
				cards.confirm(state["cards"][-1], confirm_card)
				state["cards"][-1]["confirmed_source"] = source
			state["turns"].append({"at": records.now(), "role": "user", "text": text, "source": source,
				"kind": "answer_summary" if confirm_card is not None else "message"})
			return self._exchange(record, state, history, text, source, rules, plan, selected.path)

	def _exchange(self, record, state, history, text, source, rules, plan, cwd):
		session, reason, hint = _session(state["session"], rules), "", ""
		state.update(status="busy", failure="")
		for attempt in range(2):
			state["calls"] += 1
			record.save(state)
			started, elapsed = time.monotonic(), None
			try:
				reply = providers.chat_call(plan, prompt=prompts.prompt(text, history, resume=session is not None, reason=reason + hint),
					cwd=cwd, schema=prompts.SCHEMA, timeout=TIMEOUT, session=session)
				if type(reply.seconds) in (int, float) and math.isfinite(reply.seconds) and reply.seconds >= 0:
					elapsed = reply.seconds
				value = prompts.validate_reply(reply.data, rules)
				card = cards.create(value["questions"], len(state["cards"]) + 1, "pm", rules) if value["questions"] else None
				session = _session(reply.session, rules)
			except providers.ProviderError as exc:
				reason, hint = PROVIDER_FAILURES.get(exc.kind, "대화 도구 호출을 마치지 못했습니다"), ""
			except (ValueError, TypeError, KeyError, RecursionError) as exc:
				reason = REJECTED
				# 우리 검사가 낸 짧은 문구만 재시도 안내에 붙인다(AI 응답 원문은 붙이지 않는다)
				hint = ": " + str(exc) if isinstance(exc, ValueError) and 0 < len(str(exc)) <= 100 and "\n" not in str(exc) else ""
			except Exception:
				reason, hint = "대화 도구 호출을 마치지 못했습니다", ""
			else:
				state["seconds"] += elapsed if elapsed is not None else time.monotonic() - started
				state["turns"].append({"at": records.now(), "role": "assistant", "text": value["reply"], "source": source, "kind": "message"})
				if card is not None:
					state["cards"].append(card)
				state.update(status="idle", session=session, failure="", dev_draft=value["dev_draft"], needs_user=value["needs_user"])
				record.save(state)
				return state
			state["seconds"] += elapsed if elapsed is not None else time.monotonic() - started
			session = None
			state.update(session=None, failure=reason)
		state["status"] = "idle"
		record.save(state)
		return state
