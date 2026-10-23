"""완료된 AI 답변에서 최근 작업 기록 블록만 고른다."""

import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import os

from .backfill_common import clean
from .config import classify, home
from .sessions import Session

PLAN = "작업 기록 블록"
HEADING = re.compile(r"^ {0,3}#{1,2}\s+")
RECORD = re.compile(r"^ {0,3}## 작업 기록\s*#*\s*$")


def blocks(session: Session) -> list[str]:
	blocks = []
	for turn in session.turns:
		if turn.role != "assistant" or not turn.complete or turn.channel not in (None, "final"):
			continue
		block, fence = None, None
		for line in turn.text.splitlines():
			match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
			if match:
				mark = match[1]
				if fence is None:
					fence = mark
				elif mark[0] == fence[0] and len(mark) >= len(fence):
					fence = None
			elif fence is None and HEADING.match(line):
				if block and any(re.match(r"\s*- 결정:", value) for value in block[1:]):
					blocks.append("\n".join(block).rstrip())
				block = [line] if RECORD.fullmatch(line) else None
				continue
			if block is not None:
				block.append(line)
		if block and any(re.match(r"\s*- 결정:", value) for value in block[1:]):
			blocks.append("\n".join(block).rstrip())
	return blocks[-10:]


def extract(session: Session) -> str | None:
	return clean("\n\n".join(blocks(session))) or None


def fields(session: Session, name: str) -> list[str]:
	values, fence = [], None
	for line in (blocks(session) or [""])[-1].splitlines():
		mark = re.match(r"^\s*(`{3,}|~{3,})", line)
		if mark:
			if fence is None:
				fence = mark[1]
			elif fence[0] == mark[1][0] and len(mark[1]) >= len(fence):
				fence = None
			continue
		match = re.match(rf"^\s*-\s*{re.escape(name)}\s*:\s*(.*?)\s*$", line)
		if match and fence is None:
			value = clean(match[1]).strip()
			if value.strip("(). -") not in ("", "있으면", "없음", "없습니다", "해당 없음", "완료", "미정"):
				values.append(value)
	return list(dict.fromkeys(values))


def due_hint(sentence: str, base: date) -> str:
	match = re.search(r"(?:(\d{4})[./-])?(\d{1,2})/(\d{1,2})(?!\d)|(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일|오늘|내일|(?:다음\s*)?[월화수목금토일]요일", sentence)
	if not match:
		return ""
	value = match[0]
	try:
		if value in ("오늘", "내일"):
			day = base + timedelta(days=value == "내일")
		elif "요일" in value:
			offset = ("월화수목금토일".index(value[-3]) - base.weekday()) % 7
			day = base + timedelta(days=offset or (7 if value.startswith("다음") else 0))
		else:
			year, month, day = match.group(1, 2, 3) if match[2] else match.group(4, 5, 6)
			day = date(int(year or base.year), int(month), int(day))
			if not year and day < base:
				day = day.replace(year=base.year + 1)
		return day.isoformat()
	except ValueError:
		return ""


def project(session: Session, sources, cls: str) -> str:
	paths = session.cwds + ([session.cwd] if session.cwd else [])
	matches = [repo for repo in sources.repos if repo.cls == cls and any(
		path == repo.path or path.startswith(repo.path + "/") for path in paths)]
	return max(matches, key=lambda repo: len(repo.path)).id if matches else ""


def save(ledger, session: Session, sources, cls: str) -> None:
	# 한 대화의 마지막 기록을 현재 할 일로 보아 예전의 해결된 일을 다시 늘리지 않는다.
	if any((rule := classify(path, sources.rules)) and rule.cls == "company" for path in session.cwds):
		cls = "company"
	ledger.replace_work(session.key, cls, project(session, sources, cls),
		[(value, due_hint(value, session.last.date())) for value in fields(session, "남은 일")],
		" · ".join(fields(session, "결정")), session.last.isoformat(timespec="seconds"))


@contextmanager
def processing(session: Session, sources, cls: str):
	from .backfill_common import child, private_write
	path = child(home(), "run/collector-active.json")
	private_write(path, json.dumps({"pid": os.getpid(), "cls": cls,
		"project_id": project(session, sources, cls), "at": datetime.now().astimezone().isoformat()}))
	try:
		yield
	finally:
		path.unlink(missing_ok=True)
