"""PM 대화의 질문 카드 답·제출·요약 확인만 연결한다.

대화 글 보내기·새 대화·닫기·개발 지시·반영·push·삭제·재시작·임의 명령은 받지 않는다.
침묵·다른 글은 동의가 아니다. PM 답 전문·턴은 보내지 않고 질문 카드와 도착 알림만 보낸다.
"""

import json
from datetime import datetime

from orchestrator.chat import Engine
from orchestrator.chat_record import Store
from orchestrator import question_cards
from vaultfilter.public import Rules

FAILED = "대화 확인: 실행하지 못했습니다. 사무실에서 확인하세요"
# 대화 호출 600초와 재시도 한 회에 종료 여유를 더한다.
DEADLINE = 1500


class Chat:
	def __init__(self, bridge):
		self.bridge, self.engine, self.records = bridge, Engine(), Store()

	def heading(self, local):
		state = self.records.read(local["meeting"])
		return "PM 대화 질문\n대상 저장소 ID: " + (state["target"] if state else "확인 불가")

	def sync(self, state):
		bridge = self.bridge
		for item in self.records.list_states():
			if item["status"] != "idle":
				continue
			chat = self.records.read(item["id"])
			if not chat or chat["status"] != "idle" or not chat["cards"]:
				continue
			card = chat["cards"][-1]
			local = state["cards"].get(card["id"])
			if local and (local.get("kind", "meeting") != "chat" or local["meeting"] != chat["id"]):
				continue
			if card["status"] == "waiting":
				try:
					question_cards.validate_questions(card["questions"], Rules(), minimum=1)
					if bridge.client.sensitive(json.dumps([chat["id"], card["id"], card["questions"]], ensure_ascii=False)):
						continue
				except (ValueError, TypeError, KeyError):
					continue
				if local is None:
					local = state["cards"][card["id"]] = {"kind": "chat", "meeting": chat["id"], "questions": {},
						"pending": {}, "submit_message": None, "created": datetime.now().astimezone().isoformat()}
				for number, q in enumerate(card["questions"], 1):
					if q["id"] not in local["questions"]:
						local["questions"][q["id"]] = bridge.send(*bridge.question(card, number, local))
				bridge.ready(card, local)
			elif local and card["status"] == "answered" and bridge.telegram(card) and local["submit_message"] is None:
				bridge.summary(card, local)

	def prepare(self, job):
		chat_id, card_id = job
		chat = self.records.read(chat_id)
		card = chat["cards"][-1] if chat and chat["cards"] else None
		valid = (chat and chat["status"] == "idle" and card and card["id"] == card_id
			and card["status"] == "answered" and self.bridge.telegram(card))
		return valid, ["confirm", chat_id, "--card", card_id, "--source", "telegram"], FAILED

	def finished(self, job):
		chat_id, card_id = job["key"]
		chat = self.records.read(chat_id)
		card = next((c for c in chat["cards"] if c["id"] == card_id), None) if chat else None
		if (job["timeout"] or job["process"].poll() != 0 or not chat or chat["status"] != "idle"
				or chat["failure"] or not card or card["status"] != "confirmed" or card.get("confirmed_source") != "telegram"):
			return FAILED
		return "대화 " + chat_id[:8] + ": 답이 도착했습니다. 사무실 대화창에서 확인하세요"
