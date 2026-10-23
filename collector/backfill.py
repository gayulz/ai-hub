"""프로젝트 하나의 소급 결과 준비·재개·승인 반영. 일반 실행은 볼트에 쓰지 않는다."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from . import backfill_agy as agy
from . import backfill_output as output
from . import backfill_repos as repos
from . import backfill_sessions as sessions
from . import pipeline, render, session_summaries, summarize, vault
from .backfill_common import child, clean, locked, now, numbered, packed, sha
from .backfill_store import Store
from .config import ConfigError, classify, home, load_plans, load_sources

DEFAULT_SINCE = date(2026, 7, 22)


def project(sources, pid):
	matching = [r for r in sources.repos if r.id == pid]
	if len(matching) != 1:
		raise ConfigError("승인된 원천 ID가 아닙니다. sources.toml의 repo.id를 확인하세요")
	repo = matching[0]
	if not Path(repo.path).is_absolute():
		raise ConfigError("소급 원천에는 절대 경로를 등록해야 합니다")
	rule = classify(repo.path, sources.rules)
	if rule and rule.cls != repo.cls:
		raise ConfigError("원천 경로 규칙과 프로젝트 분류가 충돌합니다")
	return repo


def policy(sources, plans, repo) -> str:
	settings = asdict(plans) if plans else None
	if settings:
		# 기존 구독 전용 묶음의 설정 지문을 유지한다.
		for plan in settings["plans"].values():
			if plan["tool"] != "ollama":
				plan.pop("url")
				plan.pop("timeout")
	return sha(packed({"project": asdict(repo), "rules": [asdict(r) for r in sources.rules],
		"vault": str(sources.vault), "plans": settings}))


def inventory(repo, sources, kind, since, until) -> dict:
	if kind == "repos":
		return repos.inventory(repo, sources)
	data = sessions.inventory(repo, sources, since, until)
	data["agy"] = agy.metadata(since, until)
	return data


def validate_source(task: dict, repo, sources) -> str:
	try:
		if task["type"] == "document":
			if sha(child(Path(repo.path), task["relative"]).read_bytes()) != task["hash"]:
				return "목록 작성 뒤 문서 내용이 변경됨. 새 준비와 확인 필요"
		elif task["type"] == "session":
			if sessions.archive_stale(task):
				return "원천 수정 시각과 보관함 사본이 다름. 보관함 갱신 후 새 준비 필요"
			session = sessions.load(task)
			_, reason = sessions.project_reason(session, repo, sources)
			if reason:
				return reason
			if pipeline.digest(session) != task["hash"] or sha("".join(sessions.prompts(session)[0])) != task["material"]:
				return "목록 작성 뒤 대화 내용이 변경됨. 새 준비와 확인 필요"
	except (OSError, ValueError, EOFError, ConfigError) as exc:
		return f"원천 사본 조회 보류: {type(exc).__name__}"
	return ""


def index_path(store, sources, repo) -> str:
	rows = store.rows("c4_indexes", "project=?", (repo.id,))
	if rows:
		return rows[0]["path"]
	folder = sources.vault / vault.LANDING[repo.cls]
	output.target(sources, str((folder / "index.md").relative_to(sources.vault)))
	with locked("backfill-index-paths"):
		path = numbered(folder, "backfill-index")
		reserved = {r["path"] for r in store.rows("c4_indexes")}
		while str(path.relative_to(sources.vault)) in reserved:
			number = int(path.name[7:9]) + 1
			if number > 99:
				raise ConfigError("오늘 생성 순번을 모두 사용했습니다")
			path = path.with_name(path.name[:7] + f"{number:02d}" + path.name[9:])
		rel = path.relative_to(sources.vault).as_posix()
		with store.conn:
			store.conn.execute("insert into c4_indexes values (?, ?)", (repo.id, rel))
	return rel


def prepare_repo(store, bid, task, scope, sources, repo):
	if task["type"] == "document":
		return output.result(sources, task, index_path(store, sources, repo), task["body"], "document-index")
	day = date.fromisoformat(task["day"])
	path = vault.daily_path(sources.vault, repo.cls, day)
	rel = path.relative_to(sources.vault).as_posix()
	output.target(sources, rel)
	c1key = f"repo:{repo.name}:commits"
	previous = vault.block_body(path, c1key)
	shorts = re.findall(r"`([0-9a-f]{7,40})`", previous)
	all_commits = [c["sha"] for t in scope["data"]["tasks"] for c in t.get("commits", [])]
	covered = set()
	for short in shorts:
		matches = {full for full in all_commits if full.startswith(short)}
		if len(matches) > 1:
			raise ConfigError("기존 커밋의 짧은 SHA 대응이 모호합니다")
		covered.update(matches)
	fresh = [c for c in task["commits"] if c["sha"] not in covered]
	if not fresh:
		reference = dict(task, block_key=c1key)
		return output.result(sources, reference, rel, previous, "existing", commit_refs=shorts)
	body = repos.commit_body(dict(task, commits=fresh))
	current = vault.block_body(path, task["block_key"])
	# 이전 C4 반영분은 같은 일자의 전체 SHA 집합에 남긴다.
	known = {c["sha"] for c in fresh}
	kept = [line for line in current.splitlines() if
		any(full not in known for full in re.findall(r"`([0-9a-f]{40})`", line))]
	body = "\n".join(kept + [body])
	return output.result(sources, task, rel, body, "commits")


def summarize_session(store, bid, task, session, plans, failed, *, local=None, prompts=None):
	if plans is None:
		return None, "사용 가능한 요금제 없음"
	if prompts is None:
		prompts, reason = sessions.prompts(session)
		if reason:
			return None, reason
	results = []
	for part, prompt in enumerate(prompts):
		calls = store.work_calls(bid, task)
		current = [c for c in calls if c["part"] == part and
			(local is None or bool(c.get("local", 0)) == local)]
		if any(c["state"] == "reserved" for c in current):
			return None, "호출 결과 불명: 사용량은 유지하며 자동 재호출하지 않음"
		ready = [c for c in current if c["state"] == "success"]
		if ready:
			results.append(json.loads(ready[0]["result"]))
			continue
		attempted = {c["plan"] for c in current}
		routing = plans.backfill_routing.get(task["cls"], plans.routing.get(task["cls"], ()))
		reason = "사용 가능한 요금제 없음 또는 이전 호출 실패로 보류"
		for pid in routing:
			plan = plans.plans[pid]
			if local is not None and (plan.tool == "ollama") != local:
				continue
			if pid in failed or pid in attempted or task["cls"] not in plan.allow:
				continue
			cid = store.reserve(bid, task, part, plan, plans.backfill_daily_limit)
			if cid is None:
				reason = "C4 구독 또는 요금제의 당일 호출 상한 도달"
				continue
			started = time.monotonic()
			try:
				metadata = {}
				text = (summarize.call(plan, prompt, metadata=metadata) if local is not None
					else summarize.call(plan, prompt))
				if local is not None:
					from .backfill_compare_report import checks
					checked = checks(prompt, text)
				text = clean(re.sub(r"```.*?(?:```|\Z)", "", text, flags=re.S).strip())
				if not text:
					raise summarize.SummarizeError("빈 정제 결과")
			except (summarize.SummarizeError, OSError, subprocess.TimeoutExpired) as exc:
				failed.add(pid)
				reason = "호출 실패: " + type(exc).__name__
				if local is True:
					reason += " · Ollama 실행·모델·timeout을 확인하고 `ollama serve`와 `ollama list`를 실행하세요"
				elif local is False:
					reason += " · CLI 상태를 확인하세요. 모델 식별에 실패하면 plans.toml에 model을 지정하세요"
				store.finish_call(cid, "failed", {"reason": reason, "plan": clean(pid),
					"model": clean(plan.model or "미확인(CLI 기본값)"), "seconds": time.monotonic() - started})
				continue
			result = {"summary": text, "plan": clean(pid), "model": clean(metadata.get("model") or plan.model or "미확인(CLI 기본값)"),
				"seconds": time.monotonic() - started}
			if local is not None:
				result["checks"] = checked
			store.finish_call(cid, "success", result)
			results.append(result)
			break
		else:
			return None, reason
	return results, ""


def cached_session(store, task, bid=""):
	for cached in store.rows("c4_items", "key=? and hash=? and batch!=? order by rowid desc", (task["key"], task["hash"], bid)):
		data = json.loads(cached["data"])
		ready = data.get("result")
		if ready and ready["method"] != "existing" and data.get("material") == task["material"] and data.get("cls") == task["cls"]:
			return ready
	return None


def prepare_session(store, bid, task, sources, repo, plans, until, failed):
	session = sessions.load(task)
	path = vault.daily_path(sources.vault, repo.cls, date.fromisoformat(task["day"]))
	rel = path.relative_to(sources.vault).as_posix()
	output.target(sources, rel)
	row = store.get(task["key"])
	previous = vault.block_body(path, task["key"])
	if row and row["hash"] == task["hash"] and row["status"] == "recorded" and previous:
		return output.result(sources, task, rel, previous, "existing"), ""
	ready = cached_session(store, dict(task, cls=repo.cls), bid)
	if ready:
		return output.result(sources, task, rel, ready["body"], "cached-ai", plans=ready.get("plans", []),
			models=ready.get("models", []), source=ready.get("source", "기존 준비 결과")), ""
	matched = session_summaries.match_summary(session, until)
	if matched:
		summary, method, used, models = matched["summary"], "codex-summary", [], ["기존 요약, 모델 미확인"]
	else:
		parts, reason = summarize_session(store, bid, task, session, plans, failed)
		if parts is None:
			return None, reason
		summary = "\n".join(p["summary"] for p in parts)
		method, used, models = "ai", [p["plan"] for p in parts], [p["model"] for p in parts]
	body = render.session_body(pipeline.redacted(session), summary, ",".join(used) or "기존 요약 재사용")
	body += f"\n  - 정제 방식: {method} · 모델: {', '.join(models)}"
	return output.result(sources, task, rel, body, method, plans=used, models=models,
		source=matched["source"] if matched else "archive/" + task["archive"],
		summary_hash=matched["source_hash"] if matched else None,
		original_missing=sessions.original_missing(task)), ""


def prepare(store, bid, sources, repo, plans):
	batch = store.batch(bid)
	scope = store.scope(batch["scope"])
	until = datetime.fromisoformat(scope["until"])
	failed = {c["plan"] for c in store.rows("c4_calls", "batch=? and state='failed'", (bid,))}
	for item in store.items(bid):
		task = item["data"]
		if item["state"] in ("prepared", "applied", "existing"):
			reason = validate_source(task, repo, sources)
			if reason:
				task["hold_reason"] = reason
				store.save(bid, task, "held")
				continue
			prepared = task["result"]
			current = vault.block_body(output.target(sources, prepared["target"]), prepared["block"])
			if item["state"] == "prepared" and sha(current) not in (prepared["hash"], prepared["before_hash"]):
				task["hold_reason"] = "준비 이후 기존 블록이 변경됨. 새 프로젝트 명령으로 다시 확인 필요"
				store.save(bid, task, "held")
				continue
			if item["state"] == "applied" and not current and prepared["method"] != "existing":
				prepared.update(before="", before_hash=sha(""))
				store.save(bid, task, "prepared")
			continue
		task["cls"] = repo.cls
		reason = task.get("reason", "") or validate_source(task, repo, sources)
		if task["type"] == "session" and not store.claim(task["key"], "backfill:" + bid):
			reason = "같은 대화를 다른 수집 작업이 선점 중"
		if task["type"] == "error":
			reason = task["reason"]
		try:
			if not reason:
				store.save(bid, task, "processing")
				if task["type"] == "session":
					prepared, reason = prepare_session(store, bid, task, sources, repo, plans, until, failed)
				else:
					prepared = prepare_repo(store, bid, task, scope, sources, repo)
				if not reason:
					task["result"] = prepared
					task.pop("hold_reason", None)
					store.save(bid, task, "existing" if prepared["method"] == "existing" else "prepared")
					continue
		except (OSError, ValueError, ConfigError) as exc:
			reason = f"준비 보류: {type(exc).__name__}: {clean(str(exc))}"
		task["hold_reason"] = reason
		store.save(bid, task, "held")
	states = {i["state"] for i in store.items(bid)}
	if states <= {"applied", "existing"} and batch["state"] in ("applied", "partial"):
		state = "applied"
	else:
		state = "prepared" if states & {"prepared", "existing"} else "held"
	store.state(bid, state)
	output.report(store, bid, sources)


def choose_tasks(store, scope, size):
	used = {r[0] for r in store.conn.execute("select i.key from c4_items i "
		"join c4_batches b on i.batch=b.id where b.scope=?", (scope["id"],))}
	remaining = [t for t in scope["data"]["tasks"] if t["key"] not in used]
	if scope["kind"] == "sessions":
		return remaining[:size]
	docs = [t for t in remaining if t["type"] != "commits"]
	return docs + [t for t in remaining if t["type"] == "commits"][:10]


def changed_scope(store, scope, repo, sources):
	if any(validate_source(t, repo, sources) for t in scope["data"]["tasks"]
			if t["type"] in ("session", "document") and not t.get("reason")):
		return True
	for batch in store.rows("c4_batches", "scope=? and state not in ('applied','superseded')", (scope["id"],)):
		for item in store.items(batch["id"]):
			prepared = item["data"].get("result")
			if not prepared or prepared["method"] == "existing":
				continue
			current = vault.block_body(output.target(sources, prepared["target"]), prepared["block"])
			if sha(current) not in (prepared["hash"], prepared["before_hash"]):
				return True
	return False


def supersede(store, pid, kind):
	for scope in store.rows("c4_scopes", "project=? and kind=?", (pid, kind)):
		for batch in store.rows("c4_batches", "scope=? and state not in ('applied','superseded')", (scope["id"],)):
			store.state(batch["id"], "superseded")
			for item in store.items(batch["id"]):
				store.release(item["key"], "backfill:" + batch["id"])


def execute(*, project_id=None, kind=None, since=None, dry_run=False, resume=None, apply=None,
		compare=None, count=20) -> int:
	if compare:
		from .backfill_compare import execute as compare_execute
		return compare_execute(compare, count, dry_run=dry_run)
	sources = load_sources()
	plans = load_plans() if (home() / "plans.toml").exists() else None
	with_store = Store(home() / "ledger.db", readonly=dry_run)
	try:
		if dry_run:
			if (resume or apply) and with_store.scope(with_store.batch(resume or apply)["scope"])["kind"] == "compare":
				from .backfill_compare_report import report
				report(with_store, resume or apply, write=False)
				return 0
			return preview(with_store, sources, plans, project_id, kind, since, resume or apply)
		with locked("backfill"):
			store = with_store
			if resume or apply:
				bid = resume or apply
				batch = store.batch(bid)
				if batch["state"] == "superseded":
					raise ConfigError("새 목록으로 대체된 묶음입니다. 최신 준비 결과를 확인하세요")
				scope = store.scope(batch["scope"])
				repo = project(sources, scope["project"])
				if scope["policy"] != policy(sources, plans, repo):
					raise ConfigError("묶음 작성 뒤 설정이 변경됐습니다. 새 프로젝트 명령으로 준비하세요")
				if scope["kind"] == "compare":
					if apply:
						raise ConfigError("비교 묶음은 볼트에 반영할 수 없습니다. report.md에서 사람이 판정하세요")
					from .backfill_compare import prepare as compare_prepare
					return compare_prepare(store, bid, sources, repo, plans)
				if apply:
					for item in store.items(bid):
						if item["state"] not in ("prepared", "existing"):
							continue
						reason = validate_source(item["data"], repo, sources)
						if reason:
							raise ConfigError(reason)
					output.apply_batch(store, bid, sources, repo)
				else:
					prepare(store, bid, sources, repo, plans)
				return 0
			repo = project(sources, project_id)
			start = since or DEFAULT_SINCE
			fingerprint = policy(sources, plans, repo)
			scopes = store.rows("c4_scopes", "project=? and kind=? and since=? and policy=? order by rowid desc",
				(project_id, kind, start.isoformat(), fingerprint))
			if scopes and changed_scope(store, store.scope(scopes[0]["id"]), repo, sources):
				scopes = []
			if scopes:
				scope = store.scope(scopes[0]["id"])
				open_batches = store.rows("c4_batches", "scope=? and state in ('pending','prepared','held') order by rowid", (scope["id"],))
				if open_batches:
					output.report(store, open_batches[0]["id"], sources)
					return 0
			else:
				supersede(store, project_id, kind)
				until = now()
				data = inventory(repo, sources, kind, start, until)
				if kind == "sessions":
					path, count = agy.report(data.pop("agy"), sources, str(start), until.isoformat(), write=True)
					print(f"AGY {count}개 전부 보류 · 보고서: {path}")
					data["agy_count"] = count
				scope = store.create_scope(project_id, kind, str(start), until.isoformat(), fingerprint, data)
			tasks = choose_tasks(store, scope, plans.backfill_batch_size if plans else 10)
			if not tasks:
				batches = store.rows("c4_batches", "scope=? order by rowid desc", (scope["id"],))
				if batches:
					if output.audit_batch(store, batches[0]["id"], sources):
						prepare(store, batches[0]["id"], sources, repo, plans)
					else:
						output.report(store, batches[0]["id"], sources)
				print("고정 목록의 다음 묶음이 없습니다. 보류·미분류는 별도 확인이 필요합니다")
				for entry in scope["data"].get("deferred", []):
					print(f"목록 보류 {entry['id']}: {entry['reason']}")
				return 0
			bid = store.create_batch(scope, tasks, numbered(home() / "backfill", "backfill-result"))
			prepare(store, bid, sources, repo, plans)
			return 0
	finally:
		with_store.close()


def preview(store, sources, plans, pid, kind, since, bid):
	if bid:
		output.report(store, bid, sources, write=False)
		return 0
	repo = project(sources, pid)
	start, until = since or DEFAULT_SINCE, now()
	data = inventory(repo, sources, kind, start, until)
	reused, expected = 0, 0
	for task in data["tasks"]:
		if task["type"] != "session" or task.get("reason"):
			continue
		row = store.get(task["key"])
		path = vault.daily_path(sources.vault, repo.cls, date.fromisoformat(task["day"]))
		try:
			existing = vault.block_body(output.target(sources, path.relative_to(sources.vault).as_posix()), task["key"])
		except ConfigError:
			existing = ""
		if (row and row["hash"] == task["hash"] and row["status"] == "recorded" and existing) \
				or session_summaries.match_summary(sessions.load(task), until):
			reused += 1
		else:
			expected += task["parts"]
	print(f"[dry-run] 프로젝트 {pid} · {kind} · {start} ~ {until.isoformat()} · 대상 {len(data['tasks'])}건")
	print(f"기존 기록·요약 재사용 후보 {reused}건 · AI 예상 최소 호출 {expected}회 (대체 호출 별도)")
	print("AI 호출·볼트·원장·실행 이력 변경 없음")
	if kind == "sessions":
		_, count = agy.report(data["agy"], sources, str(start), until.isoformat(), write=False)
		print(f"AGY {count}개 전부 보류 · 미분류 {data.get('unclassified', 0)}건")
	for warning in data.get("warnings", []):
		print("목록 주의: " + clean(warning))
	return 0
