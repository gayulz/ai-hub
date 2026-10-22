"""기록 원장(~/.ai-hub/ledger.db): 원천별 해시·상태, 요금제별 하루 호출 수, 실행 기록."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
create table if not exists sessions (
	key text primary key,
	tool text not null,
	sid text not null,
	cls text,
	hash text not null,
	status text not null,
	plan text,
	note text,
	updated text not null
);
create table if not exists calls (
	day text not null,
	plan text not null,
	n integer not null default 0,
	primary key (day, plan)
);
create table if not exists runs (
	id integer primary key autoincrement,
	started text not null,
	finished text,
	exit_code integer,
	summary text
);
"""


class Ledger:
	def __init__(self, path: Path):
		path.parent.mkdir(parents=True, exist_ok=True)
		self.conn = sqlite3.connect(path)
		self.conn.row_factory = sqlite3.Row
		self.conn.executescript(SCHEMA)

	def close(self) -> None:
		self.conn.close()

	def get(self, key: str) -> sqlite3.Row | None:
		return self.conn.execute("select * from sessions where key = ?", (key,)).fetchone()

	def put(self, key: str, tool: str, sid: str, cls: str | None, digest: str,
			status: str, plan: str | None = None, note: str | None = None) -> None:
		now = datetime.now().astimezone().isoformat(timespec="seconds")
		self.conn.execute(
			"insert into sessions (key, tool, sid, cls, hash, status, plan, note, updated) "
			"values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(key) do update set "
			"cls=excluded.cls, hash=excluded.hash, status=excluded.status, plan=excluded.plan, "
			"note=excluded.note, updated=excluded.updated",
			(key, tool, sid, cls, digest, status, plan, note, now),
		)
		self.conn.commit()

	def calls_today(self, plan: str, day: str) -> int:
		row = self.conn.execute("select n from calls where day = ? and plan = ?", (day, plan)).fetchone()
		return row["n"] if row else 0

	def bump_call(self, plan: str, day: str) -> None:
		self.conn.execute(
			"insert into calls (day, plan, n) values (?, ?, 1) "
			"on conflict(day, plan) do update set n = n + 1",
			(day, plan),
		)
		self.conn.commit()

	def start_run(self) -> int:
		cur = self.conn.execute(
			"insert into runs (started) values (?)",
			(datetime.now().astimezone().isoformat(timespec="seconds"),),
		)
		self.conn.commit()
		return cur.lastrowid

	def finish_run(self, run_id: int, exit_code: int, summary: str) -> None:
		self.conn.execute(
			"update runs set finished = ?, exit_code = ?, summary = ? where id = ?",
			(datetime.now().astimezone().isoformat(timespec="seconds"), exit_code, summary, run_id),
		)
		self.conn.commit()
