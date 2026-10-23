"""Gemini CLI와 AGY 대화 읽기.

- Gemini CLI: ~/.gemini/tmp/<이름>/chats/session-*.json·jsonl (본문 있음)
- AGY: conversation_summaries.db의 제목·미리보기만 쓴다. 대화 본문은 protobuf
  블롭이라 C1에서는 다루지 않는다(C3 이후 과제).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from .sessions import INJECTED, Session, Turn, parse_time

GEMINI_HOME = Path.home() / ".gemini"
AGY_DB = GEMINI_HOME / "antigravity-cli" / "conversation_summaries.db"


def _gemini_names() -> dict[str, str]:
	"""프로젝트 이름 → 작업 경로. projects.json은 경로 → 이름으로 적혀 있다."""
	path = GEMINI_HOME / "projects.json"
	if not path.exists():
		return {}
	mapping = json.loads(path.read_text(encoding="utf-8")).get("projects", {})
	names: dict[str, str] = {}
	for cwd, name in mapping.items():
		names.setdefault(name, cwd)
	return names


def _gemini_data(path: Path, text: str) -> dict:
	"""JSONL을 기존 JSON의 메타데이터·메시지 배열 형태로 읽는다."""
	if path.suffix != ".jsonl":
		return json.loads(text)
	data: dict = {"messages": []}
	for line in text.splitlines():
		try:
			row = json.loads(line)
		except json.JSONDecodeError:
			continue  # 쓰는 중인 줄이 깨져도 나머지는 읽는다
		if not isinstance(row, dict):
			continue
		if "type" not in row and "startTime" in row:
			data.update(row)
		elif isinstance(row.get("$set"), dict):
			if "lastUpdated" in row["$set"]:
				data["lastUpdated"] = row["$set"]["lastUpdated"]
		elif row.get("type") in ("user", "gemini"):
			if isinstance(row.get("content"), list):
				row["content"] = "\n".join(part["text"] for part in row["content"]
					if isinstance(part, dict) and isinstance(part.get("text"), str))
			data["messages"].append(row)
	return data


def read_gemini(since: datetime) -> list[Session]:
	root = GEMINI_HOME / "tmp"
	if not root.exists():
		return []
	names = _gemini_names()
	since_ts = (since - timedelta(days=1)).timestamp()
	out: dict[str, Session] = {}
	for path in sorted([*root.glob("*/chats/session-*.json"), *root.glob("*/chats/session-*.jsonl")]):
		if path.stat().st_mtime < since_ts:
			continue
		try:
			data = _gemini_data(path, path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			continue
		if data.get("kind") == "subagent":
			continue  # 하위 에이전트는 제외
		started = parse_time(data.get("startTime"))
		last = parse_time(data.get("lastUpdated")) or started
		if started is None or last is None:
			continue
		sess = Session("gemini", data.get("sessionId") or path.stem,
			names.get(path.parts[-3]), started, last)
		for msg in data.get("messages", []):
			text = msg.get("content")
			if not isinstance(text, str) or not text.strip():
				continue
			if msg.get("type") == "user" and not INJECTED.match(text):
				sess.turns.append(Turn("user", text))
			elif msg.get("type") == "gemini":
				sess.turns.append(Turn("assistant", text))
		if sess.user_count > 0:
			old = out.get(sess.sid)
			if old is None or sess.last > old.last:
				out[sess.sid] = sess
	return list(out.values())


def _agy_cwd(uris: str) -> str | None:
	try:
		first = json.loads(uris or "[]")[0]
	except (ValueError, IndexError, TypeError):
		return None
	return unquote(urlparse(first).path) if first else None


def read_agy(since: datetime) -> tuple[list[Session], list[str]]:
	"""(모든 대화, 제외 목록). AGY는 사람이 요청하는 경우가 거의 없어 자동 호출도 포함한다(2026-10-22 결정). 제외 목록은 항상 비운다."""
	if not AGY_DB.exists():
		return [], []
	since_utc = (since - timedelta(days=1)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
	conn = sqlite3.connect(f"file:{AGY_DB}?mode=ro", uri=True)
	try:
		rows = conn.execute(
			"select conversation_id, title, preview, step_count, last_modified_time, workspace_uris, agent_name "
			"from conversation_summaries where last_modified_time >= ?",
			(since_utc,),
		).fetchall()
	finally:
		conn.close()
	included: list[Session] = []
	excluded: list[str] = []  # 자동 호출 제외는 하지 않는다
	for cid, title, preview, _steps, modified, uris, _agent in rows:
		last = parse_time(modified.replace(" ", "T", 1))
		if last is None:
			continue
		sess = Session("agy", cid, _agy_cwd(uris), last, last, partial=True, title=title or "")
		if preview:
			sess.turns.append(Turn("user", preview))
		included.append(sess)
	return included, excluded
