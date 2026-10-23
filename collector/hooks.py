"""훅의 상태·끝 신호. 원문을 열거나 AI를 호출하지 않는다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

EVENTS = {
	"claude": {"UserPromptSubmit": ("일하는 중", None), "Stop": ("대기", None), "SessionEnd": ("끝남", 0)},
	"codex": {"SessionStart": ("일하는 중", None), "PreToolUse": ("일하는 중", None), "Stop": ("대기", 30)},
	"gemini": {"BeforeTool": ("일하는 중", None), "AfterAgent": ("대기", 30)},
}
MARKER = re.compile(r"(claude|codex|gemini)-([a-f0-9]{64})\.json")


def session_hash(key: str) -> str:
	return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class Pending:
	path: Path
	text: str
	due: datetime


def read_pending() -> dict[str, Pending]:
	from .backfill_common import child
	from .config import ConfigError, home
	result = {}
	try:
		folder = child(home(), "run/pending")
		for path in folder.iterdir():
			match = MARKER.fullmatch(path.name)
			if not match:
				continue
			try:
				path = child(folder, path.name)
				if not path.is_file():
					continue
				text = path.read_text(encoding="utf-8")
				data = json.loads(text)
				due = datetime.fromisoformat(data["due"])
				if data["session_hash"] == match[2] and data["tool"] == match[1] and due.tzinfo:
					result[match[2]] = Pending(path, text, due)
			except (OSError, ValueError, TypeError, KeyError, ConfigError):
				continue
	except (OSError, ValueError, ConfigError):
		pass
	return result


def acknowledge(marker: Pending) -> None:
	from .backfill_common import child, locked
	from .config import ConfigError, home
	try:
		with locked("hook:" + marker.path.stem):
			path = child(home(), "run/pending/" + marker.path.name)
			if path.read_text(encoding="utf-8") == marker.text:
				path.unlink()
	except (OSError, ConfigError):
		pass


def kickstart(now: datetime) -> None:
	import getpass
	from .backfill_common import child, locked, private_write
	from .config import home
	with locked("hook-kick"):
		path = child(home(), "run/hook-kick.json")
		try:
			last = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"])
			if now - last < timedelta(minutes=10):
				return
		except (OSError, ValueError, KeyError, TypeError):
			pass
		label = os.environ.get("AI_HUB_COLLECTOR_LABEL", f"com.{getpass.getuser()}.ai-hub-collector")
		if not re.fullmatch(r"[a-zA-Z0-9_.-]+", label):
			return
		# 기다리지 않고 띄운다. 훅 시간 예산 안에 끊기면 수집기가 깨지 않기 때문이다.
		subprocess.Popen(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
			stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		private_write(path, json.dumps({"at": now.isoformat()}))


def receive(tool: str, data: dict, now: datetime) -> None:
	from .backfill_common import child, locked, private_write
	from .config import classify, home, load_sources
	if not isinstance(data, dict):
		return
	sid, cwd, event = (data.get(key) for key in ("session_id", "cwd", "hook_event_name"))
	if not all(isinstance(value, str) and value and len(value) <= 4096 and "\0" not in value
			for value in (sid, cwd, event)) or event not in EVENTS[tool]:
		return
	status, delay = EVENTS[tool][event]
	cls, project_id = None, None
	try:
		sources = load_sources()
		rule = classify(cwd, sources.rules)
		cls = rule.cls if rule else None
		matches = [repo for repo in sources.repos if cwd == repo.path or cwd.startswith(repo.path + "/")]
		if matches:
			project_id = max(matches, key=lambda repo: len(repo.path)).id
	except Exception:
		pass
	digest = session_hash(f"{tool}:{sid}")
	stem = f"{tool}-{digest}"
	with locked("hook:" + stem):
		presence = child(home(), f"run/presence/{tool}.json")
		since = now.isoformat()
		try:
			previous = json.loads(presence.read_text(encoding="utf-8"))
			if all(previous.get(k) == v for k, v in (("status", status), ("cls", cls), ("project_id", project_id))):
				value = datetime.fromisoformat(previous.get("since", previous["at"]))
				last = datetime.fromisoformat(previous["at"])
				if value.tzinfo and last.tzinfo and value <= last <= now and now - last <= timedelta(minutes=10):
					since = value.isoformat()
		except (OSError, ValueError, TypeError, KeyError, AttributeError):
			pass
		private_write(presence, json.dumps({"status": status, "at": now.isoformat(),
			"since": since, "session_hash": digest, "cls": cls, "project_id": project_id}, ensure_ascii=False))
		pending = child(home(), f"run/pending/{stem}.json")
		if delay is not None:
			private_write(pending, json.dumps({"tool": tool, "session_hash": digest,
				"at": now.isoformat(), "due": (now + timedelta(minutes=delay)).isoformat()}))
		elif status == "일하는 중" and pending.is_file():
			pending.unlink()
	if delay == 0:
		kickstart(now)


class HookTimeout(BaseException):
	"""훅의 시간 예산 소진. 도구에는 오류를 전파하지 않는다."""


def _timeout(_signum, _frame):
	raise HookTimeout


def main(argv: list[str]) -> int:
	if len(argv) != 1 or argv[0] not in EVENTS:
		return 0
	previous = signal.signal(signal.SIGALRM, _timeout)
	signal.setitimer(signal.ITIMER_REAL, 0.12)
	try:
		payload = sys.stdin.read(65537)
		if len(payload) <= 65536:
			receive(argv[0], json.loads(payload), datetime.now().astimezone())
	except (Exception, HookTimeout):
		pass
	finally:
		signal.setitimer(signal.ITIMER_REAL, 0)
		signal.signal(signal.SIGALRM, previous)
	return 0
