"""Claude Code와 Codex 대화 읽기.

모든 원천은 같은 모양(Session)으로 돌려준다. 대화에서 남기는 것은
사용자 질문과 AI의 텍스트 답변뿐이다(도구 실행 결과·생각 과정 제외).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
# 사용자 메시지 중 도구가 자동으로 넣는 태그 블록은 질문이 아니다.
INJECTED = re.compile(r"^\s*<(system-reminder|command-|local-command|task-notification|environment_context)")
CODEX_INJECTED = re.compile(r"^\s*(# AGENTS\.md instructions|<)")
SKIP_CODEX_THREADS = ("subagent", "guardian_review")


@dataclass
class Turn:
	role: str  # "user" | "assistant"
	text: str
	channel: str | None = None
	message_id: str | None = None
	complete: bool = True


@dataclass
class Session:
	tool: str
	sid: str
	cwd: str | None
	started: datetime
	last: datetime
	turns: list[Turn] = field(default_factory=list)
	partial: bool = False  # 본문 없이 제목·미리보기만 있는 원천(AGY)
	title: str = ""
	cwds: list[str] = field(default_factory=list)

	@property
	def key(self) -> str:
		return f"{self.tool}:{self.sid}"

	@property
	def user_count(self) -> int:
		return sum(1 for t in self.turns if t.role == "user")


def parse_time(value: str | None) -> datetime | None:
	if not value:
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
	except ValueError:
		return None


def _text_of(content) -> str:
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
		return "\n".join(p for p in parts if p)
	return ""


def _claude_file(path: Path, since_ts: float, text: str | None = None) -> list[Session]:
	if text is None and path.stat().st_mtime < since_ts:
		return []
	groups: dict[str, Session] = {}
	for line in (text if text is not None else path.read_text(encoding="utf-8", errors="replace")).splitlines():
		try:
			rec = json.loads(line)
		except json.JSONDecodeError:
			continue
		kind = rec.get("type")
		# 사람이 연 CLI 대화만: sdk-cli(claude -p 등)와 하위 에이전트는 제외
		if kind not in ("user", "assistant") or rec.get("entrypoint") != "cli":
			continue
		if rec.get("isSidechain") or rec.get("isMeta"):
			continue
		sid = rec.get("sessionId") or path.stem
		when = parse_time(rec.get("timestamp"))
		if when is None:
			continue
		sess = groups.get(sid)
		if sess is None:
			sess = groups[sid] = Session("claude", sid, rec.get("cwd"), when, when)
		sess.started = min(sess.started, when)
		sess.last = max(sess.last, when)
		if rec.get("cwd") and rec["cwd"] not in sess.cwds:
			sess.cwds.append(rec["cwd"])
		content = (rec.get("message") or {}).get("content")
		if kind == "user":
			text = _text_of(content)  # tool_result만 있는 줄은 빈 문자열이 된다
			if text and not INJECTED.match(text):
				sess.turns.append(Turn("user", text))
		else:
			text = _text_of(content)
			if text:
				message = rec.get("message") or {}
				complete = message.get("stop_reason") in (None, "end_turn", "stop_sequence")
				if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content):
					complete = False
				sess.turns.append(Turn("assistant", text, message_id=message.get("id"), complete=complete))
	return [s for s in groups.values() if s.user_count > 0]


def read_claude(since: datetime) -> list[Session]:
	if not CLAUDE_ROOT.exists():
		return []
	since_ts = (since - timedelta(days=1)).timestamp()
	out: list[Session] = []
	for path in sorted(CLAUDE_ROOT.glob("*/*.jsonl")):
		out.extend(_claude_file(path, since_ts))
	return out


def _codex_file(path: Path, text: str | None = None) -> Session | None:
	meta: dict = {}
	sess: Session | None = None
	for line in (text if text is not None else path.read_text(encoding="utf-8", errors="replace")).splitlines():
		try:
			rec = json.loads(line)
		except json.JSONDecodeError:
			continue
		payload = rec.get("payload") or {}
		if rec.get("type") == "session_meta" and not meta:
			meta = payload
			if meta.get("originator") == "codex_exec" or meta.get("thread_source") in SKIP_CODEX_THREADS:
				return None
			when = parse_time(meta.get("timestamp") or rec.get("timestamp"))
			if when is None:
				return None
			sess = Session("codex", meta.get("id") or path.stem, meta.get("cwd"), when, when)
			if meta.get("cwd"):
				sess.cwds.append(meta["cwd"])
			continue
		if sess is not None and payload.get("cwd") and payload["cwd"] not in sess.cwds:
			sess.cwds.append(payload["cwd"])
		if sess is None or rec.get("type") != "response_item" or payload.get("type") != "message":
			continue
		when = parse_time(rec.get("timestamp"))
		if when:
			sess.last = max(sess.last, when)
		# Codex 항목 타입은 input_text·output_text라 _text_of(type == "text")를 쓸 수 없다
		text = "\n".join(c["text"] for c in payload.get("content") or []
			if isinstance(c, dict) and c.get("text"))
		if not text:
			continue
		role = payload.get("role")
		if role == "user" and not CODEX_INJECTED.match(text):
			sess.turns.append(Turn("user", text))
		elif role == "assistant":
			channel = payload.get("channel") or payload.get("phase")
			sess.turns.append(Turn("assistant", text, "final" if channel == "final_answer" else channel,
				payload.get("id"), payload.get("status") != "incomplete"))
	if sess is None or sess.user_count == 0:
		return None
	return sess


def read_codex(since: datetime) -> list[Session]:
	if not CODEX_ROOT.exists():
		return []
	cutoff = (since - timedelta(days=1)).date()
	out: list[Session] = []
	for path in sorted(CODEX_ROOT.glob("*/*/*/*.jsonl")):
		try:
			day = datetime(*(int(p) for p in path.parts[-4:-1])).date()
		except ValueError:
			continue
		if day < cutoff:
			continue  # 폴더 날짜(YYYY/MM/DD)로 먼저 거른다. 7GB 전체를 읽지 않게
		sess = _codex_file(path)
		if sess:
			out.append(sess)
	return out
