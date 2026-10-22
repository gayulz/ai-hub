"""수집 한 바퀴: 원천 읽기 → 분류 → 요약(또는 최소 기록) → 볼트 덩어리 쓰기 → 원장 갱신."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import render, repos, summarize, vault
from .config import ConfigError, Plans, Rule, Sources, classify, home, load_plans, load_sources
from .ledger import Ledger
from .redact import redact
from .sessions import Session, read_claude, read_codex
from .sessions_other import read_agy, read_gemini

LOOKBACK_DAYS = 2
ACTIVE_MINUTES = 15


def collect(since: date) -> tuple[list[Session], list[str]]:
	"""시작한 날이 since 이후인 대화. AGY 자동 호출 제외 목록도 함께 돌려준다."""
	since_dt = datetime.combine(since, time.min).astimezone()
	sessions = read_claude(since_dt) + read_codex(since_dt) + read_gemini(since_dt)
	agy, excluded = read_agy(since_dt)
	sessions += agy
	return [s for s in sessions if s.started.date() >= since], excluded


def redacted(session: Session) -> Session:
	turns = [replace(t, text=redact(t.text)) for t in session.turns]
	return replace(session, turns=turns, title=redact(session.title))


def digest(session: Session) -> str:
	"""비밀값을 뺀 뒤의 내용 해시. 같으면 AI를 다시 부르지 않는다."""
	payload = json.dumps([[t.role, t.text] for t in redacted(session).turns], ensure_ascii=False)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def window(since_arg: date | None, today: date) -> date:
	return since_arg or today - timedelta(days=LOOKBACK_DAYS)


def _heading(rule: Rule) -> str:
	return rule.name or "기타"


class Pipeline:
	def __init__(self, sources: Sources, plans: Plans | None, ledger: Ledger, dry_run: bool):
		self.sources = sources
		self.plans = plans
		self.ledger = ledger
		self.dry_run = dry_run
		self.counts: Counter[str] = Counter()
		self.excluded_titles: list[str] = []
		self.failed_plans: set[str] = set()
		self.now = datetime.now().astimezone()
		self.today = self.now.date()

	def run(self, since: date) -> None:
		if not self.dry_run:
			for folder in vault.landing_dirs(self.sources.vault):
				folder.mkdir(parents=True, exist_ok=True)
		sessions, excluded = collect(since)
		self.excluded_titles = excluded
		for session in sessions:
			self.handle_session(session)
		self.handle_repos(since)
		self.write_check(since)

	def handle_session(self, session: Session) -> None:
		rule = classify(session.cwd, self.sources.rules)
		if rule is None:
			self.counts["unclassified"] += 1
			return
		if rule.cls == "exclude":
			self.counts["excluded"] += 1
			return
		if self.now - session.last < timedelta(minutes=ACTIVE_MINUTES):
			self.counts["active"] += 1
			return
		self.counts[f"seen:{session.tool}:{rule.cls}"] += 1
		digest_now = digest(session)
		row = self.ledger.get(session.key)
		if row is not None and row["hash"] == digest_now and row["status"] == "recorded":
			self.counts["same"] += 1
			return
		if self.dry_run:
			self.counts["would_record"] += 1
			return
		summary, plan_id, reason = self.summarize(session, rule.cls)
		body = render.session_body(redacted(session), summary, plan_id, reason)
		vault.upsert_block(vault.daily_path(self.sources.vault, rule.cls, session.started.date()),
			session.key, body, _heading(rule), session.started.date(), rule.cls)
		status = "recorded" if summary else "minimal"
		self.ledger.put(session.key, session.tool, session.sid, rule.cls, digest_now, status, plan_id, reason or None)
		self.counts[status] += 1

	def summarize(self, session: Session, cls: str) -> tuple[str | None, str | None, str]:
		"""routing 순서대로 부르고, 실패하면(한도 초과 등) 다음 요금제로 넘어간다.

		한 번 실패한 요금제는 이 실행이 끝날 때까지 다시 부르지 않는다(한도가 찬 요금제를 대화마다 두드리지 않게).
		"""
		today = self.today.isoformat()
		prompt = summarize.build_prompt(redacted(session))
		reason = "사용 가능한 요금제 없음"
		for plan in summarize.usable_plans(cls, self.plans, self.ledger, today, self.failed_plans):
			self.ledger.bump_call(plan.id, today)
			try:
				return summarize.call(plan, prompt), plan.id, ""
			except summarize.SummarizeError as exc:
				self.failed_plans.add(plan.id)
				reason = f"호출 실패({exc})"
		return None, None, reason

	def handle_repos(self, since: date) -> None:
		day = since
		while day <= self.today:
			for repo in self.sources.repos:
				path = Path(repo.path)
				if not path.is_dir():
					self.counts["repo_missing"] += 1
					continue
				commits = repos.commits_on(path, day)
				docs = repos.docs_on(path, repo.doc_dirs, day)
				self.counts["commits"] += len(commits)
				self.counts["doc_changes"] += len(docs)
				if self.dry_run or not (commits or docs):
					continue
				target = vault.daily_path(self.sources.vault, repo.cls, day)
				if commits:
					vault.upsert_block(target, f"repo:{repo.name}:commits", render.commit_body(commits),
						repo.name, day, repo.cls)
				if docs:
					vault.upsert_block(target, f"repo:{repo.name}:docs", render.docs_body(docs),
						repo.name, day, repo.cls)
			day += timedelta(days=1)

	def unregistered(self) -> list[str]:
		registered = {r.path for r in self.sources.repos}
		return repos.find_unregistered(Path.home(), registered, set(self.sources.ignore_repos))

	def write_check(self, since: date) -> None:
		if self.dry_run:
			return
		unrecorded, unclassified, waiting = self.audit(since)
		body = render.check_body(self.now, unrecorded, unclassified, len(self.unregistered()), waiting)
		target = vault.daily_path(self.sources.vault, "personal", self.today)
		vault.upsert_block(target, "check", body, "ai-hub 수집기", self.today, "personal")

	def audit(self, since: date) -> tuple[int, int, int]:
		"""(미기록, 미분류, 요약 대기). 활동 중인 대화는 셈에 넣지 않는다."""
		sessions, _ = collect(since)
		unrecorded = unclassified = waiting = 0
		for session in sessions:
			rule = classify(session.cwd, self.sources.rules)
			if rule is None:
				unclassified += 1
				continue
			if rule.cls == "exclude" or self.now - session.last < timedelta(minutes=ACTIVE_MINUTES):
				continue
			row = self.ledger.get(session.key)
			if row is None or row["hash"] != digest(session):
				unrecorded += 1
			elif row["status"] == "minimal":
				waiting += 1
		return unrecorded, unclassified, waiting


def load_all() -> tuple[Sources, Plans | None, list[str]]:
	"""설정 읽기. plans.toml이 없거나 틀리면 요약 없이 최소 기록만 남기고 경고를 알린다."""
	sources = load_sources()
	warnings: list[str] = []
	if not (home() / "redact_hosts.toml").exists():
		warnings.append("redact_hosts.toml 이 없어 사내 호스트 마스킹을 건너뜁니다")
	try:
		plans: Plans | None = load_plans()
	except ConfigError as exc:
		plans = None
		warnings.append(f"요금제 설정을 쓰지 않습니다(최소 기록만): {exc}")
	return sources, plans, warnings


def open_ledger() -> Ledger:
	return Ledger(home() / "ledger.db")
