"""매시 수집에서 하루 한 번 대화 소급을 이어 간다."""

from __future__ import annotations

import contextlib
import io
from collections import Counter

from . import backfill
from .backfill_common import locked, now
from .backfill_store import Store
from .config import home


SCHEMA = """
create table if not exists c4_daily (
 day text, project text, baseline integer, batch text, done integer default 0,
 primary key(day, project)
);
create table if not exists c4_daily_report (day text primary key, status text);
"""


def process(store, repo, day):
	row = store.conn.execute("select * from c4_daily where day=? and project=?", (day, repo.id)).fetchone()
	if row is None:
		baseline = store.conn.execute("select coalesce(max(rowid),0) from c4_batches").fetchone()[0]
		with store.conn:
			store.conn.execute("insert into c4_daily(day,project,baseline) values (?,?,?)", (day, repo.id, baseline))
		row = store.conn.execute("select * from c4_daily where day=? and project=?", (day, repo.id)).fetchone()
	while True:
		bid = row["batch"]
		if row["done"]:
			if not (bid and settle_company_noop(store, repo, day, bid)):
				return
			row = store.conn.execute("select * from c4_daily where day=? and project=?", (day, repo.id)).fetchone()
			continue
		if process_batch(store, repo, day, row):
			row = store.conn.execute("select * from c4_daily where day=? and project=?", (day, repo.id)).fetchone()
			continue
		return


def settle_company_noop(store, repo, day, bid):
	if repo.cls != "company":
		return False
	batch = store.batch(bid)
	scope = store.scope(batch["scope"])
	items = store.items(bid)
	if (batch["state"] != "prepared" or scope["project"] != repo.id or scope["kind"] != "sessions"
			or not items or any(item["state"] not in ("existing", "held") for item in items)):
		return False
	for item in items:
		store.release(item["key"], "backfill:" + bid)
	with store.conn:
		store.conn.execute("update c4_batches set state=? where id=?",
			("held" if any(item["state"] == "held" for item in items) else "applied", bid))
		store.conn.execute("update c4_daily set batch=null, done=0, baseline=(select coalesce(max(rowid),0) from c4_batches) "
			"where day=? and project=?", (day, repo.id))
	return True


def process_batch(store, repo, day, row):
	bid = row["batch"]
	if not bid:
		new = store.conn.execute("select b.id from c4_batches b join c4_scopes s on s.id=b.scope "
			"where b.rowid>? and s.project=? and s.kind='sessions' order by b.rowid limit 1",
			(row["baseline"], repo.id)).fetchone()
		opened = store.conn.execute("select b.id from c4_batches b join c4_scopes s on s.id=b.scope "
			"where s.project=? and s.kind='sessions' and b.state in ('pending','prepared') "
			"order by b.rowid limit 1", (repo.id,)).fetchone()
		bid = (new or opened)[0] if (new or opened) else None
		if not bid:
			backfill.execute(project_id=repo.id, kind="sessions", daily=True)
			new = store.conn.execute("select b.id from c4_batches b join c4_scopes s on s.id=b.scope "
				"where b.rowid>? and s.project=? and s.kind='sessions' order by b.rowid limit 1",
				(row["baseline"], repo.id)).fetchone()
			bid = new[0] if new else None
		with store.conn:
			store.conn.execute("update c4_daily set batch=? where day=? and project=?", (bid, day, repo.id))
	if bid:
		batch = store.batch(bid)
		if batch["state"] == "pending":
			backfill.execute(resume=bid, daily=True)
		batch = store.batch(bid)
		if settle_company_noop(store, repo, day, bid):
			return True
		if batch["state"] == "held":
			for item in store.items(bid):
				store.release(item["key"], "backfill:" + bid)
		if repo.cls != "company" and batch["state"] in ("prepared", "partial"):
			backfill.execute(apply=bid, daily=True)
	with store.conn:
		store.conn.execute("update c4_daily set done=1 where day=? and project=?", (day, repo.id))
	return False


def reason_kind(reason):
	if "상한" in reason:
		return "호출 한도"
	if "호출 결과 불명" in reason:
		return "호출 결과 불명"
	if "호출 실패" in reason or "요금제" in reason:
		return "호출 또는 요금제"
	if "활동 중" in reason:
		return "활동 중"
	return "원천·검증 등"


def summary(store, sources, day, failures=0):
	applied = 0
	for row in store.rows("c4_daily", "day=? and batch is not null", (day,)):
		repo = next((r for r in sources.repos if r.id == row["project"]), None)
		if repo and repo.cls != "company":
			applied += sum(i["state"] == "applied" for i in store.items(row["batch"]))
	waiting, remaining, held, agy, missing = 0, 0, Counter(), 0, 0
	held_seen = set()
	deferred_seen = set()
	for repo in sources.repos:
		if repo.cls not in ("personal", "study", "career", "company"):
			continue
		scopes = store.rows("c4_scopes", "project=? and kind='sessions' order by rowid desc", (repo.id,))
		if not scopes:
			missing += 1
			continue
		scope = store.scope(scopes[0]["id"])
		agy = max(agy, scope["data"].get("agy_count", 0))
		for item in scope["data"].get("deferred", []):
			if item["id"] not in deferred_seen:
				deferred_seen.add(item["id"])
				held[reason_kind(item.get("reason", ""))] += 1
		used = set()
		for batch in store.rows("c4_batches", "scope=? and state!='superseded'", (scope["id"],)):
			items = store.items(batch["id"])
			used.update(i["key"] for i in items)
		remaining += sum(t["key"] not in used for t in scope["data"]["tasks"])
		for old in scopes:
			for batch in store.rows("c4_batches", "scope=? and state!='superseded'", (old["id"],)):
				items = store.items(batch["id"])
				if repo.cls == "company" and batch["state"] == "prepared" and any(i["state"] == "prepared" for i in items):
					waiting += 1
				for item in items:
					identity = (item["key"], item["hash"])
					if item["state"] == "held" and identity not in held_seen:
						held_seen.add(identity)
						held[reason_kind(item["data"].get("hold_reason", ""))] += 1
	if agy:
		held["AGY 전체 보류"] += agy
	if failures:
		held["원천 처리 실패"] += failures
	calls = store.rows("c4_calls", "day=?", (day,))
	reasons = ", ".join(f"{key} {value}건" for key, value in sorted(held.items())) or "없음"
	left = f"남은 대상 미확인 (기존 목록 {remaining}건)" if failures or missing else f"남은 대상 {remaining}건 (고정 목록 기준)"
	return (f"대화 기록 정리 일일 요약\n개인 자동 반영 {applied}건\n회사 승인 대기 {waiting}묶음\n"
		f"보류 {sum(held.values())}건 ({reasons})\n당일 호출 {len(calls)}회 (구독 {sum(not c['local'] for c in calls)}회)\n"
		+ left)


def run(sources):
	current = now()
	day = current.date().isoformat()
	if not 2 <= current.hour < 6:
		path = home() / "ledger.db"
		if not path.exists():
			return
		check = Store(path, readonly=True)
		try:
			started = check.rows("c4_daily", "day=?", (day,))
			if not started or (all(r["done"] for r in started) and check.rows("c4_daily_report", "day=?", (day,))):
				return
		finally:
			check.close()
	with locked("backfill-daily"):
		store = Store(home() / "ledger.db")
		try:
			store.conn.executescript(SCHEMA)
			failures = 0
			for repo in sources.repos:
				if repo.cls in ("personal", "study", "career", "company"):
					# 기존 CLI 출력에 원천 정보가 있어 일일 로그에는 남기지 않는다.
					try:
						with contextlib.redirect_stdout(io.StringIO()):
							process(store, repo, day)
					except Exception as exc:
						failures += 1
						print(f"대화 소급 원천 처리 실패: {type(exc).__name__}")
			if not store.rows("c4_daily_report", "day=?", (day,)):
				message = summary(store, sources, day, failures)
				with store.conn:
					store.conn.execute("insert into c4_daily_report values (?, 'attempted')", (day,))
				try:
					from office.telegram_client import Client
					Client().send_message(message)
				except Exception as exc:
					with store.conn:
						store.conn.execute("update c4_daily_report set status=? where day=?", ("failed:" + type(exc).__name__, day))
				else:
					with store.conn:
						store.conn.execute("update c4_daily_report set status='sent' where day=?", (day,))
		finally:
			store.close()
