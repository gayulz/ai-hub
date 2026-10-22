"""Gemini CLI와 AGY 대화 읽기.

- Gemini CLI: ~/.gemini/tmp/<이름>/chats/session-*.json (본문 있음)
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
AUTO_CALL_MAX_STEPS = 2  # 이하이면 한 번 묻고 끝나는 자동 호출로 보고 제외(정본 계획 2절)


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


def read_gemini(since: datetime) -> list[Session]:
	root = GEMINI_HOME / "tmp"
	if not root.exists():
		return []
	names = _gemini_names()
	since_ts = (since - timedelta(days=1)).timestamp()
	out: list[Session] = []
	for path in sorted(root.glob("*/chats/session-*.json")):
		if path.stat().st_mtime < since_ts:
			continue
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
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
			out.append(sess)
	return out


def _agy_cwd(uris: str) -> str | None:
	try:
		first = json.loads(uris or "[]")[0]
	except (ValueError, IndexError, TypeError):
		return None
	return unquote(urlparse(first).path) if first else None


def read_agy(since: datetime) -> tuple[list[Session], list[str]]:
	"""(포함할 대화, 자동 호출로 제외한 대화의 제목 목록). 제외 목록은 첫 실행 때 사용자가 확인한다."""
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
	excluded: list[str] = []
	for cid, title, preview, steps, modified, uris, agent in rows:
		last = parse_time(modified.replace(" ", "T", 1))
		if last is None:
			continue
		if steps <= AUTO_CALL_MAX_STEPS or agent:
			excluded.append(title or cid)
			continue
		sess = Session("agy", cid, _agy_cwd(uris), last, last, partial=True, title=title or "")
		if preview:
			sess.turns.append(Turn("user", preview))
		included.append(sess)
	return included, excluded
