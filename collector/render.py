"""일지 덩어리 본문 만들기. 모든 본문은 비밀값 제거를 거친 뒤 여기서 만든다."""

from __future__ import annotations

from datetime import datetime

from .redact import redact
from .repos import Commit
from .sessions import Session

TOOL_LABEL = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "agy": "AGY"}
FIRST_LINE_LIMIT = 80


def _hm(when: datetime) -> str:
	return when.strftime("%H:%M")


def first_question(session: Session) -> str:
	for turn in session.turns:
		if turn.role == "user":
			line = redact(turn.text.strip().splitlines()[0]) if turn.text.strip() else ""
			return line[:FIRST_LINE_LIMIT]
	return session.title[:FIRST_LINE_LIMIT]


def session_body(session: Session, summary: str | None, plan_id: str | None, reason: str = "") -> str:
	label = TOOL_LABEL.get(session.tool, session.tool)
	span = f"{_hm(session.started)}–{_hm(session.last)}"
	if summary:
		bullets = "\n".join(f"  {line.strip()}" if line.strip().startswith("-") else f"  - {line.strip()}"
			for line in redact(summary).splitlines() if line.strip())
		head = f"- **{span} · {label}** · 사용자 메시지 {session.user_count}개 _(요금제: {plan_id})_"
		return f"{head}\n{bullets}"
	note = f"요약 없음 — {reason}" if reason else "요약 없음"
	if session.partial:
		note += ", 제목·미리보기만 기록"
	head = f"- **{span} · {label}** · 사용자 메시지 {session.user_count}개 _({note})_"
	question = first_question(session)
	return f"{head}\n  - 첫 질문: {question}" if question else head


def commit_body(commits: list[Commit]) -> str:
	return "\n".join(f"- {_hm(c.when)} 커밋 `{c.sha}` {redact(c.subject)}" for c in commits)


def docs_body(paths: list[str]) -> str:
	return "\n".join(f"- 문서 변경: `{p}`" for p in paths)


def check_body(now: datetime, unrecorded: int, unclassified: int, unregistered: int, waiting: int) -> str:
	return (f"- 점검 {_hm(now)} — 미기록 원천 {unrecorded}건 · 미분류 {unclassified}건 · "
		f"미등록 저장소 {unregistered}곳 · 요약 대기 {waiting}건")
