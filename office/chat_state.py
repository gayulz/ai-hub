"""대화 기록을 쓰기·복구 없이 읽어 공개 가능한 칸만 화면에 보낸다."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from collector.config import ConfigError, home
from orchestrator import chat_record, question_cards
from orchestrator.chat import Engine
from orchestrator.draft import safe_text
from orchestrator.meeting_prompts import LOCAL_PATH
from vaultfilter.public import Rules

from .common import normal, unavailable
from .meetings import masked
from .telegram_bridge import PATH

ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, ConfigError, RuntimeError, OverflowError)
STATUSES = ("idle", "busy", "closed")


class MissingChat(LookupError):
	pass


def clean(value: str, rules: Rules) -> str:
	# 경로 안의 이름을 먼저 가리면 경로 일부가 남을 수 있어 경로부터 생략한다.
	return masked(PATH.sub("[경로 생략]", LOCAL_PATH.sub("[경로 생략]", value)), rules)


def questions(card: dict, rules: Rules) -> list[dict]:
	result = []
	for q in card["questions"]:
		for identity in (q["id"], q["recommended"], *(o["id"] for o in q["options"])):
			safe_text(question_cards.identifier(identity), rules)
		result.append({"id": q["id"], **{key: clean(q[key], rules) for key in ("text", "reason")},
			"recommended": q["recommended"], "allow_delegate": q["allow_delegate"],
			"options": [{key: clean(o[key], rules) for key in ("id", "label", "difference", "reply")} for o in q["options"]]})
	return result


def summary(state: dict, rules: Rules) -> dict:
	chat_record.check(state, state["id"])
	return {key: clean(state[key], rules) for key in ("id", "target", "plan", "tool", "status")} | {
		"needs_user": clean(state["needs_user"], rules)[:200], "calls": state["calls"],
		"user_turns": sum(t["role"] == "user" for t in state["turns"])}


def modified(root: Path, identity: str) -> float:
	try:
		parent = chat_record.directory(root / "chats")
		try:
			folder = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
		finally:
			os.close(parent)
	except FileNotFoundError:
		raise MissingChat from None
	try:
		return os.stat("chat.json", dir_fd=folder, follow_symlinks=False).st_mtime
	finally:
		os.close(folder)


def stamp(value: float) -> str:
	return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def read_state(root: Path) -> dict:
	return snapshot(root)[0]


def snapshot(root: Path) -> tuple[dict, list[dict]]:
	# 목록은 20개로 줄이되 할 일의 대기 질문은 같은 읽기에서 빠짐없이 집계한다.
	items, broken = [], False
	try:
		plans = Engine().plans()
	except ERRORS:
		plans, broken = [], True
	try:
		if (root / "chats").exists() or (root / "chats").is_symlink():
			parent = chat_record.directory(root / "chats")
			try:
				names = [name for name in os.listdir(parent) if chat_record.ID.fullmatch(name)]
			finally:
				os.close(parent)
			rules, store = Rules(), chat_record.Store()
			for identity in names:
				try:
					state = store.read(identity)
					if state is None:
						raise ValueError("대화 기록을 읽지 못했습니다")
					row = summary(state, rules)
					updated = modified(root, identity)
					preview = next((t["text"] for t in reversed(state["turns"]) if t["role"] == "assistant"), "")
					card = state["cards"][-1] if state["cards"] else None
					row.update(updated=stamp(updated), preview=clean(preview, rules)[:60], card=None)
					if card and card["status"] == "waiting":
						row["card"] = {"card": card["id"], "turn": card["turn"], "questions": questions(card, rules)}
					items.append((updated, row))
				except (*ERRORS, MissingChat):
					broken = True
	except ERRORS:
		broken = True
	items.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
	group = unavailable("대화 설정 또는 일부 기록을 확인할 수 없습니다") if broken else normal()
	pending = [{"chat": row["id"], **row["card"]} for _, row in items if row["card"] is not None and row["status"] != "closed"]
	return {"chats": {**group, "items": [row for _, row in items[:20]]}, "chat_plans": plans}, pending


def detail(identity: str) -> dict:
	chat_record.identity(identity)
	updated = modified(home(), identity)
	state = chat_record.Store().read(identity)
	if state is None:
		raise ValueError("대화 기록을 읽지 못했습니다")
	rules = Rules()
	value = summary(state, rules)
	value.pop("user_turns")
	card = state["cards"][-1] if state["cards"] else None
	return {**value, "turns": [{key: clean(turn[key], rules)[:6000] for key in ("role", "text", "at", "source", "kind")}
		for turn in state["turns"]],
		"card": None if card is None else {key: card[key] for key in ("id", "turn", "status")} | {
			"questions": questions(card, rules), "summary": clean(card["summary"], rules)},
		"dev_draft": clean(state["dev_draft"], rules)[:2000], "seconds": state["seconds"],
		"failure": "대화를 마치지 못했습니다. 상태를 확인해 주세요." if state["failure"] else "", "updated": stamp(updated)}
