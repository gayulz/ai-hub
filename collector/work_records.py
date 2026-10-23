"""완료된 AI 답변에서 최근 작업 기록 블록만 고른다."""

import re

from .backfill_common import clean
from .sessions import Session

PLAN = "작업 기록 블록"
HEADING = re.compile(r"^ {0,3}#{1,2}\s+")
RECORD = re.compile(r"^ {0,3}## 작업 기록\s*#*\s*$")


def extract(session: Session) -> str | None:
	blocks = []
	for turn in session.turns:
		if turn.role != "assistant" or not turn.complete or turn.channel not in (None, "final"):
			continue
		block, fence = None, None
		for line in turn.text.splitlines():
			match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
			if match:
				mark = match[1]
				if fence is None:
					fence = mark
				elif mark[0] == fence[0] and len(mark) >= len(fence):
					fence = None
			elif fence is None and HEADING.match(line):
				if block and any(re.match(r"\s*- 결정:", value) for value in block[1:]):
					blocks.append("\n".join(block).rstrip())
				block = [line] if RECORD.fullmatch(line) else None
				continue
			if block is not None:
				block.append(line)
		if block and any(re.match(r"\s*- 결정:", value) for value in block[1:]):
			blocks.append("\n".join(block).rstrip())
	return clean("\n\n".join(blocks[-10:])) if blocks else None
