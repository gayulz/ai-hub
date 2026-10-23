"""원래 Codex 세션과 ID·경로·시각이 맞는 기존 요약만 재사용한다."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .backfill_common import clean, files, sha
from .sessions import Session, parse_time

SUMMARY_ROOT = Path.home() / ".codex/memories/rollout_summaries"


def read_summary(path: Path) -> dict | None:
	try:
		text = path.read_text(encoding="utf-8")
		if path.suffix == ".json":
			data = json.loads(text)
			if not isinstance(data, dict):
				return None
		else:
			data = {}
			for name in ("thread_id", "session_id", "cwd", "updated_at", "started_at", "ended_at", "thread_source"):
				match = re.search(rf"(?m)^{name}:\s*(.+)$", text)
				if match:
					data[name] = match[1].strip().strip('"\'`')
			data["summary"] = re.sub(r"(?m)^(?:thread_id|session_id|cwd|updated_at|started_at|ended_at|rollout_path|thread_source):.*\n?", "", text)
		data["source_hash"] = sha(text)
		return data
	except (OSError, ValueError, TypeError):
		return None


def match_summary(session: Session, until: datetime) -> dict | None:
	if session.tool != "codex":
		return None
	candidates = []
	for path in files(SUMMARY_ROOT):
		if path.suffix not in (".md", ".json"):
			continue
		data = read_summary(path)
		if not data or (data.get("thread_id") or data.get("session_id")) != session.sid:
			continue
		updated = parse_time(data.get("updated_at"))
		if data.get("cwd") != session.cwd or not updated or not session.last <= updated <= until:
			continue
		if data.get("thread_source") in ("subagent", "guardian_review"):
			continue
		start, end = parse_time(data.get("started_at")), parse_time(data.get("ended_at"))
		if (start and start > session.started) or (end and end < session.last):
			continue
		body = data.get("summary", "")
		if not isinstance(body, str):
			continue
		body = re.sub(r"```.*?(?:```|\Z)", "", body, flags=re.S).strip()
		if not body or re.search(r"(?m)^\s*(?:diff --git|@@ |tool_result|function_call|\{\s*$)", body):
			continue
		candidates.append({"summary": clean(body), "source_hash": data["source_hash"],
			"source": clean(path.absolute().as_uri()), "updated_at": updated.isoformat()})
	return candidates[0] if len(candidates) == 1 else None
