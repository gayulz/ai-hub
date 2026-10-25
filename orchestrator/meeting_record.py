"""회의별 소유자 전용 저장·잠금·자기완결 회의록과 기존 개인 일지 착지점."""

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
from pathlib import Path

from collector import vault
from collector.backfill_common import clean, safe
from collector.config import home, load_sources

MAX_CALLS, MAX_SECONDS = 14, 1800
# 정리 호출에 실제 소요 시간과 저장 여유를 남긴다(큰 회의의 정리는 180초로 모자랐다).
SUMMARY_SECONDS = 420
# 참석자·분석 호출 하나가 남은 시간을 모두 쓰면 정리 단계가 굶는다. 한 번의 호출은 이 시간까지만 쓴다.
CALL_SECONDS = 360
ID = re.compile(r"m-[a-f0-9]{32}")
FILES = ("meeting.json", "minutes.md", ".lock")
STATUSES = ("analyzing", "participants_pending", "questions_pending", "answers_pending", "running",
	"completed", "failed", "budget_exhausted", "questions_exhausted", "record_pending")


def identity(value):
	if not isinstance(value, str) or not ID.fullmatch(value):
		raise ValueError("회의 ID 형식이 잘못됐습니다")
	return value


def new(agenda, target):
	return {"version": 1, "id": "m-" + uuid.uuid4().hex, "created": datetime.now().astimezone().isoformat(),
		"agenda": agenda, "target": target, "status": "analyzing", "phase": "analysis", "confirmed": False,
		"analysis": None, "participants": [], "absent": [], "rounds": {"1": [], "2": []}, "cards": [], "question_queue": [],
		"calls": [], "active_seconds": 0.0, "failure": "", "summary": None, "partial": False,
		"journal": "pending", "open_questions": []}


def minutes(state):
	lines = ["# 읽기 전용 회의 " + state["id"], "", "- 안건: " + clean(state["agenda"]),
		"- 대상: " + state["target"], "- 생성: " + state["created"], "- 상태: " + state["status"],
		"- 현재 단계: " + state["phase"], "- 참석자 확인: " + str(state["confirmed"]),
		f"- 호출: {len(state['calls'])}/{MAX_CALLS} · 능동 시간: {state['active_seconds']:.3f}/{MAX_SECONDS}초",
		f"- 재시도: {sum(bool(call.get('retry')) for call in state['calls'])}회",
		"- 실패: " + (state["failure"] or "없음"), "- 개인 일지: " + state["journal"],
		"- 결정 항목은 토론의 후보이며 구현·반영 승인이 아니다."]
	lines += ["- 불참: " + ", ".join((a["role"], a["phase"], a["reason"] + ": " + a["detail"]))
		for a in state.get("absent", [])]
	for heading, value in (("안건 분석", state["analysis"]), ("참석자와 선택 이유", state["participants"]),
			("참석자 확인·조정·출처", state.get("participant_confirmation")),
			("round1", state["rounds"]["1"]), ("round2", state["rounds"]["2"]),
			("문답·위임·출처·요약 확인", state["cards"]), ("정리", state["summary"]),
			("열린 질문", state["open_questions"] + state["question_queue"]), ("호출·실패 예산", state["calls"])):
		lines += ["", "## " + heading, "", "```json", clean(json.dumps(value, ensure_ascii=False, indent=1)), "```"]
	return "\n".join(lines) + "\n"


def regular(fd):
	info = os.fstat(fd)
	if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
		raise ValueError("회의 파일은 링크 없는 일반 파일이어야 합니다")
	os.fchmod(fd, 0o600)


def directory(path):
	if not path.is_absolute() or ".." in path.parts or not safe(path):
		raise ValueError("회의 경로에 상대 경로·링크·비밀 경로를 사용할 수 없습니다")
	# 부모까지 디렉터리 핸들로 걸어가 링크 교체로 경계가 바뀌는 것을 막는다.
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


class Record:
	def __init__(self, fd, folder, meeting_id):
		self.fd, self.folder, self.id = fd, folder, meeting_id

	def check_files(self):
		for name in FILES:
			try:
				fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
			except FileNotFoundError:
				continue
			try:
				regular(fd)
			finally:
				os.close(fd)

	def load(self):
		self.check_files()
		fd = os.open("meeting.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
		with os.fdopen(fd, "r", encoding="utf-8") as stream:
			regular(stream.fileno())
			if os.fstat(stream.fileno()).st_size > 1_000_000:
				raise ValueError("회의 기록 크기 한도입니다")
			state = json.load(stream)
		if (not isinstance(state, dict) or state.get("id") != self.id or state.get("version") != 1
				or not isinstance(state.get("calls"), list) or len(state["calls"]) > MAX_CALLS
				or not isinstance(state.get("absent", []), list)
				or type(state.get("active_seconds")) not in (int, float)
				or not math.isfinite(state["active_seconds"]) or state["active_seconds"] < 0):
			raise ValueError("회의 기록의 ID·버전·예산이 잘못됐습니다")
		state.setdefault("absent", [])
		return state

	def write(self, name, value):
		if name not in FILES[:2]:
			raise ValueError("회의 출력 파일이 아닙니다")
		self.check_files()
		temporary = ".meeting-" + uuid.uuid4().hex
		fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as stream:
				os.fchmod(stream.fileno(), 0o600)
				stream.write(value)
				stream.flush()
				os.fsync(stream.fileno())
			os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
			os.fsync(self.fd)
		finally:
			try:
				os.unlink(temporary, dir_fd=self.fd)
			except FileNotFoundError:
				pass

	def save(self, state):
		if state["id"] != self.id:
			raise ValueError("다른 회의에는 저장할 수 없습니다")
		self.write("meeting.json", json.dumps(state, ensure_ascii=False, allow_nan=False, indent=1) + "\n")
		self.write("minutes.md", minutes(state))


class Store:
	def read(self, meeting_id):
		"""잠금 생성·권한 수정·중단 복구 없이 원자 교체된 상태만 읽는다."""
		root_fd = folder_fd = None
		try:
			identity(meeting_id)
			root_fd = directory(home() / "meetings")
			folder_fd = os.open(meeting_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
			for name in FILES:
				try:
					info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
				except FileNotFoundError:
					continue
				if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
					return None
			fd = os.open("meeting.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
			with os.fdopen(fd, "r", encoding="utf-8") as stream:
				info = os.fstat(stream.fileno())
				if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000:
					return None
				state = json.load(stream)
			if (not isinstance(state, dict) or state.get("id") != meeting_id or state.get("version") != 1
					or state.get("status") not in STATUSES or not isinstance(state.get("agenda"), str)
					or not isinstance(state.get("calls"), list) or len(state["calls"]) > MAX_CALLS
					or not isinstance(state.get("absent", []), list)
					or type(state.get("active_seconds")) not in (int, float)
					or not math.isfinite(state["active_seconds"]) or state["active_seconds"] < 0
					or not isinstance(state.get("cards"), list) or len(state["cards"]) > 5
					or (state.get("summary") is not None and not isinstance(state["summary"], dict))):
				return None
			datetime.fromisoformat(state["created"])
			for card in state["cards"]:
				if (not isinstance(card, dict) or not re.fullmatch(r"q-[a-f0-9]{32}", card.get("id", ""))
						or card.get("status") not in ("waiting", "answered", "confirmed")
						or not isinstance(card.get("questions"), list) or not 1 <= len(card["questions"]) <= 3
						or not isinstance(card.get("answers"), list) or not isinstance(card.get("summary"), str)):
					return None
				if any(not isinstance(a, dict) or not isinstance(a.get("source"), str) for a in card["answers"]):
					return None
				for question in card["questions"]:
					if (not isinstance(question, dict) or not isinstance(question.get("id"), str)
							or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", question["id"])
							or not isinstance(question.get("text"), str) or not isinstance(question.get("reason"), str)
							or type(question.get("allow_delegate")) is not bool
							or not isinstance(question.get("options"), list) or not 2 <= len(question["options"]) <= 3):
						return None
					for option in question["options"]:
						if not isinstance(option, dict) or any(not isinstance(option.get(k), str) for k in ("id", "label", "difference", "reply")):
							return None
					if question.get("recommended") not in {option["id"] for option in question["options"]}:
						return None
			state.setdefault("absent", [])
			return state
		except (OSError, ValueError, TypeError, KeyError, RecursionError):
			return None
		finally:
			for fd in (folder_fd, root_fd):
				if fd is not None:
					os.close(fd)

	def list_states(self):
		"""안전한 회의 ID와 상태 JSON 목록. 잘못된 항목은 다른 회의와 격리한다."""
		try:
			fd = directory(home() / "meetings")
			try:
				names = sorted(name for name in os.listdir(fd) if ID.fullmatch(name))
			finally:
				os.close(fd)
		except (OSError, ValueError):
			return []
		return [state for name in names if (state := self.read(name)) is not None]

	@contextmanager
	def locked(self, meeting_id, *, create=False):
		identity(meeting_id)
		root = home() / "meetings"
		parent = directory(home())
		root_fd = folder_fd = lock_fd = None
		try:
			if create:
				try:
					os.mkdir("meetings", 0o700, dir_fd=parent)
				except FileExistsError:
					pass
			root_fd = os.open("meetings", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
			os.fchmod(root_fd, 0o700)
			if create:
				os.mkdir(meeting_id, 0o700, dir_fd=root_fd)
			folder_fd = os.open(meeting_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
			os.fchmod(folder_fd, 0o700)
			lock_fd = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=folder_fd)
			regular(lock_fd)
			try:
				fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				raise RuntimeError("같은 회의가 실행 중입니다") from None
			record = Record(folder_fd, root / meeting_id, meeting_id)
			record.check_files()
			yield record
		finally:
			for fd in (lock_fd, folder_fd, root_fd, parent):
				if fd is not None:
					os.close(fd)


def journal(state):
	day = datetime.fromisoformat(state["created"]).date()
	path = vault.daily_path(load_sources().vault, "personal", day)
	if not safe(path) or not path.parent.is_dir():
		raise ValueError("기존 개인 일지 착지점이 없거나 안전하지 않습니다")
	body = ("- 회의: " + state["id"] + "\n- 안건: " + clean(state["agenda"]) + "\n"
		+ "- 결론: " + clean(state["summary"]["conclusion"][0]["text"]) + "\n"
		+ f"- 실제 호출 {len(state['calls'])}회 · 읽기 전용 회의 완료 · 구현·반영은 별도 승인\n")
	vault.upsert_block(path, "meeting:" + state["id"], body, "AI 회의", day, "personal")
