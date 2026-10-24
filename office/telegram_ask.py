"""확인 요청을 보내고 답을 읽는다. 수신과 실행은 명령줄에서 하지 않는다."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from orchestrator.question_cards import text
from vaultfilter.public import Rules

from .telegram_client import Client
from .telegram_state import ASK, Store, message_id


class Parser(argparse.ArgumentParser):
	def error(self, message):
		raise ValueError("명령줄 인자를 확인하세요")


def timeout(value):
	seconds = int(value)
	if not 1 <= seconds <= 86400:
		raise ValueError("대기는 1~86400초여야 합니다")
	return seconds


def checked_text(client, value, maximum, rules):
	if client.sensitive(value):
		raise ValueError("민감값은 보낼 수 없습니다")
	return text(value, "확인 요청", maximum, rules=rules)


def ask(client, store, path, options):
	if client.status != "켜짐":
		raise ValueError("텔레그램 설정을 확인하세요")
	with path.open(encoding="utf-8") as stream:
		question = stream.read(3001)
	rules = Rules()
	checked_text(client, question, 3000, rules)
	options = [option.strip() for option in options.split(",")]
	if not 2 <= len(options) <= 4:
		raise ValueError("선택지는 2~4개여야 합니다")
	for option in options:
		checked_text(client, option, 20, rules)
	with store.locked() as state:
		identity = "k-" + uuid.uuid4().hex[:8]
		while identity in state["asks"]:
			identity = "k-" + uuid.uuid4().hex[:8]
		buttons = [[{"text": option, "callback_data": f"k|{identity}|{index}"}]
			for index, option in enumerate(options, 1)]
		result = client.send_message(question, buttons)
		if not isinstance(result, dict) or not message_id(result.get("message_id")):
			raise ValueError("확인 메시지 ID를 받지 못했습니다")
		state["asks"][identity] = {"id": identity, "message_id": result["message_id"], "options": options,
			"created": datetime.now().astimezone().isoformat(), "answer": None}
	return identity


def current(store, identity):
	if not ASK.fullmatch(identity):
		raise ValueError("확인 ID 형식 오류")
	with store.locked() as state:
		if identity not in state["asks"]:
			raise ValueError("확인 요청을 찾을 수 없습니다")
		return state["asks"][identity]["answer"]


def wait(store, identity, seconds, *, emit=True):
	deadline = time.monotonic() + seconds
	while True:
		answer = current(store, identity)
		if answer is not None:
			if emit:
				print(json.dumps({"id": identity, **answer}, ensure_ascii=False))
			return 0
		if time.monotonic() >= deadline:
			return 3
		time.sleep(1)


def answer_option(client, callback, state):
	data = callback.get("data", "")
	parts = data.split("|") if isinstance(data, str) and len(data.encode("utf-8")) <= 64 else []
	if len(parts) != 3 or parts[0] != "k" or not ASK.fullmatch(parts[1]) or parts[2] not in ("1", "2", "3", "4"):
		return "허용하지 않는 응답입니다"
	ask = state["asks"].get(parts[1])
	if ask is None or callback["message"].get("message_id") != ask["message_id"] or int(parts[2]) > len(ask["options"]):
		return "확인 요청을 찾을 수 없습니다"
	if ask["answer"] is not None:
		return "이미 답했습니다"
	value = ask["options"][int(parts[2]) - 1]
	if client.sensitive(value):
		return "민감값은 기록할 수 없습니다"
	ask["answer"] = {"kind": "option", "value": value, "at": datetime.now().astimezone().isoformat()}
	client.edit_message(ask["message_id"], "선택됨: " + value)
	return "선택했습니다"


def answer_text(client, message, state):
	reply = message.get("reply_to_message")
	mid = reply.get("message_id") if isinstance(reply, dict) else None
	if not message_id(mid):
		return False
	ask = next((ask for ask in state["asks"].values() if ask["message_id"] == mid), None)
	if ask is None:
		return False
	if ask["answer"] is None:
		value = message.get("text")
		try:
			if not isinstance(value, str):
				raise ValueError("문자열 답이 필요합니다")
			checked_text(client, value, 1000, Rules())
		except (ValueError, TypeError):
			return True
		ask["answer"] = {"kind": "text", "value": value, "at": datetime.now().astimezone().isoformat()}
	return True


def main(argv=None):
	parser = Parser(description="텔레그램 확인 요청과 답 기록")
	commands = parser.add_subparsers(dest="command", required=True)
	send = commands.add_parser("ask", help="선택지 버튼이 있는 질문 보내기")
	send.add_argument("--text-file", type=Path, required=True)
	send.add_argument("--options", required=True)
	send.add_argument("--timeout", type=timeout, help="ID 출력 후 답을 기다릴 시간(1~86400초)")
	pending = commands.add_parser("ask-wait", help="답이 올 때까지 대기")
	pending.add_argument("id")
	pending.add_argument("--timeout", type=timeout, required=True, help="필수 대기 상한(1~86400초)")
	status = commands.add_parser("ask-status", help="현재 답 또는 null 출력")
	status.add_argument("id")
	try:
		args = parser.parse_args(argv)
		store = Store()
		if args.command == "ask":
			identity = ask(Client(), store, args.text_file, args.options)
			print(identity, flush=True)
			return wait(store, identity, args.timeout, emit=False) if args.timeout is not None else 0
		if args.command == "ask-wait":
			return wait(store, args.id, args.timeout)
		print(json.dumps({"id": args.id, "answer": current(store, args.id)}, ensure_ascii=False))
		return 0
	except Exception:
		print("확인 요청을 처리하지 못했습니다. 인자·설정·민감정보 포함 여부를 확인하세요", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
