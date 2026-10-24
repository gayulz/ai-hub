"""결재함의 세 동작만 잠금 안에서 재확인하고 로컬에 반영한다."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector.config import ConfigError, home
from collector.redact import redact
from collector.redact_pii import redact_pii
from orchestrator import targets

from . import drafts, meetings
from .agents import read_runs
from .common import action_id, regular
from .ledger import read_ledger, read_r1
from .scene import scene

REPO = Path(__file__).resolve().parent.parent
COMMANDS = {"c4_apply": ("collector", "backfill", "--apply"),
	"run_apply": ("orchestrator", "apply"), "r1_apply": ("vaultreorg", "apply", "--folder")}
LABELS = {"c4_apply": "C4 묶음", "run_apply": "오케스트레이션 실행", "r1_apply": "R1 폴더"}


def result(ok: bool, message: str, output: str = "", restart_needed: bool = False) -> dict:
	return {"ok": ok, "message": message, "output": output, "restart_needed": restart_needed}


@contextmanager
def private_file(root: Path, directory: str, name: str):
	folder = root / directory
	if folder.is_symlink():
		raise ValueError("기록 폴더는 링크일 수 없습니다")
	folder.mkdir(mode=0o700, parents=True, exist_ok=True)
	folder.chmod(0o700)
	fd = os.open(folder / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "a", encoding="utf-8") as stream:
		os.fchmod(stream.fileno(), 0o600)
		yield stream


def pending(root: Path, action: str, identity: str) -> bool:
	if action == "c4_apply":
		group = read_ledger(root, datetime.now().date().isoformat())["batches"]
		return any(row["id"] == identity and row["prepared"] > 0 for row in group["items"])
	if action == "r1_apply":
		return any(row["id"] == identity and row["pending"] > 0 for row in read_r1(root)["items"])
	if not any(row["id"] == identity and row["status"] == "승인 필요" for row in read_runs(root)["items"]):
		return False
	with regular(root / "runs" / identity / "run.json", root).open(encoding="utf-8") as source:
		run = json.load(source)
	# 다른 저장소의 실행은 사무실의 반영 대상이 아니다.
	return Path(run["repo"]).resolve() == REPO and run["status"] == "승인 필요"


def git(*args: str, env: dict) -> str:
	return subprocess.run(["git", *args], cwd=REPO, env=env, capture_output=True,
		text=True, encoding="utf-8", errors="replace", timeout=5, check=True, shell=False).stdout.strip()


def execute(action: str, identity: str, audit) -> tuple[int, dict]:
	started = time.monotonic()
	env = {**os.environ, "PYTHONPATH": str(REPO)}
	code, output, restart, before = -1, "", False, None
	status, message = 500, "반영 명령을 실행하지 못했습니다. 로컬 기록을 확인하세요."
	try:
		if action == "run_apply":
			before = git("rev-parse", "--verify", "HEAD", env=env)
		completed = subprocess.run([sys.executable, "-m", *COMMANDS[action], identity], cwd=REPO, env=env,
			stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
			timeout=600, shell=False)
		code, output = completed.returncode, completed.stdout
		status = 200 if code == 0 else 500
		message = LABELS[action] + (" 반영을 마쳤습니다." if code == 0 else f" 반영 실패(종료 코드 {code}). 마지막 출력을 확인하세요.")
	except subprocess.TimeoutExpired as exc:
		code, status, output = 124, 504, exc.output or ""
		message = "반영 시간이 제한을 넘었습니다. 일부 반영 여부를 확인하세요."
	except (OSError, subprocess.CalledProcessError):
		pass
	if before is not None:
		try:
			restart = bool(git("diff", "--name-only", "-z", before, "HEAD", "--", "office/", env=env))
		except (OSError, subprocess.SubprocessError):
			restart = True
			message += " 사무실 변경 여부를 확인할 수 없어 재시작 확인이 필요합니다."
	if isinstance(output, bytes):
		output = output.decode("utf-8", errors="replace")
	try:
		output = "\n".join(redact_pii(redact(output)).splitlines()[-20:])
	except (OSError, ValueError, TypeError):
		output = "출력 가림 처리를 하지 못해 내용을 숨겼습니다."
	lines = output.splitlines()
	if action == "run_apply" and lines and lines[-1].startswith("재기동 필요: "):
		message += "\n" + lines[-1]
	ok = code == 0
	row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "id": identity,
		"exit_code": code, "seconds": round(time.monotonic() - started, 3), "ok": ok, "message": message.splitlines()[0]}
	try:
		audit.write(json.dumps(row, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		status, ok = 500, False
		message += " 활동 기록 저장에 실패했습니다. 다시 반영하기 전에 결과를 확인하세요."
	return status, result(ok, message, output, restart)


def apply(payload) -> tuple[int, dict]:
	try:
		if not isinstance(payload, dict):
			raise ValueError
		action = payload.get("action")
		if not isinstance(action, str) or action not in (*COMMANDS, *drafts.ACTIONS, *meetings.ACTIONS):
			raise ValueError
		keys = {"action", "id", "text"} if action in ("dev_draft", "dev_redraft") else {"action", "id"}
		if action == "dev_draft":
			keys.update(("target", "origin"))
		if action in meetings.ACTIONS:
			keys = meetings.KEYS[action]
		if set(payload) != keys:
			raise ValueError
		identity = action_id(action, payload["id"])
		if "text" in keys:
			drafts.string(payload["text"], "사용자 글", 2000)
		if action in ("dev_draft", "meeting_analyze"):
			if type(payload["origin"]) is not int or not any(person["id"] == payload["origin"] for person in scene()["people"]):
				raise ValueError
		if action == "dev_draft":
			targets.target(payload["target"])
		if action in meetings.ACTIONS:
			meetings.validate(payload)
	except meetings.Invalid as exc:
		return 400, result(False, str(exc))
	except (ValueError, TypeError, ConfigError):
		return 400, result(False, "허용된 동작과 올바른 식별자가 필요합니다.")
	root = home()
	try:
		with private_file(root, "run", "office-action.lock") as lock:
			try:
				fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				return 409, result(False, "이미 실행 중")
			if action in drafts.ACTIONS:
				with private_file(root, "logs", "office-actions.jsonl") as audit:
					return drafts.execute(root, action, identity, payload.get("text"), audit, REPO, payload.get("target", "ai-hub"), payload.get("origin"))
			if action in meetings.ACTIONS:
				with private_file(root, "logs", "office-actions.jsonl") as audit:
					return meetings.execute(root, payload, audit, REPO)
			if not pending(root, action, identity):
				return 409, result(False, "결재함에 반영 대기 항목이 없습니다. 상태를 다시 읽어 주세요.")
			with private_file(root, "logs", "office-actions.jsonl") as audit:
				return execute(action, identity, audit)
	except (OSError, ValueError, TypeError, KeyError, ConfigError):
		return 409, result(False, "반영 상태 또는 잠금·기록을 확인할 수 없습니다. 다시 반영하기 전에 상태를 확인하세요.")
