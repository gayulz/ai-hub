"""대화의 소유자 전용 기록·원자 교체·폴더 잠금과 전체 호출 슬롯."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime

from collector.config import home

from . import chat_prompts as prompts, question_cards as cards

ID = re.compile(r"c-[a-f0-9]{32}")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}")
SOURCES = ("office", "telegram", "cli")
# 읽기 전용이 실제로 지켜지는 도구만 둔다(AGY 는 plan 모드에서도 파일을 만들어 뺐다).
TOOLS = ("codex", "claude")
FIELDS = ("version", "id", "created", "target", "plan", "tool", "session", "status", "turns", "cards",
	"dev_draft", "needs_user", "calls", "seconds", "failure")


class ChatError(ValueError):
	"""입력이나 외부 예외 원문 없이 사용자에게 보여 줄 수 있는 이유."""


def identity(value):
	if not isinstance(value, str) or not ID.fullmatch(value):
		raise ChatError("대화 ID 형식이 잘못됐습니다")
	return value


def now():
	return datetime.now().astimezone().isoformat()


def new(target, plan, tool):
	return {"version": 1, "id": "c-" + uuid.uuid4().hex, "created": now(), "target": target, "plan": plan,
		"tool": tool, "session": None, "status": "idle", "turns": [], "cards": [], "dev_draft": "",
		"needs_user": "", "calls": 0, "seconds": 0.0, "failure": ""}


def _text(value, maximum, minimum=0):
	if (not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value
			or minimum and not value.strip()):
		raise ValueError("대화 기록 문자열 형식이 잘못됐습니다")
	value.encode("utf-8")


def _date(value):
	_text(value, 64, 1)
	if datetime.fromisoformat(value).utcoffset() is None:
		raise ValueError("대화 기록 시각 형식이 잘못됐습니다")


def _card(card):
	keys = {"id", "turn", "source", "questions", "status", "answers", "summary"}
	if isinstance(card, dict) and "confirmed_source" in card:
		keys.add("confirmed_source")
	cards.exact(card, keys)
	if (not isinstance(card["id"], str) or not re.fullmatch(r"q-[a-f0-9]{32}", card["id"])
			or type(card["turn"]) is not int or not 1 <= card["turn"] <= 5 or card["source"] != "pm"
			or card["status"] not in ("waiting", "answered", "confirmed")):
		raise ValueError("대화 질문 카드 형식이 잘못됐습니다")
	prompts.validate(card["questions"], cards.array(cards.QUESTION_SCHEMA, 3, 1))
	questions = set()
	for question in card["questions"]:
		key = cards.identifier(question["id"])
		options = [cards.identifier(o["id"]) for o in question["options"]]
		if key in questions or len(set(options)) != len(options) or question["recommended"] not in options:
			raise ValueError("대화 질문·선택지 형식이 잘못됐습니다")
		questions.add(key)
	_text(card["summary"], 2000)
	answers = card["answers"]
	if not isinstance(answers, list) or len(answers) > 3:
		raise ValueError("대화 답변 형식이 잘못됐습니다")
	seen = set()
	for answer in answers:
		cards.exact(answer, ("question_id", "kind", "value", "delegated", "source"))
		key = cards.identifier(answer["question_id"])
		if (key not in questions or key in seen or answer["kind"] not in ("choice", "text", "delegate")
				or type(answer["delegated"]) is not bool or answer["delegated"] != (answer["kind"] == "delegate")
				or answer["source"] not in SOURCES):
			raise ValueError("대화 답변 형식이 잘못됐습니다")
		seen.add(key)
		_text(answer["value"], 2000, 1)
	if (card["status"] == "waiting" and (answers or card["summary"])
			or card["status"] != "waiting" and (seen != questions or not card["summary"])):
		raise ValueError("대화 답변 상태가 잘못됐습니다")
	if "confirmed_source" in card and (card["status"] != "confirmed" or card["confirmed_source"] not in SOURCES):
		raise ValueError("대화 확인 출처가 잘못됐습니다")


def check(state, chat_id):
	cards.exact(state, FIELDS)
	if (state["id"] != identity(chat_id) or type(state["version"]) is not int or state["version"] != 1
			or state["tool"] not in TOOLS or state["status"] not in ("idle", "busy", "closed")):
		raise ValueError("대화 기록 형식이 잘못됐습니다")
	_date(state["created"])
	for key in ("target", "plan"):
		if not isinstance(state[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", state[key]):
			raise ValueError("대화 대상·요금제 ID 형식이 잘못됐습니다")
	if state["session"] is not None and (not isinstance(state["session"], str) or not SESSION.fullmatch(state["session"])):
		raise ValueError("대화 세션 ID 형식이 잘못됐습니다")
	if (type(state["calls"]) is not int or not 0 <= state["calls"] <= 100
			or type(state["seconds"]) not in (int, float) or not math.isfinite(state["seconds"]) or state["seconds"] < 0):
		raise ValueError("대화 호출 수·시간 형식이 잘못됐습니다")
	turns = state["turns"]
	if not isinstance(turns, list) or len(turns) > 100:
		raise ValueError("대화 턴 형식이 잘못됐습니다")
	for turn in turns:
		cards.exact(turn, ("at", "role", "text", "source", "kind"))
		if (turn["role"] not in ("user", "assistant") or turn["source"] not in SOURCES
				or turn["kind"] not in ("message", "answer_summary")
				or turn["role"] == "assistant" and turn["kind"] != "message"):
			raise ValueError("대화 턴 형식이 잘못됐습니다")
		_date(turn["at"])
		_text(turn["text"], 2000 if turn["role"] == "user" else 6000, 1)
	if sum(t["role"] == "user" for t in turns) > 50 or sum(t["role"] == "assistant" for t in turns) > 50:
		raise ValueError("대화 턴 한도를 넘었습니다")
	if not isinstance(state["cards"], list) or len(state["cards"]) > 5:
		raise ValueError("대화 질문 카드 형식이 잘못됐습니다")
	for card in state["cards"]:
		_card(card)
	if len({c["id"] for c in state["cards"]}) != len(state["cards"]):
		raise ValueError("대화 질문 카드 ID가 중복됐습니다")
	for key, maximum in (("dev_draft", 2000), ("needs_user", 200), ("failure", 200)):
		_text(state[key], maximum)
	if "\n" in state["failure"] or "\r" in state["failure"]:
		raise ValueError("대화 실패 이유는 한 줄이어야 합니다")
	return state


def directory(path):
	if not path.is_absolute() or ".." in path.parts:
		raise ChatError("대화 폴더 경로가 올바르지 않습니다")
	fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
	try:
		for part in path.parts[1:]:
			next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
			os.close(fd)
			fd = next_fd
		return fd
	except BaseException:
		os.close(fd)
		raise


def regular(fd, *, private=False):
	info = os.fstat(fd)
	if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
		raise ChatError("대화 파일은 링크 없는 일반 파일이어야 합니다")
	if private:
		os.fchmod(fd, 0o600)
	return info


class Record:
	def __init__(self, fd, chat_id):
		self.fd, self.id = fd, chat_id

	def check_files(self):
		for name in ("chat.json", ".lock"):
			try:
				info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
			except FileNotFoundError:
				continue
			if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
				raise ChatError("대화 파일은 링크 없는 일반 파일이어야 합니다")

	def load(self):
		self.check_files()
		fd = os.open("chat.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
		with os.fdopen(fd, "r", encoding="utf-8") as stream:
			if regular(stream.fileno()).st_size > 3_000_000:
				raise ChatError("대화 기록 크기 한도입니다")
			return check(json.load(stream), self.id)

	def modified(self):
		return os.stat("chat.json", dir_fd=self.fd, follow_symlinks=False).st_mtime

	def save(self, state):
		check(state, self.id)
		self.check_files()
		temporary = ".chat-" + uuid.uuid4().hex
		fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as stream:
				regular(stream.fileno(), private=True)
				json.dump(state, stream, ensure_ascii=False, allow_nan=False)
				stream.write("\n")
				stream.flush()
				os.fsync(stream.fileno())
			os.replace(temporary, "chat.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
			os.fsync(self.fd)
		finally:
			try:
				os.unlink(temporary, dir_fd=self.fd)
			except FileNotFoundError:
				pass


class Store:
	def read(self, chat_id):
		"""쓰기·잠금 생성·권한 변경 없이 안전한 상태만 읽는다."""
		root_fd = folder_fd = None
		try:
			identity(chat_id)
			root_fd = directory(home() / "chats")
			folder_fd = os.open(chat_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
			return Record(folder_fd, chat_id).load()
		except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
			return None
		finally:
			for fd in (folder_fd, root_fd):
				if fd is not None:
					os.close(fd)

	def list_states(self):
		"""최근 50개 요약. 링크나 손상된 항목은 건너뛴다."""
		try:
			fd = directory(home() / "chats")
			try:
				names = [name for name in os.listdir(fd) if ID.fullmatch(name)]
			finally:
				os.close(fd)
		except (OSError, ValueError):
			return []
		states = [s for name in names if (s := self.read(name)) is not None]
		states.sort(key=lambda s: datetime.fromisoformat(s["created"]).timestamp(), reverse=True)
		return [summary(s) for s in states[:50]]

	@contextmanager
	def locked(self, chat_id, *, create=False):
		identity(chat_id)
		parent = directory(home())
		root_fd = folder_fd = lock_fd = None
		try:
			if create:
				try:
					os.mkdir("chats", 0o700, dir_fd=parent)
				except FileExistsError:
					pass
			root_fd = os.open("chats", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
			os.fchmod(root_fd, 0o700)
			if create:
				os.mkdir(chat_id, 0o700, dir_fd=root_fd)
			folder_fd = os.open(chat_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
			os.fchmod(folder_fd, 0o700)
			lock_fd = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=folder_fd)
			regular(lock_fd, private=True)
			try:
				fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				raise ChatError("이미 답을 쓰는 중입니다") from None
			record = Record(folder_fd, chat_id)
			record.check_files()
			yield record
		finally:
			for fd in (lock_fd, folder_fd, root_fd, parent):
				if fd is not None:
					os.close(fd)


def summary(state):
	return {key: state[key] for key in ("id", "created", "target", "plan", "tool", "status", "needs_user", "calls", "seconds", "failure")} | {
		"user_turns": sum(t["role"] == "user" for t in state["turns"])}


@contextmanager
def slot():
	"""세 호출까지만 허용하며, 프로세스가 끝나면 운영체제가 슬롯을 돌려준다."""
	fd = directory(home())
	lock_fd = None
	try:
		for name in ("run", "chat-slots"):
			try:
				os.mkdir(name, 0o700, dir_fd=fd)
			except FileExistsError:
				pass
			next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
			os.close(fd)
			fd = next_fd
		os.fchmod(fd, 0o700)
		for number in range(3):
			lock_fd = os.open(f"slot{number}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
			regular(lock_fd, private=True)
			try:
				fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				os.close(lock_fd)
				lock_fd = None
				continue
			break
		if lock_fd is None:
			raise ChatError("대화 호출이 가득 찼습니다")
		yield
	finally:
		if lock_fd is not None:
			os.close(lock_fd)
		os.close(fd)
