"""보관함의 대화만 읽고 질문·마지막 완료 답을 고른다."""

from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import archive, pipeline, sessions, sessions_other, summarize
from .backfill_common import child, clean, files, safe, sha
from .config import Repo, Sources, classify, home
from .sessions import Session, Turn, parse_time

DUMP_LINE = re.compile(r"^\s*(?:diff --git|@@ |\{\s*$|import \w|def \w)")


def read_text(path: Path) -> str:
	if path.name.endswith(".zst"):
		try:
			from compression import zstd
		except ImportError as exc:
			raise ValueError("현재 Python에 표준 zstd 해제가 없어 보류") from exc
		with zstd.open(path, "rt", encoding="utf-8") as source:
			return source.read()
	if path.name.endswith(".gz"):
		with gzip.open(path, "rt", encoding="utf-8") as source:
			return source.read()
	return path.read_text(encoding="utf-8")


def read(path: Path, tool: str, names: dict[str, str]) -> list[Session]:
	text = read_text(path)
	original = Path(path.name.removesuffix(".gz"))
	if tool == "claude":
		return sessions._claude_file(original, 0, text)
	if tool == "codex":
		result = sessions._codex_file(original, text)
		if result:
			stamps = [parse_time(json.loads(line).get("timestamp")) for line in text.splitlines()
				if line.strip().startswith("{")]
			result.last = max([result.last] + [s for s in stamps if s])
		return [result] if result else []
	data = sessions_other._gemini_data(original, text)
	if data.get("kind") == "subagent":
		return []
	started, last = parse_time(data.get("startTime")), parse_time(data.get("lastUpdated"))
	if started is None:
		return []
	cwd = data.get("cwd") or data.get("projectPath") or names.get(path.parts[-3])
	session = Session("gemini", data.get("sessionId") or original.stem, cwd, started, last or started)
	if cwd:
		session.cwds.append(cwd)
	for message in data.get("messages", []):
		text = message.get("content")
		if not isinstance(text, str) or not text.strip():
			continue
		kind = message.get("type")
		if kind == "user" and not sessions.INJECTED.match(text):
			session.turns.append(Turn("user", text))
		elif kind == "gemini":
			session.turns.append(Turn("assistant", text, complete=not bool(message.get("toolCalls"))))
	return [session] if session.user_count else []


def names() -> dict[str, str]:
	# 원문은 보관함에서만 읽는다. 프로젝트 이름 대응표는 경로 분류용 메타데이터다.
	try:
		return sessions_other._gemini_names()
	except (OSError, ValueError):
		return {}


def select(session: Session) -> list[Turn]:
	last_question = max((i for i, t in enumerate(session.turns) if t.role == "user"), default=-1)
	answers = [(i, t) for i, t in enumerate(session.turns) if i > last_question and
		t.role == "assistant" and t.complete and t.channel not in ("analysis", "commentary")]
	if not answers:
		return []
	finals = [(i, t) for i, t in answers if t.channel == "final"]
	index, final = (finals or answers)[-1]
	parts = [final.text]
	if final.message_id:
		for turn in reversed(session.turns[last_question + 1:index]):
			if turn.message_id != final.message_id or turn.role != "assistant":
				break
			parts.insert(0, turn.text)
	return [t for t in session.turns if t.role == "user"] + [replace(final, text="\n".join(parts))]


def prompts(session: Session) -> tuple[list[str], str]:
	selected = select(session)
	if not selected:
		return [], "마지막 사용자 질문 뒤의 완료 답이 없음"
	lines = []
	for turn in selected:
		text = re.sub(r"```.*?(?:```|\Z)", "", turn.text, flags=re.S).strip()
		# 파일 본문·큰 붙여넣기는 대화 전체를 보류하지 않고 그 부분만 AI 입력에서 뺀다.
		# 질문은 앞 설명 20줄만 남기고, 마지막 답은 길어도 본문이라 코드 같은 줄만 뺀다.
		kept = [line for line in text.splitlines() if not DUMP_LINE.match(line)]
		if turn.role == "user" and (text.count("\n") > 60 or len(kept) < text.count("\n") + 1):
			text = "\n".join(kept[:20]).strip() + "\n(긴 붙여넣기·파일 본문 생략)"
		elif turn.role != "user":
			text = "\n".join(kept).strip()
		if text:
			lines.append(("[사용자 질문] " if turn.role == "user" else "[AI 마지막 완료 답] ") + clean(text))
	if not lines or not lines[-1].startswith("[AI 마지막 완료 답]"):
		return [], "완료 답에서 코드·파일 덤프를 제외한 본문이 없음"
	body = "\n".join(lines)
	room = summarize.MAX_PROMPT_CHARS - len(summarize.PROMPT_HEAD) - len(summarize.PROMPT_TAIL) - 80
	chunks = [body[i:i + room] for i in range(0, len(body), room)]
	return [summarize.PROMPT_HEAD + f"부분 {i + 1}/{len(chunks)} (전체 결과가 모이기 전에는 부분 기록)\n"
		+ part + summarize.PROMPT_TAIL for i, part in enumerate(chunks)], ""


def project_reason(session: Session, repo: Repo, sources: Sources) -> tuple[bool, str]:
	paths = {os.path.normpath(p) for p in session.cwds + ([session.cwd] if session.cwd else [])}
	root = Path(os.path.normpath(repo.path))
	belongs = any(Path(p).is_relative_to(root) for p in paths)
	if not belongs:
		return False, "경로 미확인" if not paths else "다른 프로젝트"
	for path in paths:
		rule = classify(path, sources.rules)
		if any(p.startswith("🔐") for p in Path(path).parts):
			return True, "비밀 경로 제외"
		if not Path(path).is_relative_to(root) or rule is None or rule.cls != repo.cls:
			return True, "대화의 작업 경로 분류가 미확인 또는 충돌"
	return True, ""


def inventory(repo: Repo, sources: Sources, since: date, until: datetime) -> dict:
	root = home() / "archive"
	tasks, warnings, unknown, deferred = {}, [], 0, []
	project_names = names()
	for tool in ("claude", "codex", "gemini"):
		for path in files(root / tool):
			if not path.name.endswith((".jsonl.gz", ".json.gz", ".jsonl.zst")):
				continue
			rel = path.relative_to(root).as_posix()
			if "subagents" in path.relative_to(root / tool).parts:
				continue
			try:
				found = read(path, tool, project_names)
			except (OSError, ValueError, EOFError, TypeError, AttributeError) as exc:
				reason = f"보관함 형식·조회 보류: {type(exc).__name__}"
				warnings.append(f"{reason} {clean(rel)}")
				deferred.append({"id": sha(rel), "source": clean(rel), "reason": reason})
				continue
			for session in found:
				paths = session.cwds + ([session.cwd] if session.cwd else [])
				if not isinstance(session.sid, str) or any(not isinstance(p, str) for p in paths):
					deferred.append({"id": sha(rel), "source": clean(rel), "reason": "대화 메타데이터 형식 미확인"})
					unknown += 1
					continue
				session.started = session.started.astimezone(ZoneInfo("Asia/Seoul"))
				session.last = session.last.astimezone(ZoneInfo("Asia/Seoul"))
				if session.started.date() < since or session.started > until:
					continue
				belongs, reason = project_reason(session, repo, sources)
				if not belongs:
					unknown += reason == "경로 미확인"
					if reason == "경로 미확인":
						deferred.append({"id": clean(session.key), "source": clean(rel), "reason": reason})
					continue
				if session.last > until - timedelta(minutes=pipeline.ACTIVE_MINUTES):
					reason = "고정 종료 시각 기준 활동 중인 대화"
				parts, prompt_reason = prompts(session)
				digest = pipeline.digest(session)
				task = {"key": session.key, "hash": digest, "type": "session", "archive": clean(rel),
					"tool": tool, "sid": clean(session.sid), "day": session.last.date().isoformat(),
					"started": session.started.isoformat(), "last": session.last.isoformat(),
					"material": sha("".join(parts)), "parts": len(parts), "reason": reason or prompt_reason}
				if clean(rel) != rel or not re.fullmatch(r"[\w.-]{1,200}", session.sid):
					task["key"] = tool + ":held-" + sha(session.key)
					task["reason"] = "원천 식별자에 보안값이 있어 자동 처리 보류"
				old = tasks.get(session.key)
				if old and old["hash"] != digest:
					task["reason"] = "같은 대화 ID의 보관함 사본 내용이 서로 다름"
				tasks[task["key"]] = task
	return {"tasks": sorted(tasks.values(), key=lambda t: (t["started"], t["key"])),
		"warnings": warnings, "unclassified": unknown, "deferred": deferred}


def load(task: dict) -> Session:
	path = child(home() / "archive", task["archive"])
	for session in read(path, task["tool"], names()):
		if session.key == task["key"]:
			return session
	raise ValueError("고정 목록의 대화를 보관함에서 찾을 수 없습니다")


def original_missing(task: dict) -> bool:
	tool, rel = task["archive"].split("/", 1)
	path = archive.ROOTS[tool] / rel.removesuffix(".gz")
	return not safe(path) or not path.exists()


def archive_stale(task: dict) -> bool:
	tool, rel = task["archive"].split("/", 1)
	source = archive.ROOTS[tool] / rel.removesuffix(".gz")
	if not safe(source) or not source.exists():
		return False
	return source.stat().st_mtime_ns != child(home() / "archive", task["archive"]).stat().st_mtime_ns
