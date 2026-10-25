"""개인 할 일과 기존 결정의 처리 기록을 로컬에 저장한다."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

from .common import identifier, normal, unavailable
from .drafts import directory, private

ACTIONS = ("task_create", "task_update", "task_complete", "task_reopen")
KEYS = {"action", "id", "title", "memo"}
FIELDS = {"id", "kind", "title", "memo", "status", "created", "updated", "completed", "version"}


class Invalid(ValueError):
	pass


class Conflict(ValueError):
	pass


def checked_text(value, label: str, limit: int, required: bool = False) -> str:
	if (not isinstance(value, str) or len(value) > limit or (required and not value.strip())
			or any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in value)):
		raise Invalid(f"{label}의 형식과 길이를 확인해 주세요(최대 {limit}자).")
	return value.strip()


def identity(kind: str, value) -> str:
	identifier(value)
	if kind not in ("task", "decision") or (kind == "task" and not re.fullmatch(r"t-[a-f0-9]{32}", value)):
		raise ValueError("할 일 식별자 형식 오류")
	return value


def validate(payload: dict) -> None:
	try:
		create = payload["action"] == "task_create"
		if payload["action"] not in ACTIONS or set(payload) != (KEYS if create else KEYS | {"kind", "version"}):
			raise ValueError
		identity("task" if create else payload["kind"], payload["id"])
		if not create and (type(payload["version"]) is not int or payload["version"] < 0):
			raise ValueError
		checked_text(payload["title"], "제목", 300 if payload.get("kind") == "decision" else 200, True)
		checked_text(payload["memo"], "메모", 2000)
	except (ValueError, TypeError, KeyError) as exc:
		if isinstance(exc, Invalid):
			raise
		raise Invalid("할 일 요청의 동작·식별자·변경 버전을 확인해 주세요.") from None


def folder_path(root: Path) -> Path:
	folder = root / "office-tasks"
	if root.is_symlink() or folder.is_symlink():
		raise ValueError("할 일 기록은 링크 경로를 사용할 수 없습니다")
	if folder.exists() and (not folder.is_dir() or folder.stat().st_mode & 0o777 != 0o700):
		raise ValueError("할 일 폴더 권한을 확인해 주세요")
	return folder


def load(root: Path) -> list[dict]:
	folder = folder_path(root)
	if not folder.exists():
		return []
	rows = []
	for path in sorted(folder.iterdir()):
		if path.name.startswith(".pending-"):
			continue
		with private(path, root).open(encoding="utf-8") as stream:
			row = json.load(stream)
		if not isinstance(row, dict) or set(row) != FIELDS:
			raise ValueError("할 일 기록 형식 오류")
		identity(row["kind"], row["id"])
		if (path.name != row["kind"] + "-" + row["id"] + ".json" or row["status"] not in ("open", "done")
				or type(row["version"]) is not int or row["version"] < 1):
			raise ValueError("할 일 기록 형식 오류")
		checked_text(row["title"], "제목", 300, True)
		checked_text(row["memo"], "메모", 2000)
		for key in ("created", "updated"):
			if datetime.fromisoformat(row[key]).tzinfo is None:
				raise ValueError
		if row["status"] == "done":
			if datetime.fromisoformat(row["completed"]).tzinfo is None:
				raise ValueError
		elif row["completed"] is not None:
			raise ValueError
		rows.append(row)
	return rows


def read_state(root: Path, decisions: dict) -> tuple[dict, dict, dict]:
	try:
		stored = load(root)
	except (OSError, ValueError, TypeError, KeyError, RecursionError):
		error = unavailable("할 일 기록을 읽지 못했습니다. 파일과 권한을 확인해 주세요.", items=[])
		return error, error.copy(), {**decisions, "status": "확인 불가"}
	by_id = {row["id"]: row for row in stored if row["kind"] == "decision"}
	decision_rows = []
	for source in decisions.get("items", []):
		row = {"kind": "decision", "memo": "", "version": 0, "status": "open",
			"created": None, "updated": None, "completed": None, **source}
		if source["id"] in by_id:
			row.update(by_id[source["id"]])
			row["title"] = source["title"]
		if row["status"] == "open":
			decision_rows.append(row)
	personal = [row for row in stored if row["kind"] == "task" and row["status"] == "open"]
	completed = [row for row in stored if row["status"] == "done"]
	personal.sort(key=lambda row: (row["created"], row["id"]), reverse=True)
	completed.sort(key=lambda row: (row["completed"], row["id"]), reverse=True)
	return normal(items=personal + decision_rows + completed), normal(items=personal), {**decisions, "items": decision_rows}


def save(root: Path, row: dict) -> None:
	folder_path(root)
	folder = directory(root, "office-tasks")
	path = folder / (row["kind"] + "-" + row["id"] + ".json")
	if path.exists() or path.is_symlink():
		private(path, root)
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, prefix=".pending-", delete=False) as stream:
		temporary = Path(stream.name)
		try:
			os.fchmod(stream.fileno(), 0o600)
			json.dump(row, stream, ensure_ascii=False)
			stream.flush()
			os.fsync(stream.fileno())
			os.replace(temporary, path)
		finally:
			temporary.unlink(missing_ok=True)


def execute(root: Path, payload: dict, audit) -> tuple[int, dict]:
	from .state import read_decisions
	from .actions import result
	try:
		stored = load(root)
		validate(payload)
		action, identity_value = payload["action"], payload["id"]
		kind = "task" if action == "task_create" else payload["kind"]
		row = next((r.copy() for r in stored if r["kind"] == kind and r["id"] == identity_value), None)
		now = datetime.now().astimezone().isoformat(timespec="microseconds")
		if action == "task_create":
			if row:
				raise Conflict("이 할 일은 이미 등록됐습니다. 목록에서 확인해 주세요.")
			row = {"id": identity_value, "kind": kind, "status": "open", "created": now, "completed": None, "version": 0}
		else:
			if row is None and kind == "decision":
				source = read_decisions(root, datetime.now().date())["decisions"]
				_, _, group = read_state(root, source)
				row = next((r for r in group["items"] if r["id"] == identity_value), None)
			if row is None or row["version"] != payload["version"]:
				raise Conflict("할 일이 바뀌었거나 없습니다. 다시 읽고 확인해 주세요.")
			if kind == "decision" and (action == "task_reopen" or row["status"] == "open"):
				source = read_decisions(root, datetime.now().date())["decisions"]
				current = next((r for r in source["items"] if r["id"] == identity_value), None)
				if source["status"] != "정상" or current is None:
					raise Conflict("기존 결정이 닫혔거나 없습니다. 원본 상태를 확인해 주세요.")
				row["title"] = current["title"]
			if ((action == "task_complete" and row["status"] != "open")
					or (action == "task_reopen" and row["status"] != "done")):
				raise Conflict("할 일의 처리 상태가 바뀌었습니다. 다시 읽어 주세요.")
		row = {key: value for key, value in row.items() if key in FIELDS}
		row.update(title=checked_text(payload["title"], "제목", 200, True) if kind == "task" else row["title"],
			memo=checked_text(payload["memo"], "메모", 2000), updated=now, version=row["version"] + 1)
		row["created"] = row["created"] or now
		if action in ("task_complete", "task_reopen"):
			row["status"] = "done" if action == "task_complete" else "open"
			row["completed"] = now if action == "task_complete" else None
		save(root, row)
		message = {"task_create": "할 일을 등록했습니다.", "task_update": "메모를 저장했습니다.",
			"task_complete": "완료했습니다. 완료 탭에서 다시 볼 수 있습니다.", "task_reopen": "할 일을 다시 열었습니다."}[action]
		entry = {"at": now, "action": action, "id": identity_value, "ok": True, "message": message}
		try:
			audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
			audit.flush()
		except OSError:
			message += " 활동 기록 저장에 실패했지만 할 일은 저장됐습니다."
		return 200, {**result(True, message), "task": row}
	except Conflict as exc:
		return 409, result(False, str(exc))
	except (OSError, ValueError, TypeError, KeyError, RecursionError):
		return 409, result(False, "할 일을 저장하지 못했습니다. 기록과 권한을 확인한 뒤 다시 읽어 주세요.")
