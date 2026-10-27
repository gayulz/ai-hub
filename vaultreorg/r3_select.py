"""R3 경로·머리말을 먼저 검사하고 개인 본문만 읽는다."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path

from collector.config import ConfigError

from . import frontmatter, rules

MARKER = "<!-- ai-hub:r3 -->"
HEADINGS = ("무엇을 했나", "결정과 이유", "실패와 원인", "남은 일")
TYPES = {"study", "decision", "troubleshoot", "meeting", "project"}
MAX_HEADER = 65536


def path_reason(path: Path) -> str:
	for part in path.parts:
		if part.startswith("🔐"):
			return "보호 경로"
		if part.startswith("."):
			return "숨김 경로"
		if rules.folder(Path(unicodedata.normalize("NFC", part)) / "note.md") == "Company":
			return "회사 폴더"
	return ""


def target(vault: Path, value: str) -> Path:
	rel = rules.relative(value)
	if path_reason(rel):
		raise ConfigError("R3 제외 대상 경로입니다")
	return rules.target(vault, value)


def inventory(vault: Path, counts: Counter):
	for path in sorted(vault.iterdir()):
		reason = path_reason(Path(path.name))
		if reason:
			counts[reason] += 1
		elif path.is_symlink():
			counts["심볼릭 링크"] += 1
		elif path.is_dir():
			yield from inventory(path, counts)
		elif path.suffix == ".md" and path.is_file():
			yield path


def fields(header: frontmatter.Header) -> dict[str, str]:
	values = {}
	for line in header.lines:
		key = frontmatter.key(line)
		if key not in ("sensitivity", "type"):
			continue
		if key in values:
			raise frontmatter.NoteError("머리말 키 중복")
		raw = line.split(":", 1)[1].rstrip("\r\n").split(" #", 1)[0]
		values[key] = frontmatter.scalar(raw)[0]
	return values


def read_personal(vault: Path, value: str) -> tuple[bytes | None, str]:
	path = target(vault, value)
	# 버퍼의 미리 읽기까지 막아 회사·분류 없는 노트의 본문을 열지 않는다.
	with path.open("rb", buffering=0) as source:
		opening = source.readline(MAX_HEADER + 1)
		if opening.removeprefix(frontmatter.BOM).rstrip(b"\r\n") != b"---":
			return None, "머리말 없음"
		lines, size = [opening], len(opening)
		while size <= MAX_HEADER:
			line = source.readline(MAX_HEADER - size + 1)
			if not line:
				break
			lines.append(line)
			size += len(line)
			if line.rstrip(b"\r\n") == b"---":
				header = b"".join(lines)
				values = fields(frontmatter.split(header))
				sensitivity = values.get("sensitivity")
				if sensitivity != "personal":
					return None, "회사 노트" if sensitivity == "company" else "personal 분류 없음"
				return header + source.read(), ""
	return None, "머리말 형식 오류"


def choose(data: bytes) -> tuple[str | None, str, str]:
	header = frontmatter.split(data)
	values = fields(header)
	kind = values.get("type", "")
	if values.get("sensitivity") != "personal":
		return None, kind, "personal 분류 없음"
	if MARKER.encode() in data:
		return None, kind, "이미 R3 표식"
	body = header.body.decode("utf-8")
	pattern = r"(?m)^##[ \t]+(?:" + "|".join(HEADINGS) + r")[ \t]*(?:#+[ \t]*)?\r?$"
	if re.search(pattern, body):
		return None, kind, "기존 5-2 제목"
	if kind not in TYPES:
		return None, kind, "제외 종류" if kind else "종류 없음"
	if not body.strip():
		return None, kind, "본문 없음"
	if kind == "project" and len(body) < 5000:
		return None, kind, "작은 project"
	return "study" if kind == "study" else "outline", kind, ""
