"""회의·개발 초안·숏폼이 공유할 질문 계약. 후자의 실제 연결은 후속 단계다.

카드는 1~3개 질문, 대화는 최대 5회다. accept는 호출자가 잡은 잠금 안에서만
사용한다. 답변 묶음 전체가 유효해야 저장하며 confirmed 전에는 진행하지 않는다.
"""

from __future__ import annotations

import re
import uuid

from .draft import safe_text, string

IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,40}")
HOME_PATH = re.compile(r"/(?:Users|home)/[^\s/]+|[A-Za-z]:[\\/]Users[\\/]|~[\\/]")


def text(value, field="문자열", maximum=2000, *, rules):
	value = string(value, field, maximum)
	if any(ord(c) < 32 and c not in "\n\t" for c in value) or "\x7f" in value:
		raise ValueError("제어 문자는 허용하지 않습니다")
	safe_text(value, rules)
	if HOME_PATH.search(value):
		raise ValueError("홈 경로는 허용하지 않습니다")
	return value


def exact(value, keys):
	if not isinstance(value, dict) or set(value) != set(keys):
		raise ValueError("필수 필드 또는 허용된 JSON 형식과 다릅니다")


def identifier(value):
	if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
		raise ValueError("질문·선택지 ID 형식이 잘못됐습니다")
	return value


def validate_questions(value, rules, *, minimum=0):
	if not isinstance(value, list) or not minimum <= len(value) <= 3:
		raise ValueError("질문은 한 번에 1~3개여야 합니다")
	seen = set()
	for q in value:
		exact(q, ("id", "text", "options", "recommended", "reason", "allow_delegate"))
		identity = identifier(q["id"])
		text(identity, "질문 ID", 40, rules=rules)
		if identity in seen:
			raise ValueError("질문 ID가 중복됐습니다")
		seen.add(identity)
		text(q["text"], "질문", 500, rules=rules)
		text(q["reason"], "추천 이유", 500, rules=rules)
		if type(q["allow_delegate"]) is not bool or not isinstance(q["options"], list) or not 2 <= len(q["options"]) <= 3:
			raise ValueError("선택지 2~3개와 명시 위임 허용 여부가 필요합니다")
		options = set()
		for option in q["options"]:
			exact(option, ("id", "label", "difference", "reply"))
			key = identifier(option["id"])
			text(key, "선택지 ID", 40, rules=rules)
			if key in options:
				raise ValueError("선택지 ID가 중복됐습니다")
			options.add(key)
			for field in ("label", "difference", "reply"):
				text(option[field], field, 500, rules=rules)
		if q["recommended"] not in options:
			raise ValueError("추천 선택지가 목록에 없습니다")
	return value


def create(questions, turn: int, source: str, rules) -> dict:
	if type(turn) is not int or not 1 <= turn <= 5:
		raise ValueError("질문 대화 5회 한도입니다. 자동 동의하지 않습니다")
	validate_questions(questions, rules, minimum=1)
	text(source, "질문 출처", 100, rules=rules)
	return {"id": "q-" + uuid.uuid4().hex, "turn": turn, "source": source,
		"questions": questions, "status": "waiting", "answers": [], "summary": ""}


def accept(card: dict, card_id: str, answers, source: str, rules) -> None:
	if card["id"] != card_id or card["status"] != "waiting":
		raise ValueError("현재 대기 카드가 아니거나 이미 유효한 답을 받았습니다")
	text(source, "응답 출처", 100, rules=rules)
	validate_questions(card["questions"], rules, minimum=1)
	if not isinstance(answers, list) or len(answers) != len(card["questions"]):
		raise ValueError("모든 질문의 답이 필요합니다")
	by_id, accepted, summary = {q["id"]: q for q in card["questions"]}, [], []
	seen = set()
	for answer in answers:
		exact(answer, ("question_id", "kind", "value"))
		identity = identifier(answer["question_id"])
		if identity not in by_id or identity in seen:
			raise ValueError("질문 ID가 잘못됐거나 중복됐습니다")
		seen.add(identity)
		q, kind = by_id[identity], answer["kind"]
		value = text(answer["value"], "답변", rules=rules).strip()
		if kind == "choice":
			option = next((o for o in q["options"] if o["id"] == value or o["reply"] == value), None)
			if option is None:
				raise ValueError("선택지 또는 회신 문구와 다릅니다")
			resolved = option["label"] + ": " + option["reply"]
		elif kind in ("text", "delegate"):
			if (len(value) < 2 or not any(c.isalnum() for c in value)
					or value.lower() in ("네", "예", "동의", "모름", "몰라요", "ok", "yes")
					or (value == "알아서" and kind != "delegate")):
				raise ValueError("구체적인 답변 또는 명시 위임이 필요합니다")
			if kind == "delegate" and not q["allow_delegate"]:
				raise ValueError("이 질문은 위임할 수 없습니다")
			resolved = ("명시 위임: " if kind == "delegate" else "직접 답변: ") + value
		else:
			raise ValueError("답변은 choice·text·delegate 중 하나여야 합니다")
		accepted.append({**answer, "value": value, "delegated": kind == "delegate", "source": source})
		summary.append(q["text"] + " → " + resolved)
	card.update(status="answered", answers=accepted, summary="\n".join(summary))


def confirm(card, card_id):
	if card["id"] != card_id or card["status"] != "answered":
		raise ValueError("답변 요약 확인이 필요한 현재 카드가 아닙니다")
	card["status"] = "confirmed"


def obj(properties):
	return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def array(items, maximum=3, minimum=0):
	return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


STRING = {"type": "string", "minLength": 1, "maxLength": 500}
QUESTION_SCHEMA = obj({"id": {"type": "string", "pattern": IDENTIFIER.pattern}, "text": STRING,
	"options": array(obj({"id": STRING, "label": STRING, "difference": STRING, "reply": STRING}), 3, 2),
	"recommended": STRING, "reason": STRING, "allow_delegate": {"type": "boolean"}})
