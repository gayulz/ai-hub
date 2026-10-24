"""승인 전 결과와 블록 비교 보고, 승인된 캐시의 볼트 반영·재읽기."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from . import vault, work_records
from .backfill_common import child, clean, locked, now, private_write, sha
from .config import ConfigError, Repo, Sources, home


def target(sources: Sources, relative: str) -> Path:
	path = child(sources.vault, relative)
	if path.parent not in vault.landing_dirs(sources.vault):
		raise ConfigError("볼트의 정해진 착지점 밖입니다")
	if not path.parent.is_dir():
		raise ConfigError("볼트 착지점이 없습니다. 새 폴더는 만들지 않습니다")
	return path


def result(sources: Sources, task: dict, relative: str, body: str, method: str, **extra) -> dict:
	path = target(sources, relative)
	block = task.get("block_key", task["key"])
	before = vault.block_body(path, block)
	body = clean(body)
	return {"target": relative, "block": block, "body": body, "hash": sha(body),
		"before": clean(before), "before_hash": sha(before), "method": method, **extra}


def audit_batch(store, bid: str, sources: Sources) -> list[str]:
	missing = []
	for item in store.items(bid):
		prepared = item["data"].get("result")
		if not prepared or item["state"] not in ("prepared", "applied", "existing"):
			continue
		path = target(sources, prepared["target"])
		body = vault.block_body(path, prepared["block"])
		if prepared["method"] == "existing":
			if not body or any(commit not in body for commit in prepared.get("commit_refs", [])):
				missing.append(item["key"])
		elif sha(body) != prepared["hash"]:
			missing.append(item["key"])
	return missing


def report(store, bid: str, sources: Sources, *, write: bool = True) -> str:
	batch = store.batch(bid)
	scope = store.scope(batch["scope"])
	items = store.items(bid)
	counts = Counter(item["state"] for item in items)
	methods = Counter(i["data"].get("result", {}).get("method", "보류") for i in items)
	calls = store.rows("c4_calls", "batch=?", (bid,))
	local = sum(bool(c.get("local", 0)) for c in calls)
	call_states = Counter(c["state"] for c in calls)
	today = store.rows("c4_calls", "day=?", (now().date().isoformat(),))
	subs_today = sum(not c.get("local", 0) for c in today)
	used = {row["key"] for row in store.conn.execute(
		"select i.key from c4_items i join c4_batches b on i.batch=b.id where b.scope=?", (scope["id"],))}
	left = sum(t.get("parts", 0) for t in scope["data"]["tasks"] if t["key"] not in used)
	text = (f"# C4 묶음 {bid}\n\n프로젝트: {scope['project']} · 자료 종류: {scope['kind']}\n\n"
		f"고정 기간: {scope['since']} ~ {scope['until']} (문서·커밋은 기간 제한 없음)\n\n"
		f"전체 목록 {len(scope['data']['tasks'])}건 · 이번 대상 {len(items)}건 · 상태 {batch['state']}\n\n"
		f"준비 {counts['prepared']} · 기존 기록 {counts['existing']} · 반영 {counts['applied']} · "
		f"보류 {counts['held']} · 최소 기록 0 · 미분류 {scope['data'].get('unclassified', 0)}\n\n"
		f"정제 방식별: {dict(methods)}\n\n호출 성공 {call_states['success']} · 실패 {call_states['failed']} · "
		f"결과 불명 {call_states['reserved']}\n\n이번 구독 호출 {len(calls) - local}회 (실패·불명 포함) · "
		f"당일 C4 구독 누계 {subs_today}회 · 로컬 호출 {local}회 · 다음 묶음 예상 최소 호출 {left}회\n\n"
		f"AGY 전부 보류: {scope['data'].get('agy_count', 0)}개, 별도 보고서 backfill/agy-report.md\n\n"
		f"승인 회신 문구: `C4 {bid} 반영해`\n\n실행 명령: `python3 -m collector backfill --apply {bid}`\n")
	for warning in scope["data"].get("warnings", []):
		text += "\n- 목록 주의: " + warning + "\n"
	if scope["kind"] == "repos":
		text += (f"\nGit 루트 {scope['data'].get('git_roots', 0)}개 · 본인 커밋 {scope['data'].get('commits', 0)}건 · "
			f"현재 저자 기준 밖의 커밋 {scope['data'].get('excluded_commits', 0)}건\n")
	for deferred in scope["data"].get("deferred", []):
		text += f"\n- 목록 보류 `{deferred['id']}`: {deferred['reason']} · {deferred['source']}\n"
	for item in items:
		task = item["data"]
		text += f"\n## {item['key']}\n\n상태: {item['state']}\n"
		if task.get("hold_reason") or task.get("reason"):
			text += "\n보류 이유: " + (task.get("hold_reason") or task["reason"]) + "\n"
		prepared = task.get("result")
		if prepared:
			text += (f"\n노트: `{prepared['target']}`\n\n정제 방식: {prepared['method']} · "
				f"요금제: {prepared.get('plans', '없음')} · 모델: {prepared.get('models', '미확인')}\n\n"
				f"원천: {prepared.get('source', task.get('archive', task.get('relative', '로컬 Git 이력')))}\n\n"
				f"원천 소실: {prepared.get('original_missing', '해당 없음')}\n\n"
				f"변경 전:\n\n````markdown\n{clean(prepared['before'])}\n````\n\n"
				f"준비 결과(변경 후):\n\n````markdown\n{prepared['body']}\n````\n")
	if batch["state"] in ("applied", "partial"):
		text += f"\n반영 후 블록 대조 불일치: {len(audit_batch(store, bid, sources))}건\n"
	path = Path(batch["report"])
	if write:
		private_write(path, clean(text))
	print(f"묶음 {bid} · 프로젝트 {scope['project']} · {scope['kind']} · {scope['since']} ~ {scope['until']} · 대상 {len(items)}건")
	print(f"준비 {counts['prepared']} · 기존 {counts['existing']} · 보류 {counts['held']} · "
		f"반영 {counts['applied']} · 구독 호출 {len(calls) - local}회 / 당일 {subs_today}회 · "
		f"로컬 호출 {local}회 · 남은 예상 {left}회")
	print(f"준비 결과: {path}")
	print(f"승인 회신: C4 {bid} 반영해")
	return str(path)


def apply_batch(store, bid: str, sources: Sources, repo: Repo) -> None:
	items = store.items(bid)
	with locked("vaultsync", exact=home() / "run/vaultsync.lock"):
		# 이미 생긴 충돌은 쓰기 전에 발견해 부분 반영을 줄인다.
		for item in items:
			prepared = item["data"].get("result")
			if not prepared or item["state"] not in ("prepared", "applied", "existing"):
				continue
			path = target(sources, prepared["target"])
			current = vault.block_body(path, prepared["block"])
			if sha(prepared["body"]) != prepared["hash"]:
				raise ConfigError("준비 결과 해시가 다릅니다")
			if prepared["method"] == "existing":
				if not current or any(c not in current for c in prepared.get("commit_refs", [])):
					raise ConfigError("참조한 상시 기록이 없어졌거나 변경됐습니다")
			elif sha(current) not in (prepared["before_hash"], prepared["hash"]):
				raise ConfigError("준비 이후 기존 블록이 변경됐습니다. --resume 결과를 확인하세요")
		for item in items:
			task = item["data"]
			prepared = task.get("result")
			if not prepared or item["state"] not in ("prepared", "applied", "existing"):
				continue
			if prepared["method"] == "existing":
				store.save(bid, task, "existing")
				store.release(task["key"], "backfill:" + bid)
				continue
			path = target(sources, prepared["target"])
			day = date.fromisoformat(task.get("day", now().date().isoformat()))
			initial = None
			if task["type"] == "document":
				initial = (f"---\ntype: reference\nsensitivity: {'company' if repo.cls == 'company' else 'personal'}\n"
					f"created: {now().date()}\ntags: [backfill-index]\n---\n\n# 문서 소급 색인\n")
			vault.upsert_block(path, prepared["block"], prepared["body"], clean(repo.name).replace("\n", " "),
				day, repo.cls, expected_hash=prepared["before_hash"], initial=initial)
			if sha(vault.block_body(path, prepared["block"])) != prepared["hash"]:
				raise ConfigError("볼트 재읽기 해시가 다릅니다")
			if task["type"] == "session":
				from .backfill_sessions import load
				work_records.save(store, load(task), sources, repo.cls)
				store.put(task["key"], task["tool"], task["sid"], repo.cls, task["hash"], "recorded",
					",".join(prepared.get("plans", [])) or None, prepared["target"])
			store.save(bid, task, "applied")
			store.release(task["key"], "backfill:" + bid)
	rows = store.items(bid)
	for item in rows:
		if item["state"] not in ("applied", "existing"):
			# 이 묶음이 쓰지 않는 대화(보류 등)는 선점을 풀어 매시 수집이 기록하게 한다
			store.release(item["key"], "backfill:" + bid)
	states = {i["state"] for i in rows}
	store.state(bid, "applied" if states <= {"applied", "existing"} else "partial")
	report(store, bid, sources)
