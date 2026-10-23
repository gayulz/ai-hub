"""원장의 본문·경로 열을 조회하지 않고 건수만 읽는다."""

from __future__ import annotations

import sqlite3
import shutil
import tempfile
from datetime import date
from pathlib import Path

from collector.backfill_store import Store
from collector.config import ConfigError

from .common import identifier, normal, regular, stamp, text, unavailable

PARTS = ("progress", "batches", "active", "usage", "calls", "followups", "records")


def read_ledger(root: Path, day: str) -> dict:
	snapshot = None
	try:
		path = regular(root / "ledger.db", root)
		wal = path.with_name(path.name + "-wal")
		if wal.exists() or wal.is_symlink():
			regular(wal, root)
		else:
			# WAL이 잠시 없는 원장도 SQLite가 원본에 보조 파일을 만들지 않게 사본에서 연다.
			snapshot = tempfile.TemporaryDirectory(prefix="office-ledger-")
			before = path.stat()
			copy = Path(snapshot.name) / "ledger.db"
			shutil.copyfile(path, copy)
			after = path.stat()
			if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or wal.exists():
				raise ConfigError("읽는 중 원장 변경")
			path = copy
		store = Store(path, readonly=True)
	except (OSError, ValueError, sqlite3.Error, ConfigError):
		if snapshot is not None:
			snapshot.cleanup()
		return {key: unavailable("원장이 없거나 읽을 수 없습니다", items=[]) for key in PARTS}
	try:
		result = {}
		for key, reader in (("progress", progress), ("batches", batches), ("active", active),
				("usage", lambda conn: usage(conn, day)), ("calls", lambda conn: calls(conn, day)),
				("followups", followups), ("records", records)):
			try:
				result[key] = normal(**reader(store.conn))
			except (sqlite3.Error, ValueError, TypeError):
				result[key] = unavailable("원장의 해당 표·항목을 확인할 수 없습니다", items=[])
		return result
	finally:
		store.close()
		if snapshot is not None:
			snapshot.cleanup()


def progress(conn) -> dict:
	rows = conn.execute("""select s.project, s.kind, count(*) as total,
		sum(i.state='applied') as applied from c4_items i
		join c4_batches b on b.id=i.batch join c4_scopes s on s.id=b.scope
		where s.kind in ('repos','sessions') and b.state!='superseded'
		group by s.project,s.kind order by s.project,s.kind""")
	return {"items": [{"project": identifier(r["project"]), "kind": r["kind"],
		"applied": r["applied"], "total": r["total"]} for r in rows]}


def batches(conn) -> dict:
	rows = conn.execute("""select b.id,s.project,s.kind,
		sum(i.state='prepared') as prepared, sum(i.state='held') as held
		from c4_batches b join c4_scopes s on s.id=b.scope
		left join c4_items i on i.batch=b.id where b.state='prepared'
		group by b.id having sum(i.state='prepared') > 0 order by b.id""")
	items = []
	for row in rows:
		bid = identifier(row["id"])
		items.append({"id": bid, "project": identifier(row["project"]), "kind": identifier(row["kind"]),
			"prepared": row["prepared"] or 0, "held": row["held"] or 0, "reply": f"C4 {bid} 반영해"})
	return {"items": items}


def active(conn) -> dict:
	rows = conn.execute("""select s.project,s.kind,c.part,
		(select count(*) from c4_items n where n.batch=i.batch and n.rowid<=i.rowid) as position,
		(select count(*) from c4_items n where n.batch=i.batch) as total
		from c4_calls c left join c4_batches b on b.id=c.batch left join c4_scopes s on s.id=b.scope
		left join c4_items i on i.batch=c.batch and i.key=c.key
		where c.local=1 and c.state='reserved' order by c.id""")
	return {"items": [{"project": identifier(r["project"]), "kind": identifier(r["kind"]),
		"position": r["position"], "total": r["total"], "part": r["part"]} for r in rows]}


def usage(conn, day: str) -> dict:
	row = conn.execute("select count(*) as n, coalesce(sum(local=1),0) as local from c4_calls where day=?", (day,)).fetchone()
	return {"local": row["local"], "subscription": row["n"] - row["local"]}


def calls(conn, day: str) -> dict:
	rows = conn.execute("select plan,n from calls where day=? order by plan", (day,))
	items = []
	for row in rows:
		if not isinstance(row["n"], int) or row["n"] < 0:
			raise ValueError("호출 수 오류")
		items.append({"plan": identifier(row["plan"]), "count": row["n"]})
	return {"items": items}


def followups(conn) -> dict:
	items = []
	# 회사 문장 열은 조회하지 않는다. 일정에는 프로젝트별 건수와 기한만 보낸다.
	for row in conn.execute("select project,due,count(*) as n from followups where cls='company' group by project,due"):
		project = identifier(row["project"]) if row["project"] else "미지정"
		items.append({"project": project, "title": f"{project} · 후속 {row['n']}건", "due": row["due"], "source": "회사", "kind": "followup"})
	for row in conn.execute("select project,sentence,due from followups where cls in ('personal','study','career') order by created"):
		items.append({"project": identifier(row["project"]) if row["project"] else "", "title": text(row["sentence"]),
			"due": row["due"], "source": "개인", "kind": "followup"})
	for item in items:
		if item["due"]:
			date.fromisoformat(item["due"])
	items.sort(key=lambda item: (item["due"] or "9999", item["project"], item["title"]))
	return {"items": items}


def records(conn) -> dict:
	rows = conn.execute("select project,decision,created from work_records where cls in ('personal','study','career') order by created desc limit 10")
	return {"items": [{"kind": "chat", "title": "작업 기록" + (" · " + identifier(r["project"]) if r["project"] else ""),
		"text": text(r["decision"]), "at": stamp(r["created"])} for r in rows]}
