"""비공개 우선 스킬 읽기. 머리말은 두 단일 행 문자열만 해석한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from collector.backfill_common import safe
from collector.config import ConfigError, home

REPO = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class Skill:
	name: str
	description: str
	body: str
	source: str
	sha256: str


def _read(path: Path) -> bytes | None:
	if not safe(path):
		raise ConfigError("스킬 폴더와 파일에 심볼릭 링크를 사용할 수 없습니다")
	try:
		fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	except FileNotFoundError:
		return None
	with os.fdopen(fd, "rb") as stream:
		info = os.fstat(stream.fileno())
		if not stat.S_ISREG(info.st_mode):
			raise ConfigError("스킬은 일반 파일이어야 합니다")
		if info.st_size > MAX_BYTES:
			raise ConfigError("스킬 크기는 64KB 이하여야 합니다")
		data = stream.read(MAX_BYTES + 1)
	if len(data) > MAX_BYTES:
		raise ConfigError("스킬 크기는 64KB 이하여야 합니다")
	return data


def _scalar(value: str) -> str:
	value = value.strip()
	if value.startswith('"'):
		try:
			value = json.loads(value)
		except ValueError:
			raise ConfigError("스킬 머리말의 따옴표가 잘못됐습니다") from None
	elif value.startswith("'"):
		if len(value) < 2 or not value.endswith("'"):
			raise ConfigError("스킬 머리말의 따옴표가 잘못됐습니다")
		value = value[1:-1].replace("''", "'")
	if not isinstance(value, str) or value in ("|", ">", "|-", ">-", "|+", ">+"):
		raise ConfigError("스킬 머리말은 한 줄 문자열이어야 합니다")
	return value


def load(name: str) -> Skill:
	if not isinstance(name, str) or not NAME.fullmatch(name):
		raise ConfigError("스킬 이름 형식이 잘못됐습니다")
	try:
		for source, root in (("private", home()), ("repo", REPO)):
			data = _read(root / "skills" / name / "SKILL.md")
			if data is not None:
				break
		else:
			raise ConfigError("스킬 파일이 없습니다")
		text = data.decode("utf-8")
	except (OSError, UnicodeError):
		raise ConfigError("스킬을 UTF-8 일반 파일로 읽을 수 없습니다") from None
	lines = text.splitlines(keepends=True)
	if not lines or lines[0].rstrip("\r\n") != "---":
		raise ConfigError("스킬 맨 앞에 머리말이 필요합니다")
	metadata = {}
	for index, line in enumerate(lines[1:], 1):
		if line.rstrip("\r\n") == "---":
			break
		key, separator, value = line.partition(":")
		if separator and key in ("name", "description"):
			if key in metadata:
				raise ConfigError("스킬 머리말 항목이 중복됩니다")
			metadata[key] = _scalar(value)
	else:
		raise ConfigError("스킬 머리말의 닫는 구분자가 없습니다")
	if metadata.get("name") != name:
		raise ConfigError("스킬 머리말의 name이 폴더 이름과 다릅니다")
	description = metadata.get("description", "")
	if not 1 <= len(description) <= 1024 or not description.strip() or "\x00" in description or "\n" in description:
		raise ConfigError("스킬 description은 1~1024자의 한 줄 문자열이어야 합니다")
	return Skill(name, description, "".join(lines[index + 1:]).strip(), source,
		hashlib.sha256(data).hexdigest())


def prompt_block(skills) -> str:
	return "\n\n".join(f"## 스킬: {skill.name}\n{skill.body}" for skill in skills)
