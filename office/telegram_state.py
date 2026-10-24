"""텔레그램의 재시작 위치·중복 방지를 잠금과 원자 교체로 보관한다."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from collector.config import home
from orchestrator.meeting_record import directory, identity
from orchestrator.question_cards import identifier
from .telegram_client import BOT_SECRET

CARD = re.compile(r"q-[a-f0-9]{32}")
ASK = re.compile(r"k-[a-f0-9]{8}")


def empty():
	return {"offset": 0, "baseline_done": False, "seen": [], "cards": {}, "asks": {}}


def message_id(value):
	return type(value) is int and value > 0


def checked(value):
	if isinstance(value, dict) and set(value) == set(empty()) - {"asks"}:
		value["asks"] = {}
	if (not isinstance(value, dict) or set(value) != set(empty()) or type(value["offset"]) is not int
			or value["offset"] < 0 or type(value["baseline_done"]) is not bool
			or not isinstance(value["seen"], list) or not isinstance(value["cards"], dict) or not isinstance(value["asks"], dict)):
		raise ValueError("연동 상태 형식 오류")
	for key in value["seen"]:
		if not isinstance(key, str) or not re.fullmatch(r"(?:meeting:m-[a-f0-9]{32}:[a-z_]+|run:[0-9-]{14})", key):
			raise ValueError("알림 키 형식 오류")
	now, rows = datetime.now().astimezone(), []
	for key, card in value["cards"].items():
		if not CARD.fullmatch(key) or not isinstance(card, dict) or set(card) != {"meeting", "questions", "pending", "submit_message", "created"}:
			raise ValueError("카드 상태 형식 오류")
		identity(card["meeting"])
		if not isinstance(card["questions"], dict) or not isinstance(card["pending"], dict) or len(card["questions"]) > 3:
			raise ValueError("질문 상태 형식 오류")
		for qid, mid in card["questions"].items():
			identifier(qid)
			if not message_id(mid):
				raise ValueError("메시지 ID 형식 오류")
		for qid, answer in card["pending"].items():
			if (qid not in card["questions"] or not isinstance(answer, dict) or set(answer) != {"kind", "value"}
					or answer["kind"] not in ("choice", "text", "delegate") or not isinstance(answer["value"], str)
					or not 1 <= len(answer["value"]) <= 2000 or BOT_SECRET.search(answer["value"])):
				raise ValueError("대기 답변 형식 오류")
		if card["submit_message"] is not None and not message_id(card["submit_message"]):
			raise ValueError("제출 메시지 형식 오류")
		created = datetime.fromisoformat(card["created"])
		if created.tzinfo is None:
			raise ValueError("생성 시각 형식 오류")
		if now - timedelta(days=7) <= created <= now:
			rows.append((key, card))
	value["seen"] = list(dict.fromkeys(value["seen"]))[-500:]
	value["cards"] = dict(sorted(rows, key=lambda row: row[1]["created"])[-100:])
	value["asks"] = checked_asks(value["asks"], now)
	return value


def checked_asks(asks, now):
	rows = []
	for key, ask in asks.items():
		if (not isinstance(key, str) or not ASK.fullmatch(key) or not isinstance(ask, dict)
				or set(ask) != {"id", "message_id", "options", "created", "answer"} or ask["id"] != key
				or not message_id(ask["message_id"]) or not isinstance(ask["options"], list)
				or not 2 <= len(ask["options"]) <= 4):
			raise ValueError("확인 요청 형식 오류")
		if any(not isinstance(option, str) or not 1 <= len(option) <= 20 or BOT_SECRET.search(option) for option in ask["options"]):
			raise ValueError("확인 선택지 형식 오류")
		created = datetime.fromisoformat(ask["created"])
		if created.tzinfo is None:
			raise ValueError("확인 생성 시각 형식 오류")
		answer = ask["answer"]
		if answer is not None:
			if (not isinstance(answer, dict) or set(answer) != {"kind", "value", "at"}
					or answer["kind"] not in ("option", "text") or not isinstance(answer["value"], str)
					or not 1 <= len(answer["value"]) <= 1000 or BOT_SECRET.search(answer["value"])
					or (answer["kind"] == "option" and answer["value"] not in ask["options"])):
				raise ValueError("확인 답 형식 오류")
			at = datetime.fromisoformat(answer["at"])
			if at.tzinfo is None or not created <= at <= now:
				raise ValueError("확인 답 시각 형식 오류")
		if now - timedelta(days=7) <= created <= now:
			rows.append((key, ask))
	return dict(sorted(rows, key=lambda row: row[1]["created"])[-50:])


def private(fd):
	info = os.fstat(fd)
	if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
		raise ValueError("연동 파일 권한·링크 확인 필요")


class Store:
	def __init__(self):
		self.root = home()
		self._lock = threading.RLock()

	def _load(self, folder):
		try:
			fd = os.open("telegram-state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
			with os.fdopen(fd, "r", encoding="utf-8") as stream:
				private(stream.fileno())
				if os.fstat(stream.fileno()).st_size > 2_000_000:
					raise ValueError("연동 상태 크기 한도")
				return checked(json.load(stream))
		except (OSError, ValueError, TypeError, KeyError, RecursionError):
			return empty()

	def _save(self, folder, state):
		try:
			info = os.stat("telegram-state.json", dir_fd=folder, follow_symlinks=False)
		except FileNotFoundError:
			pass
		else:
			if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
				raise ValueError("연동 파일 링크 확인 필요")
		name = ".telegram-" + uuid.uuid4().hex
		fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as stream:
				os.fchmod(stream.fileno(), 0o600)
				json.dump(checked(state), stream, ensure_ascii=False, allow_nan=False)
				stream.flush()
				os.fsync(stream.fileno())
			os.replace(name, "telegram-state.json", src_dir_fd=folder, dst_dir_fd=folder)
			os.fsync(folder)
		finally:
			try:
				os.unlink(name, dir_fd=folder)
			except FileNotFoundError:
				pass

	@contextmanager
	def locked(self):
		with self._lock:
			parent = directory(self.root)
			folder = lock = None
			try:
				try:
					os.mkdir("run", 0o700, dir_fd=parent)
				except FileExistsError:
					pass
				folder = os.open("run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
				os.fchmod(folder, 0o700)
				try:
					lock = os.open(".telegram.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=folder)
				except FileExistsError:
					lock = os.open(".telegram.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
				private(lock)
				fcntl.flock(lock, fcntl.LOCK_EX)
				state = self._load(folder)
				try:
					yield state
				finally:
					self._save(folder, state)
			finally:
				for fd in (lock, folder, parent):
					if fd is not None:
						os.close(fd)
