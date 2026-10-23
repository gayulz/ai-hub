"""개인 대화의 로컬·구독 비교. C4의 목록·호출 예약·재개를 공유한다."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date

from . import backfill, backfill_sessions as sessions
from . import ollama, session_summaries, vault
from .backfill_common import child, clean, locked, packed, sha
from .backfill_compare_report import report
from .backfill_store import Store
from .config import ConfigError, home, load_plans, load_sources


def routes(plans, cls):
	if cls not in ("personal", "study", "career"):
		raise ConfigError("비교에는 개인·공부·커리어 원천만 지정할 수 있습니다. 회사 대화는 제외합니다")
	ids = plans.backfill_routing.get(cls, plans.routing.get(cls, ()))
	allowed = [plans.plans[pid] for pid in ids if cls in plans.plans[pid].allow]
	local = next((p for p in allowed if p.tool == "ollama"), None)
	subs = [p for p in allowed if p.tool != "ollama"]
	if local is None or not subs:
		raise ConfigError("비교에는 해당 분류의 routing·allow에 허용된 Ollama와 구독 요금제가 모두 필요합니다")
	return local, replace(plans, backfill_routing={**plans.backfill_routing,
		cls: tuple([local.id] + [p.id for p in subs])})


def candidates(store, data, sources, repo, until, count):
	selected, excluded = [], Counter(d["reason"] for d in data.get("deferred", []))
	for task in data["tasks"]:
		task = dict(task, cls=repo.cls)
		reason = task.get("reason", "") or backfill.validate_source(task, repo, sources)
		if not reason:
			row = store.get(task["key"])
			path = vault.daily_path(sources.vault, repo.cls, date.fromisoformat(task["day"]))
			path = child(sources.vault, path.relative_to(sources.vault).as_posix())
			if (row and row["status"] == "recorded") or vault.block_body(path, task["key"]):
				reason = "이미 기록된 대화"
			elif session_summaries.match_summary(sessions.load(task), until):
				reason = "기존 요약 재사용 대상"
			elif any(c["state"] in ("success", "reserved") for c in store.work_calls("", task)):
				reason = "기존 정제 결과 재사용 또는 진행 중인 호출"
			elif backfill.cached_session(store, task):
				reason = "기존 준비 결과 재사용 대상"
			elif task["parts"] != 1:
				reason = "분할 입력이 필요해 모델별 1회 비교에서 제외"
		if reason:
			excluded[reason] += 1
		elif len(selected) < count:
			selected.append(task)
	if len(selected) < count:
		excluded["새 정제가 필요한 적격 대화가 요청 수보다 적음"] += count - len(selected)
	return selected, dict(excluded)


def prepare(store, bid, sources, repo, plans):
	local, comparison_plans = routes(plans, repo.cls)
	batch = store.batch(bid)
	child(home(), f"backfill/compare-{bid}/report.md")
	if batch["state"] == "compared":
		report(store, bid, write=False)
		return 0
	items = store.items(bid)
	if any(i["state"] != "compared" and not i["data"].get("comparison", {}).get("local") for i in items):
		try:
			ollama.check(local)
		except ollama.OllamaError as exc:
			print(str(exc))
			store.state(bid, "held")
			report(store, bid)
			return 1
	failed = {c["plan"] for c in store.rows("c4_calls", "batch=? and state='failed'", (bid,))}
	for item in items:
		if item["state"] == "compared":
			continue
		task = item["data"]
		reason = backfill.validate_source(task, repo, sources)
		if not reason and not store.claim(task["key"], "backfill:" + bid):
			reason = "같은 대화를 다른 수집 작업이 선점 중"
		try:
			if not reason:
				session = sessions.load(task)
				prompts, reason = sessions.prompts(session)
				if not reason and (len(prompts) != 1 or sha(prompts[0]) != task["material"]):
					reason = "고정한 비교 입력이 변경됐습니다. 새 비교가 필요합니다"
				if not reason:
					task["input"] = prompts[0]
					for side, is_local in (("local", True), ("subscription", False)):
						parts, reason = backfill.summarize_session(store, bid, task, session,
							comparison_plans, failed, local=is_local, prompts=prompts)
						if reason:
							break
						task.setdefault("comparison", {})[side] = parts[0]
						store.save(bid, task, "processing")
					if not reason:
						task.pop("hold_reason", None)
						store.save(bid, task, "compared")
			if reason:
				task["hold_reason"] = reason
				store.save(bid, task, "held")
				print("비교 중단: " + clean(reason))
				break
		except (OSError, ValueError, ConfigError) as exc:
			task["hold_reason"] = "비교 준비 보류: " + type(exc).__name__
			store.save(bid, task, "held")
			print(task["hold_reason"])
			break
		finally:
			store.release(task["key"], "backfill:" + bid)
	complete = all(i["state"] == "compared" for i in store.items(bid))
	requested = store.scope(batch["scope"])["data"]["requested"]
	store.state(bid, "compared" if complete and len(items) == requested else "held")
	report(store, bid)
	return 0 if store.batch(bid)["state"] == "compared" else 1


def execute(pid, count=20, *, dry_run=False):
	if type(count) is not int or not 1 <= count <= 20:
		raise ConfigError("비교 count는 1~20 사이 정수여야 합니다")
	sources, plans = load_sources(), load_plans()
	repo = backfill.project(sources, pid)
	routes(plans, repo.cls)
	store = Store(home() / "ledger.db", readonly=dry_run)
	try:
		if dry_run:
			until = backfill.now()
			data = sessions.inventory(repo, sources, backfill.DEFAULT_SINCE, until)
			tasks, reasons = candidates(store, data, sources, repo, until, count)
			print(f"[dry-run] 비교 대상 {len(tasks)}건 · 부족 {count - len(tasks)}건 · 이유 {packed(reasons)}")
			return 0
		with locked("backfill"):
			fingerprint = backfill.policy(sources, plans, repo)
			for row in store.rows("c4_scopes", "project=? and kind='compare' and policy=? order by rowid desc", (pid, fingerprint)):
				scope = store.scope(row["id"])
				if scope["data"]["requested"] == count:
					batches = store.rows("c4_batches", "scope=?", (scope["id"],))
					if batches:
						report(store, batches[0]["id"], write=False)
						return 0 if batches[0]["state"] == "compared" else 1
			until = backfill.now()
			data = sessions.inventory(repo, sources, backfill.DEFAULT_SINCE, until)
			tasks, reasons = candidates(store, data, sources, repo, until, count)
			data.update(tasks=tasks, requested=count, exclusions=reasons)
			scope = store.create_scope(pid, "compare", str(backfill.DEFAULT_SINCE), until.isoformat(), fingerprint, data)
			base = child(home(), "backfill")
			base.mkdir(mode=0o700, exist_ok=True)
			base.chmod(0o700)
			bid = store.create_batch(scope, tasks, base / "report.md")
			path = child(base, "compare-" + bid + "/report.md")
			with store.conn:
				store.conn.execute("update c4_batches set report=? where id=?", (str(path), bid))
			return prepare(store, bid, sources, repo, plans)
	finally:
		store.close()
