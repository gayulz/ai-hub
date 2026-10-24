"""자유 문장을 내보내지 않고 로컬 메타데이터 신호만 읽는다."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path

from collector.backfill_common import files, safe
from collector.config import ConfigError, load_sources

from .common import identifier, normal, regular, unavailable

ENGINES = {"codex": "Codex", "agy": "AGY", "claude": "Claude", "ollama": "로컬", "gemini": "Gemini", "nlm": "NotebookLM"}


def recent(value, now: datetime) -> bool:
	at = datetime.fromisoformat(value) if isinstance(value, str) else datetime.fromtimestamp(value, now.tzinfo)
	if not at.tzinfo:
		raise ValueError
	return 0 <= (now - at).total_seconds() <= 600


def sources(root: Path):
	try:
		return load_sources(regular(root / "sources.toml", root))
	except (KeyError, TypeError, AttributeError):
		raise ConfigError("원천 설정 형식을 확인할 수 없습니다") from None


def presence(root: Path, tool: str, now: datetime) -> dict:
	try:
		with regular(root / f"run/presence/{tool}.json", root).open(encoding="utf-8") as source:
			data = json.load(source)
		if data["status"] not in ("일하는 중", "대기", "끝남") or data.get("cls") not in (None, "personal", "company", "study", "career", "exclude"):
			raise ValueError
		fresh = recent(data["at"], now)
		active = fresh and data["status"] == "일하는 중"
		since = datetime.fromisoformat(data.get("since", data["at"]))
		if not since.tzinfo or since > datetime.fromisoformat(data["at"]):
			raise ValueError
		return {"status": "작업 중" if active else "대기", "cls": data.get("cls"),
			"project": identifier(data["project_id"]) if data.get("project_id") else "",
			"updated": data["at"], "duration": max(0, int((now - since).total_seconds())) if fresh else 0,
			"engine": ENGINES[tool]}
	except FileNotFoundError:
		return {"status": "대기", "cls": None, "project": "", "duration": 0}
	except (OSError, ValueError, TypeError, KeyError, AttributeError):
		return unavailable("직접 대화 신호를 읽을 수 없습니다")


def processes() -> dict:
	try:
		result = subprocess.run(["pgrep", "-fl", r"collector|/bin/nlm|serve_outreach"],
			capture_output=True, text=True, timeout=2)
		if result.returncode not in (0, 1):
			raise ValueError
		commands = [m[1] for line in result.stdout.splitlines() if (m := re.match(r"^\s*\d+\s+(.+)$", line))]
		return normal(commands=commands)
	except (OSError, ValueError, subprocess.TimeoutExpired):
		return unavailable("프로세스를 조회할 수 없습니다", commands=[])


def collecting(root: Path, now: datetime) -> dict:
	try:
		with regular(root / "run/collector-active.json", root).open(encoding="utf-8") as source:
			data = json.load(source)
		if type(data["pid"]) is not int or data["pid"] <= 0 or data["cls"] not in ("company", "personal", "study", "career"):
			raise ValueError
		if not recent(data["at"], now):
			return normal(active=False)
		try:
			os.kill(data["pid"], 0)
		except ProcessLookupError:
			return normal(active=False)
		return normal(active=True, cls=data["cls"], project=identifier(data["project_id"]) if data["project_id"] else "", updated=data["at"])
	except FileNotFoundError:
		return normal(active=False)
	except (OSError, ValueError, TypeError, KeyError):
		return unavailable("수집 중인 대화의 분류를 확인할 수 없습니다", active=False)


def blog_logs(folder: Path, kinds: tuple[str, ...], now: datetime) -> dict:
	try:
		if not safe(folder):
			raise ValueError
		if not folder.exists():
			return {"status": "대기"}
		latest = None
		pattern = re.compile(r"-(claude|codex|agy|gemini|ollama|nlm)-(" + "|".join(kinds) + r")[.]log$", re.I)
		for path in folder.iterdir():
			match = pattern.search(path.name)
			if not match:
				continue
			regular(path, folder)
			at = path.stat().st_mtime
			if recent(at, now) and (latest is None or at > latest[0]):
				latest = (at, match[1].lower(), match[2])
		if latest:
			return {"status": "작업 중", "engine": ENGINES[latest[1]], "task": "블로그 작업 · " + latest[2],
				"updated": datetime.fromtimestamp(latest[0], now.tzinfo).isoformat(timespec="seconds")}
		return {"status": "대기"}
	except (OSError, ValueError):
		return unavailable("블로그 작업 로그 시각을 읽을 수 없습니다")


def tistory(blog: Path, now: datetime) -> dict:
	log = blog_logs(blog / ".blogops/logs", ("TISTORY", "TISTORY_PUBLISH"), now)
	if log["status"] != "대기":
		return log
	try:
		with regular(blog / ".blogops/daemon.json", blog).open(encoding="utf-8") as source:
			data = json.load(source)
		# 데몬 실행 자체는 티스토리 작업 신호가 아니어서 명시된 최근 작업만 인정한다.
		if data.get("current_task") in ("TISTORY", "TISTORY_PUBLISH") and recent(data["updated"], now):
			return {"status": "작업 중", "engine": ENGINES.get(data.get("engine"), ""), "task": "티스토리 작업 중", "updated": data["updated"]}
		return {"status": "대기"}
	except FileNotFoundError:
		return {"status": "대기"}
	except (OSError, ValueError, TypeError, KeyError, AttributeError):
		return unavailable("티스토리 작업 상태를 읽을 수 없습니다")


def naver(blog: Path, now: datetime) -> dict:
	try:
		path = blog / ".blogops/blogops.db"
		try:
			path.stat()
		except FileNotFoundError:
			return {"status": "대기"}
		path = regular(path, blog)
		with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
			jobs = conn.execute("SELECT kind, status, started_at, finished_at FROM job_run WHERE kind IN ('STAGE', 'BLOG_RANK')").fetchall()
			events = conn.execute("SELECT platform, to_state, created_at FROM post_event WHERE platform='NAVER' AND to_state IN ('STAGING', 'STAGED')").fetchall()
		matches = []
		for kind, status, started, finished in jobs:
			value = finished or started
			at = datetime.fromisoformat(value)
			if not at.tzinfo:
				raise ValueError
			fresh = recent(finished, now) if finished else 0 <= (now - at).total_seconds() <= 7200
			if fresh:
				matches.append((2 if not finished else 1, at,
					"네이버 편집기 올리기" if kind == "STAGE" else "검색 순위 관측", value))
		for platform, to_state, value in events:
			if recent(value, now):
				matches.append((0, datetime.fromisoformat(value), "네이버 글 올리는 중", value))
		if matches:
			priority, at, task, value = max(matches)
			return {"status": "작업 중", "task": task, "engine": "로컬", "updated": value}
		return {"status": "대기"}
	except (OSError, sqlite3.Error, ValueError, TypeError):
		return unavailable("네이버 작업 상태를 읽을 수 없습니다")


def shorts(user_home: Path, now: datetime) -> dict:
	try:
		root = user_home / ".codex/sessions"
		if not safe(root):
			raise ValueError
		projects = user_home / ".codex/.chatgpt-projects"
		main, auxiliary = [], []
		folders = [root] if root.is_dir() else []
		while folders:
			with os.scandir(folders.pop()) as entries:
				for entry in entries:
					if entry.name.startswith("🔐") or entry.is_symlink():
						continue
					if entry.is_dir(follow_symlinks=False):
						folders.append(entry.path)
						continue
					if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
						continue
					modified = entry.stat(follow_symlinks=False).st_mtime
					if not recent(modified, now):
						continue
					# 대화 본문은 읽지 않고 첫 줄의 작업 폴더·스레드 종류만 읽는다.
					with Path(entry.path).open(encoding="utf-8") as source:
						line = source.readline(65537)
					if len(line) > 65536:
						raise ValueError
					meta = json.loads(line)
					if not isinstance(meta, dict) or not isinstance(meta.get("payload"), dict):
						raise ValueError
					payload = meta["payload"]
					cwd = Path(payload.get("cwd", "/"))
					if meta.get("type") != "session_meta" or not cwd.is_relative_to(projects) or any(p.startswith("🔐") for p in cwd.parts):
						continue
					kind = payload.get("source")
					at = datetime.fromtimestamp(modified, now.tzinfo).isoformat(timespec="seconds")
					if isinstance(kind, str):
						main.append(at)
					elif isinstance(kind, dict):
						auxiliary.append(at)
		return normal(main=max(main, default=None), auxiliary=max(auxiliary, default=None))
	except (OSError, ValueError, TypeError, AttributeError):
		return unavailable("영상 프로젝트의 대화 메타데이터를 읽을 수 없습니다")


def notebooklm(user_home: Path) -> dict:
	try:
		root = user_home / ".notebooklm-mcp-cli"
		if not safe(root):
			raise ValueError
		# 인증 파일의 존재만 확인한다. 토큰과 쿠키의 내용은 열지 않는다.
		found = any(path.name in ("storage_state.json", "auth.json", "credentials.json", "token.json", "tokens.json") for path in files(root))
		return {"status": "가동 중" if found else "로그인 필요"}
	except (OSError, ValueError):
		return unavailable("NotebookLM 인증 파일의 존재를 확인할 수 없습니다")
