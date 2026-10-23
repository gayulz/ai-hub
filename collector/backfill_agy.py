"""AGY는 본문 블롭을 열지 않고 보관함 사본의 분포와 보류 ID만 보고한다."""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .backfill_common import clean, files, private_write
from .config import Sources, classify, home
from .sessions import parse_time


def metadata(since: date, until: datetime) -> dict:
	rows, blobs, errors, known = {}, set(), [], set()
	for path in files(home() / "archive/agy"):
		if path.name != "conversation_summaries.db.gz":
			if path.name.endswith((".pb.gz", ".db.gz")):
				blobs.add(path.name.removesuffix(".gz").rsplit(".", 1)[0])
			continue
		try:
			with tempfile.TemporaryDirectory(prefix="c4-agy-") as folder:
				target = Path(folder) / "summary.db"
				with gzip.open(path, "rb") as source, target.open("wb") as output:
					shutil.copyfileobj(source, output)
				conn = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
				try:
					data = conn.execute("select conversation_id, agent_name, workspace_uris, step_count, "
						"last_modified_time from conversation_summaries").fetchall()
				finally:
					conn.close()
			for cid, agent, uris, steps, modified in data:
				known.add(str(cid))
				when = parse_time(modified)
				if when:
					when = when.astimezone(ZoneInfo("Asia/Seoul"))
				if when and (when.date() < since or when > until):
					continue
				rows[(str(cid), str(agent), str(uris), str(steps))] = (str(cid), agent, uris, steps)
		except (OSError, EOFError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
			errors.append("메타데이터 조회 실패: " + type(exc).__name__)
	return {"rows": list(rows.values()), "blob_ids": sorted(blobs - known), "errors": errors}


def report(data: dict, sources: Sources, since: str, until: str, *, write: bool) -> tuple[str, int]:
	agents, workspaces, steps = Counter(), Counter(), Counter()
	unknown, ids = 0, set(data["blob_ids"])
	for cid, agent, uris, count in data["rows"]:
		ids.add(cid)
		agents[agent or "(없음)"] += 1
		workspaces[uris or "[]"] += 1
		steps[str(count) if count is not None else "미확인"] += 1
		try:
			locations = [urlparse(uri) for uri in json.loads(uris or "[]")]
			unknown += not locations or any(uri.scheme != "file" or not classify(unquote(uri.path), sources.rules)
				for uri in locations)
		except (ValueError, TypeError, AttributeError):
			unknown += 1
	unknown += len(data["blob_ids"])
	text = (f"# AGY 소급 분포 보고서\n\n기간: {since} ~ {until}\n\n"
		f"전체 보류 ID {len(ids)}개 · 경로 미확인 {unknown}건. 수정일 기준 후보이며 시작일은 미확인.\n"
		"본문 블롭은 읽지 않았으며 AI 호출·볼트 기록은 없습니다.\n")
	for label, counts in (("agent_name", agents), ("workspace_uris", workspaces), ("step_count", steps)):
		text += f"\n## {label} 분포\n\n| 값 | 수 |\n|---|---:|\n"
		for value, number in sorted(counts.items()):
			text += f"| {str(value).replace('|', '&#124;').replace(chr(10), ' ')} | {number} |\n"
	text += "\n## 보류 ID\n\n" + "\n".join(f"- {cid}: 정책 C4-1 C, 전부 보류" for cid in sorted(ids)) + "\n"
	if data["errors"]:
		text += "\n## 조회 실패\n\n" + "\n".join("- " + e for e in data["errors"]) + "\n"
	path = home() / "backfill/agy-report.md"
	if write:
		private_write(path, clean(text))
	return str(path), len(ids)
