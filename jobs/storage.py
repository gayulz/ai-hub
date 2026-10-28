"""개인 자료 저장 경계: 전용 잠금, 권한 검사와 원자적 쓰기."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector.backfill_common import locked, private_write, safe
from collector.config import home

IDENTITY = re.compile(r"[mav]-[0-9a-f]{32}\Z")


def now():
	return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix):
	return prefix + "-" + uuid.uuid4().hex


def identity(value, prefix):
	if not isinstance(value, str) or not IDENTITY.fullmatch(value) or not value.startswith(prefix + "-"):
		raise ValueError("식별자가 잘못됐습니다")
	return value


def directory(path):
	if not safe(path.absolute()):
		raise ValueError("안전하지 않은 저장 경로입니다")
	if not path.exists():
		path.mkdir(mode=0o700)
	info = path.lstat()
	if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
		raise ValueError("개인 저장 폴더 권한을 확인하세요")
	return path


def checked_file(path):
	if not safe(path.absolute()):
		raise ValueError("안전하지 않은 파일입니다")
	if path.exists():
		info = path.lstat()
		if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
			raise ValueError("개인 저장 파일 권한을 확인하세요")


@contextmanager
def transaction():
	root = directory(home() / "jobs")
	lock = root / ".lock"
	checked_file(lock)
	with locked("jobs", exact=lock):
		checked_file(lock)
		yield root


def folder(root, name):
	return directory(root / name)


def read(path):
	checked_file(path)
	if not path.is_file():
		raise ValueError("요청한 기록이 없습니다")
	try:
		with path.open(encoding="utf-8") as stream:
			value = json.load(stream)
	except (OSError, UnicodeError, json.JSONDecodeError):
		raise ValueError("개인 기록을 읽을 수 없습니다") from None
	if not isinstance(value, dict):
		raise ValueError("개인 기록 형식이 잘못됐습니다")
	return value


def write(path, value):
	checked_file(path)
	private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def records(root, name, prefix):
	base = folder(root, name)
	result = []
	for path in sorted(base.iterdir()):
		if not path.name.endswith(".json"):
			raise ValueError("개인 기록 폴더의 항목이 잘못됐습니다")
		key = identity(path.stem, prefix)
		row = read(path)
		if row.get("id") != key:
			raise ValueError("개인 기록 식별자가 다릅니다")
		result.append(row)
	return result


def get(root, name, key, prefix):
	identity(key, prefix)
	row = read(folder(root, name) / (key + ".json"))
	if row.get("id") != key:
		raise ValueError("개인 기록 식별자가 다릅니다")
	return row


def save(root, name, row, prefix):
	key = identity(row["id"], prefix)
	write(folder(root, name) / (key + ".json"), row)


def event(root, status, version_id="", error=""):
	path = root / "events.json"
	data = read(path) if path.exists() else {"events": []}
	if not isinstance(data.get("events"), list):
		raise ValueError("작업 기록 형식이 잘못됐습니다")
	data["events"].append({"at": now(), "status": status, "version_id": version_id, "error": error})
	write(path, data)
