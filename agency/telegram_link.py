"""기존 사무실 봇의 질문·답 기록을 접수 원장과 상태 알림에 연결한다."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from collector.backfill_common import locked, private_write, safe
from office import telegram_ask
from office.telegram_client import Client
from office.telegram_state import Store

from . import departments, hooks, intake, project

client = Client()
store = Store()
LABELS = {"막힘": "[막힘]", "납품 대기": "[납품 승인 필요]", "완료": "[완료]"}
DEFER = ("사무실에서 답할게", "사무실에서 고칠게")


def read_state(identity):
	path = project._folder(identity) / "telegram.json"
	if not safe(path):
		raise ValueError("텔레그램 연결 파일에 심볼릭 링크를 사용할 수 없습니다")
	try:
		state = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError:
		return {"asks": {}, "answered": 0, "notified": []}
	if not isinstance(state, dict) or not isinstance(state.get("asks"), dict):
		raise ValueError("텔레그램 연결 기록 형식이 잘못됐습니다")
	for ask_id, ask in state["asks"].items():
		if (not isinstance(ask_id, str) or not isinstance(ask, dict) or ask.keys() != {"no", "done"}
				or type(ask["no"]) is not int or ask["no"] < 1 or type(ask["done"]) is not bool):
			raise ValueError("텔레그램 질문 짝 형식이 잘못됐습니다")
	state.setdefault("answered", 0)
	state.setdefault("notified", [])
	if (type(state["answered"]) is not int or state["answered"] < 0
			or not isinstance(state["notified"], list)
			or any(not isinstance(key, list) or len(key) != 2
				or any(not isinstance(value, str) for value in key) for key in state["notified"])):
		raise ValueError("텔레그램 처리 기록 형식이 잘못됐습니다")
	return state


def _save(identity, state):
	private_write(project._folder(identity) / "telegram.json",
		json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _ask(row, question):
	value = ("[사무실 질문] " + " ".join(row["title"].splitlines())[:40]
		+ "\n질문 " + str(question["no"]) + ". " + question["question"]
		+ "\n\n이 메시지에 답장으로 적거나 버튼을 눌러 주세요.")
	options = "맞아,사무실에서 고칠게" if question["question"].startswith(intake.CONFIRM_PREFIX) else "알아서 정해 줘,사무실에서 답할게"
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".telegram-",
			dir=project._folder(row["id"]), delete=False) as stream:
		path = Path(stream.name)
		try:
			stream.write(value)
			stream.flush()
			return telegram_ask.ask(client, store, path, options)
		finally:
			path.unlink()


def _sync(row, state):
	identity = row["id"]
	opened = {question["no"] for question in intake._questions(identity)["open"]}
	for ask_id, ask in state["asks"].items():
		if ask["done"]:
			continue
		if ask["no"] not in opened:
			ask["done"] = True
			continue
		try:
			answer = telegram_ask.current(store, ask_id)
		except ValueError:
			ask["done"] = True
			continue
		if answer is None:
			continue
		if answer["value"] not in DEFER:
			value = answer["value"]
			if value == "알아서 정해 줘":
				value = "알아서 정해 줘(PM 이 판단해 명세에 적는다)"
			try:
				intake.answer(identity, ask["no"], value)
			except ValueError:
				pass
			else:
				state["answered"] += 1
		ask["done"] = True
	if project.load(identity)["status"] != "접수 중":
		return
	sent = {ask["no"] for ask in state["asks"].values()}
	for question in intake._questions(identity)["open"]:
		if question["no"] in sent:
			continue
		try:
			ask_id = _ask(row, question)
		except ValueError:
			# 전송 전에 거부된 질문도 번호를 남겨 사무실에서만 답하게 한다.
			state["asks"]["skipped-" + str(question["no"])] = {"no": question["no"], "done": True}
		else:
			state["asks"][ask_id] = {"no": question["no"], "done": False}


def sync():
	if client.status != "켜짐":
		return
	found = departments.load_departments()
	for row in project.list_projects():
		department = found.get(row["department"])
		if row["status"] != "접수 중" or department is None or not department.external_ai:
			continue
		with locked("agency-telegram:" + row["id"]):
			state = read_state(row["id"])
			try:
				_sync(row, state)
			finally:
				_save(row["id"], state)


def notify(identity, before, after):
	if client.status != "켜짐" or after not in LABELS:
		return
	with locked("agency-telegram:" + identity):
		row = project.load(identity)
		department = departments.load_departments().get(row["department"])
		if department is None:
			return
		state = read_state(identity)
		key = [after, row["updated"]]
		if key in state["notified"]:
			return
		if not department.external_ai:
			value = "회사 프로젝트 1건 상태 변경, 사무실에서 확인"
		else:
			lines = [LABELS[after] + " " + " ".join(row["title"].splitlines())[:40], "프로젝트: " + identity]
			if after == "막힘":
				lines.append("이유: " + " ".join(row.get("blocked_reason", "").splitlines())[:120])
			elif after == "납품 대기":
				lines.append("할 일: 사무실 화면에서 납품 묶음을 확인하고 승인하세요")
			else:
				lines.append("결과: 문서 납품 완료" if department.deliverable == "document" else "결과: 본 가지 반영 완료")
			value = "\n".join(lines)
			if client.sensitive(value):
				value = "\n".join(lines[:2])
		client.send_message(value)
		state["notified"].append(key)
		_save(identity, state)


if client.status == "켜짐":
	hooks.on_before_tick(sync)
	hooks.on_changed(notify)
