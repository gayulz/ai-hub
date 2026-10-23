"""로컬 기록과 암호화 목록의 공통 형식. 절대 경로는 저장하지 않는다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from .crypto import BackupError


def object_name(relative: str) -> str:
	return hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".age"


def relative_parts(relative: str) -> tuple[str, ...]:
	if not isinstance(relative, str) or "\\" in relative or "\0" in relative:
		raise BackupError("잘못된 상대 경로입니다.")
	parts = relative.split("/")
	if PurePosixPath(relative).is_absolute() or any(part in ("", ".", "..") for part in parts):
		raise BackupError("잘못된 상대 경로입니다.")
	if parts[0] not in ("ai-hub", "claude", "codex", "gemini") or len(parts) < 2:
		raise BackupError("잘못된 상대 경로입니다.")
	return tuple(parts)


def validate_entry(relative: str, entry: dict) -> None:
	relative_parts(relative)
	if not isinstance(entry, dict) or entry.get("name") != object_name(relative):
		raise BackupError("목록의 사본 이름이 잘못되었습니다.")
	if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
		raise BackupError("목록의 해시가 잘못되었습니다.")
	if any(type(entry.get(key)) is not int or entry[key] < 0 for key in ("size", "mtime_ns")):
		raise BackupError("목록의 크기 또는 시각이 잘못되었습니다.")
	if type(entry.get("compressed")) is not bool or not isinstance(entry.get("backed_up_at"), str):
		raise BackupError("목록의 압축 또는 시각 정보가 잘못되었습니다.")


def load(path: Path) -> dict:
	try:
		with path.open(encoding="utf-8") as incoming:
			state = json.load(incoming)
	except FileNotFoundError:
		# 빈 기록에서 시작한다. 이전 목록은 backup.inherited 가 사본 목록에서 이어받는다.
		return {"files": {}, "sources": {}, "last_backup": None, "last_failed": 0}
	if not isinstance(state, dict) or not isinstance(state.get("files"), dict) or not isinstance(state.get("sources"), dict):
		raise BackupError("로컬 백업 기록 형식이 잘못되었습니다.")
	for relative, entry in state["files"].items():
		validate_entry(relative, entry)
	return state


def save(path: Path, state: dict) -> None:
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
			prefix=".backup-state-", delete=False) as outgoing:
		temporary = Path(outgoing.name)
		try:
			os.fchmod(outgoing.fileno(), 0o600)
			json.dump(state, outgoing, ensure_ascii=False, sort_keys=True)
			outgoing.flush()
			os.fsync(outgoing.fileno())
			os.replace(temporary, path)
		finally:
			temporary.unlink(missing_ok=True)
