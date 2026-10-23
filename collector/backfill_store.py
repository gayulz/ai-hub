"""소급 목록·묶음·부분 결과와 원자적인 호출 예약. 원문은 저장하지 않는다."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from .backfill_common import now, packed
from .config import ConfigError
from .ledger import Ledger, SCHEMA

C4_SCHEMA = """
create table if not exists c4_scopes (
	id text primary key, project text, kind text, since text, until text,
	policy text, data text
);
create table if not exists c4_batches (
	id text primary key, scope text, state text, report text
);
create table if not exists c4_items (
	batch text, key text, hash text, state text, data text,
	primary key(batch, key)
);
create table if not exists c4_calls (
	id integer primary key, batch text, key text, part integer, plan text,
	day text, state text, result text,
	unique(batch, key, part, plan)
);
create table if not exists c4_indexes (
	project text primary key, path text not null
);
"""


class Store(Ledger):
	def __init__(self, path: Path, readonly: bool = False):
		self.readonly = readonly
		self.snapshot = None
		if readonly:
			if path.exists():
				wal = path.with_name(path.name + "-wal")
				if wal.exists():
					# 읽기 전용 SQLite도 WAL 공유 메모리를 만들 수 있어 사본에서만 연다.
					self.snapshot = tempfile.TemporaryDirectory(prefix="c4-ledger-")
					copy = Path(self.snapshot.name) / path.name
					before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in (path, wal)]
					shutil.copyfile(path, copy)
					shutil.copyfile(wal, copy.with_name(wal.name))
					if before != [(p.stat().st_size, p.stat().st_mtime_ns) for p in (path, wal)]:
						self.snapshot.cleanup()
						raise ConfigError("미리보기 중 원장이 변경됐습니다. 다시 실행하세요")
					path = copy
				self.conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
			else:
				self.conn = sqlite3.connect(":memory:")
				self.conn.executescript(SCHEMA + C4_SCHEMA)
			self.conn.row_factory = sqlite3.Row
			self.conn.execute("pragma query_only=on")
		else:
			super().__init__(path)
			path.chmod(0o600)
			self.conn.executescript(C4_SCHEMA)

	def close(self) -> None:
		super().close()
		if self.snapshot is not None:
			self.snapshot.cleanup()

	def rows(self, table: str, where: str = "1", args: tuple = ()) -> list[dict]:
		if not self.conn.execute("select 1 from sqlite_master where name=?", (table,)).fetchone():
			return []
		return [dict(row) for row in self.conn.execute(f"select * from {table} where {where}", args)]

	def scope(self, sid: str) -> dict:
		rows = self.rows("c4_scopes", "id=?", (sid,))
		if not rows:
			raise ConfigError("소급 목록을 찾을 수 없습니다")
		result = rows[0]
		result["data"] = json.loads(result["data"])
		return result

	def batch(self, bid: str) -> dict:
		rows = self.rows("c4_batches", "id=?", (bid,))
		if not rows:
			raise ConfigError("묶음 ID를 찾을 수 없습니다")
		return rows[0]

	def items(self, bid: str) -> list[dict]:
		rows = self.rows("c4_items", "batch=? order by rowid", (bid,))
		for row in rows:
			row["data"] = json.loads(row["data"])
		return rows

	def save(self, bid: str, task: dict, state: str) -> None:
		with self.conn:
			self.conn.execute("update c4_items set state=?, data=? where batch=? and key=?",
				(state, packed(task), bid, task["key"]))

	def create_scope(self, project: str, kind: str, since: str, until: str, policy: str, data: dict) -> dict:
		sid = uuid4().hex
		with self.conn:
			self.conn.execute("insert into c4_scopes values (?, ?, ?, ?, ?, ?, ?)",
				(sid, project, kind, since, until, policy, packed(data)))
		return self.scope(sid)

	def create_batch(self, scope: dict, tasks: list[dict], report: Path) -> str:
		bid = "c4-" + uuid4().hex[:16]
		with self.conn:
			self.conn.execute("insert into c4_batches values (?, ?, 'pending', ?)",
				(bid, scope["id"], str(report)))
			for task in tasks:
				self.conn.execute("insert into c4_items values (?, ?, ?, 'pending', ?)",
					(bid, task["key"], task["hash"], packed(task)))
		return bid

	def reserve(self, bid: str, task: dict, part: int, plan, total_limit: int) -> int | None:
		with self.conn:
			self.conn.execute("begin immediate")
			day = now().date().isoformat()
			used = self.conn.execute("select count(*) from c4_calls where day=?", (day,)).fetchone()[0]
			if used >= total_limit or (plan.daily_limit is not None and
					self.calls_today(plan.id, day) >= plan.daily_limit):
				return None
			if any(c["part"] == part and c["plan"] == plan.id for c in self.work_calls(bid, task)):
				return None
			self.conn.execute("insert into calls values (?, ?, 1) "
				"on conflict(day, plan) do update set n=n+1", (day, plan.id))
			cur = self.conn.execute("insert into c4_calls(batch,key,part,plan,day,state) "
				"values (?, ?, ?, ?, ?, 'reserved')", (bid, task["key"], part, plan.id, day))
		return cur.lastrowid

	def finish_call(self, cid: int, state: str, result: dict) -> None:
		with self.conn:
			self.conn.execute("update c4_calls set state=?, result=? where id=?", (state, packed(result), cid))

	def calls(self, bid: str, key: str) -> list[dict]:
		return self.rows("c4_calls", "batch=? and key=? order by id", (bid, key))

	def work_calls(self, bid: str, task: dict) -> list[dict]:
		batches = {bid}
		for row in self.rows("c4_items", "key=? and hash=?", (task["key"], task["hash"])):
			data = json.loads(row["data"])
			if data.get("material") == task.get("material") and data.get("cls") == task.get("cls"):
				batches.add(row["batch"])
		return [row for batch in sorted(batches) for row in self.calls(batch, task["key"])]

	def state(self, bid: str, state: str) -> None:
		with self.conn:
			self.conn.execute("update c4_batches set state=? where id=?", (state, bid))
