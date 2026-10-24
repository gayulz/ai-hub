"""개발 지시 초안의 보관·재확인·실행 시작. 반영 명령은 부르지 않는다."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from orchestrator.draft import ROLES, read_draft, string, summary

from .common import action_id, identifier, normal, regular, stamp, unavailable

ACTIONS = ("dev_draft", "dev_redraft", "dev_run", "dev_discard")
STATUSES = ("초안", "실행 시작", "버림")


def directory(root: Path, relative: str) -> Path:
	if root.is_symlink():
		raise ValueError("초안 루트는 링크일 수 없습니다")
	folder = root
	for part in Path(relative).parts:
		folder = folder / part
		if folder.is_symlink():
			raise ValueError("초안·기록 폴더는 링크일 수 없습니다")
		folder.mkdir(mode=0o700, exist_ok=True)
		folder.chmod(0o700)
	return folder


def private(path: Path, root: Path) -> Path:
	path = regular(path, root)
	if path.stat().st_nlink != 1 or stat.S_IMODE(path.stat().st_mode) != 0o600:
		raise ValueError("초안은 링크가 아닌 권한 600 파일이어야 합니다")
	if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
		raise ValueError("초안 폴더는 권한 700이어야 합니다")
	return path


def save(root: Path, row: dict) -> None:
	folder = directory(root, "office-drafts")
	path = folder / (action_id("dev_run", row["id"]) + ".json")
	if path.exists() or path.is_symlink():
		private(path, root)
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as stream:
		temporary = Path(stream.name)
		try:
			os.fchmod(stream.fileno(), 0o600)
			json.dump(row, stream, ensure_ascii=False)
			stream.flush()
			os.replace(temporary, path)
		finally:
			temporary.unlink(missing_ok=True)


def metadata(root: Path, identity: str) -> dict:
	action_id("dev_run", identity)
	with private(root / "office-drafts" / (identity + ".json"), root).open(encoding="utf-8") as stream:
		row = json.load(stream)
	if (row["id"] != identity or row["role"] not in ROLES or row["status"] not in STATUSES
			or not isinstance(row["summary"], dict)):
		raise ValueError("초안 정보 형식 오류")
	string(row["text"], "사용자 글", 2000)
	stamp(row["created"])
	if row["previous"]:
		action_id("dev_run", row["previous"])
	identifier(row["summary"]["plan"])
	return row


def load(root: Path, identity: str) -> tuple[dict, dict]:
	row = metadata(root, identity)
	if row["status"] != "초안":
		raise ValueError("대기 중인 초안이 아닙니다")
	data = read_draft(private(root / "office-drafts" / (identity + ".toml"), root))
	if row["role"] != data["request"]["role"] or row["summary"] != summary(data, row["summary"]["plan"]):
		raise ValueError("초안 요약과 요청서가 다릅니다")
	return row, data


def card(row: dict, data: dict) -> dict:
	return {"id": row["id"], "created": row["created"], **summary(data, row["summary"]["plan"])}


def read_drafts(root: Path) -> dict:
	folder = root / "office-drafts"
	rows = []
	try:
		if folder.is_symlink():
			raise ValueError
		if not folder.exists():
			return normal(items=[])
		if not folder.is_dir() or stat.S_IMODE(folder.stat().st_mode) != 0o700:
			raise ValueError
		for path in sorted(folder.iterdir()):
			if path.suffix != ".json":
				continue
			row = metadata(root, path.stem)
			if row["status"] != "초안":
				continue
			row, data = load(root, path.stem)
			request = data["request"]
			rows.append({"id": row["id"], "role": row["role"], "goal": request["goal"],
				"done": len(request["done"]), "owned_paths": request["owned_paths"], "created": row["created"]})
		return normal(items=rows)
	except (OSError, ValueError, TypeError, KeyError, RecursionError):
		return unavailable("개발 지시 초안을 읽을 수 없습니다", items=[])


def generate(root: Path, role: str, text: str, previous: dict | None, repo: Path) -> dict:
	folder = directory(root, "office-drafts")
	identity = "d-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
	path = folder / (identity + ".toml")
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder) as stream:
		os.fchmod(stream.fileno(), 0o600)
		stream.write(text)
		stream.flush()
		argv = [sys.executable, "-m", "orchestrator", "draft", "--role", role,
			"--text-file", stream.name, "--out", str(path)]
		if previous:
			argv += ["--previous", str(folder / (previous["id"] + ".toml"))]
		completed = subprocess.run(argv, cwd=repo, env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)},
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=600, shell=False)
	if completed.returncode != 0:
		raise RuntimeError("초안 명령이 실패했습니다")
	data = read_draft(private(path, root))
	value = json.loads(completed.stdout)
	plan = identifier(value["plan"])
	if value != summary(data, plan) or data["request"]["role"] != role:
		raise ValueError("초안 명령의 요약을 확인할 수 없습니다")
	row = {"id": identity, "role": role, "text": text, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
		"summary": value, "status": "초안", "previous": previous["id"] if previous else ""}
	save(root, row)
	if previous:
		save(root, {**previous, "status": "버림"})
	return card(row, data)


def start(root: Path, row: dict, repo: Path) -> None:
	folder = directory(root, "logs/office-runs")
	path = folder / (row["id"] + ".log")
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as output:
		os.fchmod(output.fileno(), 0o600)
		# 먼저 예약해 두면 프로세스 시작 뒤 저장 실패로 같은 실행을 다시 띄우지 않는다.
		save(root, {**row, "status": "실행 시작"})
		try:
			subprocess.Popen([sys.executable, "-m", "orchestrator", "run",
				str(root / "office-drafts" / (row["id"] + ".toml"))], cwd=repo,
				env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)}, stdin=subprocess.DEVNULL,
				stdout=output, stderr=subprocess.STDOUT, shell=False, start_new_session=True)
		except OSError:
			save(root, row)
			path.unlink()
			raise


def execute(root: Path, action: str, identity: str, text: str | None, audit, repo: Path) -> tuple[int, dict]:
	started = time.monotonic()
	status, code, value = 200, 0, None
	try:
		if action == "dev_draft":
			value = generate(root, identity, text, None, repo)
			message = "개발 지시 초안을 썼습니다. 내용을 확인해 주세요."
		else:
			if action == "dev_discard":
				row = metadata(root, identity)
				if row["status"] != "초안":
					raise ValueError("대기 중인 초안이 아닙니다")
			else:
				row, data = load(root, identity)
			if action == "dev_redraft":
				value = generate(root, row["role"], text, row, repo)
				message = "개발 지시 초안을 다시 썼습니다."
			elif action == "dev_run":
				start(root, row, repo)
				message = "오케스트레이션 실행을 시작했습니다. 반영은 따로 승인해야 합니다."
			else:
				save(root, {**row, "status": "버림"})
				message = "초안을 버림으로 표시했습니다. 파일은 남아 있습니다."
	except subprocess.TimeoutExpired:
		status, code, message = 504, 124, "초안 작성 시간이 제한을 넘었습니다. 상태를 다시 확인하세요."
	except RuntimeError:
		status, code, message = 500, 2, "초안 작성이 실패했습니다. 사용자 글·요금제 설정을 확인하세요."
	except (OSError, ValueError, TypeError, KeyError, RecursionError):
		status, code, message = 409, 2, "초안·상태·실행 기록을 확인할 수 없습니다. 상태를 다시 읽어 주세요."
	entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "id": identity,
		"exit_code": code, "seconds": round(time.monotonic() - started, 3), "ok": code == 0, "message": message}
	try:
		audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		status, code, message = 500, 2, "활동 기록 저장에 실패했습니다. 다시 실행하기 전에 상태를 확인하세요."
	result = {"ok": code == 0, "message": message, "output": "", "restart_needed": False}
	if value is not None:
		result["draft"] = value
	return status, result
