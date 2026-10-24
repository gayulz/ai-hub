"""머리말만 편집한다. 본문·BOM·기존 키의 바이트는 보존한다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

BOM = b"\xef\xbb\xbf"
KEYS = ("type", "sensitivity", "created")
KEY = re.compile(r'''^(?:([\w*-]+)|'([^']+)'|"([^"]+)")\s*:''')


class NoteError(ValueError):
	"""노트를 안전하게 고칠 수 없을 때."""


@dataclass
class Header:
	bom: bytes
	opening: bytes
	lines: list[str]
	closing: bytes
	body: bytes
	newline: str


def key(line: str) -> str | None:
	match = KEY.match(line)
	return next((v for v in match.groups() if v is not None), None) if match else None


def split(data: bytes) -> Header:
	data.decode("utf-8")
	bom = BOM if data.startswith(BOM) else b""
	plain = data[len(bom):]
	lines = plain.splitlines(keepends=True)
	first = next((line for line in lines if line.endswith(b"\n")), b"\n")
	newline = "\r\n" if first.endswith(b"\r\n") else "\n"
	if not lines or lines[0].rstrip(b"\r\n") != b"---":
		return Header(bom, b"", [], b"", plain, newline)
	for index in range(1, len(lines)):
		if lines[index].rstrip(b"\r\n") == b"---":
			return Header(bom, lines[0], [s.decode("utf-8") for s in lines[1:index]],
				lines[index], b"".join(lines[index + 1:]), newline)
	raise NoteError("머리말 닫는 줄이 없음")


def plain_tag(value: str) -> bool:
	return bool(value) and value == value.strip() and not any(c in value for c in "[],{}\r\n") \
		and not value.startswith(("#", "&", "*", "!", "|", ">", "'", '"', "- ", "? ", ": ")) \
		and ": " not in value and " #" not in value


def scalar(raw: str) -> tuple[str, str]:
	text = raw.strip(" \t")
	if text.startswith('"'):
		value, quote = json.loads(text), '"'
	elif text.startswith("'") and text.endswith("'") and len(text) >= 2:
		inner = text[1:-1]
		if "'" in inner.replace("''", ""):
			raise ValueError
		value, quote = inner.replace("''", "'"), "'"
	elif plain_tag(text):
		value, quote = text, ""
	else:
		raise ValueError
	if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
		raise ValueError
	return value, quote


def tag_parts(text: str) -> list[str]:
	if not text.strip():
		return []
	parts, start, index, quote = [], 0, 0, ""
	while index < len(text):
		char = text[index]
		if quote:
			if quote == '"' and char == "\\":
				index += 1
			elif char == quote:
				if quote == "'" and text[index:index + 2] == "''":
					index += 1
				else:
					quote = ""
		elif char in "\"'" and not text[start:index].strip():
			quote = char
		elif char == ",":
			parts.append(text[start:index])
			start = index + 1
		index += 1
	if quote:
		raise ValueError
	parts.append(text[start:])
	return parts


def normalize(value: str, mapping: dict[str, str]) -> str:
	if value in mapping:
		return mapping[value]
	if re.fullmatch(r"[a-zA-Z0-9 _-]+", value):
		return re.sub("-+", "-", value.lower().replace(" ", "-").replace("_", "-"))
	return value


def replace_scalar(raw: str, value: str, quote: str) -> str:
	if quote == '"':
		text = json.dumps(value, ensure_ascii=False)
	elif quote == "'":
		text = "'" + value.replace("'", "''") + "'"
	elif plain_tag(value):
		text = value
	else:
		raise ValueError
	return raw[:len(raw) - len(raw.lstrip(" \t"))] + text + raw[len(raw.rstrip(" \t")):]


def edit_tags(lines: list[str], mapping: dict[str, str]) -> tuple[list[str], list[tuple[str, str]], bool]:
	indices = [i for i, line in enumerate(lines) if key(line) == "tags"]
	if not indices:
		return lines, [], False
	if len(indices) != 1:
		return lines, [], True
	start = indices[0]
	line = lines[start]
	inline = re.fullmatch(r'''((?:tags|'tags'|"tags")\s*:[ \t]*)\[(.*)\]([ \t]*(?:\#.*)?)(\r?\n)?''', line)
	try:
		if inline:
			parts = tag_parts(inline[2])
			tail = "," + parts.pop() if parts and not parts[-1].strip() else ""
			end = start + 1
		else:
			if not re.fullmatch(r'''(?:tags|'tags'|"tags")\s*:[ \t]*(?:\#.*)?(?:\r?\n)?''', line):
				raise ValueError
			end = start + 1
			while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
				end += 1
			parts = []
			for row in lines[start + 1:end]:
				if not row.strip() or row.lstrip().startswith("#"):
					parts.append((row, None))
				else:
					match = re.fullmatch(r"(  -[ \t]+)(.*?)(\r?\n)?", row)
					if not match:
						raise ValueError
					parts.append((match, match[2]))
			if not any(raw is not None for _, raw in parts):
				raise ValueError
		seen, changes, edited, removed = set(), set(), [], False
		for part in parts:
			if not inline and part[1] is None:
				edited.append(part[0])
				continue
			raw = part if inline else part[1]
			value, quote = scalar(raw)
			new = normalize(value, mapping)
			if value != new:
				changes.add((value, new))
			if new in seen:
				removed = True
				continue
			seen.add(new)
			text = replace_scalar(raw, new, quote) if value != new else raw
			edited.append(text if inline else part[0][1] + text + (part[0][3] or ""))
		if not changes and not removed:
			return lines, [], False
		if inline:
			replacement = [inline[1] + "[" + ",".join(edited) + tail + "]" + inline[3] + (inline[4] or "")]
		else:
			replacement = [line, *edited]
		return lines[:start] + replacement + lines[end:], sorted(changes), False
	except (ValueError, TypeError):
		return lines, [], True


def render(data: bytes, additions: dict[str, str], mapping: dict[str, str]):
	header = split(data)
	lines, changes, unsupported = edit_tags(header.lines, mapping)
	existing = {key(line) for line in header.lines}
	new = "".join(f"{name}: {additions[name]}{header.newline}" for name in KEYS
		if name in additions and name not in existing)
	opening = header.opening or ("---" + header.newline).encode()
	closing = header.closing or ("---" + header.newline).encode()
	after = header.bom + opening + ("".join(lines) + new).encode() + closing + header.body
	check = split(after)
	# BOM은 인코딩 표시로 맨 앞에 보존하고 본문에서 분리한다.
	if check.body != header.body or check.bom != header.bom:
		raise NoteError("본문 바이트 대조 실패")
	return after, changes, unsupported
