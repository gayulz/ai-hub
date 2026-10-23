"""출처를 읽을 때 쓰는 형식 검사와 화면용 값."""

from __future__ import annotations

import re
import tomllib
from datetime import datetime
from pathlib import Path

from collector.redact import redact


def unavailable(reason: str, **values) -> dict:
	return {"status": "확인 불가", "reason": reason, **values}


def normal(**values) -> dict:
	return {"status": "정상", **values}


def regular(path: Path, root: Path) -> Path:
	for part in (path, *path.parents):
		if part.name.startswith("🔐") or part.is_symlink():
			raise ValueError("읽기 금지 경로")
		if part == root:
			break
	if not path.is_file():
		raise FileNotFoundError
	return path


def read_toml(path: Path, root: Path) -> dict:
	with regular(path, root).open("rb") as source:
		return tomllib.load(source)


def identifier(value) -> str:
	if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value):
		raise ValueError("식별자 형식 오류")
	return value


def text(value) -> str:
	if not isinstance(value, str):
		raise ValueError("문자열 형식 오류")
	value = redact(value)
	value = re.sub(r"(?<![\w:/])(?:~?/|[A-Za-z]:[\\/])[^\s<>\"']+", "[경로 생략]", value)
	return " ".join(value.split())[:300]


def stamp(value: str) -> str:
	return datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds")
