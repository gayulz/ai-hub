"""볼트 일지 쓰기: 덩어리 단위로 만들고 바꾼다.

덩어리는 `<!-- ai-hub:KEY -->` ~ `<!-- /ai-hub -->` 표시로 감싼다.
표시 밖의 사람이 쓴 내용은 건드리지 않는다. 파일은 임시 파일을 거쳐 한 번에 바꾼다.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from .backfill_common import locked, sha

END = "<!-- /ai-hub -->"
LANDING = {
	"personal": "📥 Inbox/AI",
	"study": "📥 Inbox/AI",
	"career": "📥 Inbox/AI",
	"company": "🏢 Company/AI 일지",
}


def daily_path(vault: Path, cls: str, day: date) -> Path:
	if cls not in LANDING:
		raise ValueError(f"일지를 쓸 수 없는 분류: {cls}")
	return vault / LANDING[cls] / f"{day.isoformat()}.md"


def landing_dirs(vault: Path) -> list[Path]:
	return [vault / sub for sub in set(LANDING.values())]


def _start(key: str) -> str:
	return f"<!-- ai-hub:{key} -->"


def _new_file(day: date, cls: str) -> str:
	sensitivity = "company" if cls == "company" else "personal"
	return (
		"---\n"
		"type: daily\n"
		f"sensitivity: {sensitivity}\n"
		f"created: {day.isoformat()}\n"
		"tags: [ai-log]\n"
		"---\n\n"
		f"# {day.isoformat()} AI 작업 일지\n"
	)


def _insert_under(text: str, heading: str, block: str) -> str:
	"""`## heading` 절 끝(다음 `## ` 직전)에 덩어리를 넣는다. 절이 없으면 파일 끝에 만든다."""
	marker = f"## {heading}"
	lines = text.split("\n")
	# 작업 기록 안의 제목은 프로젝트 절의 경계가 아니다.
	headings, inside = [], False
	for i, line in enumerate(lines):
		if line.startswith("<!-- ai-hub:"):
			inside = True
		elif line == END:
			inside = False
		elif not inside and line.startswith("## "):
			headings.append(i)
	start = next((i for i in headings if lines[i].strip() == marker), None)
	if start is None:
		return text.rstrip("\n") + f"\n\n{marker}\n\n{block}\n"
	end = next((i for i in headings if i > start), len(lines))
	cut = end
	while cut > start + 1 and lines[cut - 1].strip() == "":
		cut -= 1
	new = lines[:cut] + ["", *block.split("\n"), ""] + lines[end:]
	return "\n".join(new)


def block_body(path: Path, key: str) -> str:
	text = path.read_text(encoding="utf-8") if path.exists() else ""
	pattern = re.compile(re.escape(_start(key)) + r"\n(.*?)\n" + re.escape(END), re.S)
	blocks = pattern.findall(text)
	if len(blocks) > 1:
		raise ValueError("같은 키의 기록 블록이 여러 개입니다")
	return blocks[0] if blocks else ""


def upsert_block(path: Path, key: str, body: str, heading: str, day: date, cls: str,
		*, expected_hash: str | None = None, initial: str | None = None) -> bool:
	"""덩어리를 쓰거나 바꾼다. 내용이 같으면 파일을 건드리지 않고 False를 돌려준다."""
	with locked("vault:" + str(path.absolute())):
		if expected_hash is not None:
			current = block_body(path, key)
			if sha(current) != expected_hash and current != body:
				raise ValueError("준비 이후 기존 기록 블록이 변경됐습니다")
		return _upsert_block(path, key, body, heading, day, cls, initial)


def _upsert_block(path: Path, key: str, body: str, heading: str, day: date, cls: str,
		initial: str | None) -> bool:
	if not path.parent.is_dir():
		raise FileNotFoundError(f"볼트 착지점이 없습니다: {path.parent}")
	old = path.read_text(encoding="utf-8") if path.exists() else initial or _new_file(day, cls)
	block = f"{_start(key)}\n{body}\n{END}"
	pattern = re.compile(re.escape(_start(key)) + r"\n.*?\n" + re.escape(END), re.S)
	if pattern.search(old):
		new = pattern.sub(lambda _: block, old, count=1)
	else:
		new = _insert_under(old, heading, block)
	if new == old:
		return False
	atomic_write(path, new)
	return True


def atomic_write(path: Path, text: str) -> None:
	tmp = path.with_name(f".{path.name}.tmp")
	tmp.write_text(text, encoding="utf-8")
	os.replace(tmp, path)
