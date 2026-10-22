"""한 번 실행의 진행: 준비 → 구현 → 기계 검증 → 독립 검토 → (보완 → 검증 → 검토) → 승인 대기.

예산(호출 수·시간·보완 횟수)은 호출 전에 확인하고, 다 쓰면 새 호출을 시작하지 않는다.
공급자 전환은 단계마다 한 번, plans.toml 에 있고 personal 이 허용된 요금제끼리만 한다.
단계가 끝날 때마다 run.json 에 저장하므로 끊겨도 그 자리에서 이어 간다.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from collector.config import Plan, Plans, run_dir

from . import checks, prompts
from .providers import ProviderError, call
from .record import Run, now

ROLE_KEYS = {"implement": "implementer", "review": "reviewer"}
BLOCKING = ("blocker", "major")


def role_plans(plans: Plans, orchestration: dict, role: str) -> list[Plan]:
	"""역할 담당 요금제 순서. [orchestration.models] 로 요약용 모델(예: 저가 모델) 대신 쓸 모델을 정한다("" 는 CLI 기본값)."""
	models = orchestration.get("models", {})
	found = []
	for plan_id in orchestration.get(ROLE_KEYS[role], []):
		plan = plans.plans.get(plan_id)
		if plan is None or "personal" not in plan.allow:
			continue
		found.append(replace(plan, model=models[plan_id] or None) if plan_id in models else plan)
	return found


def worktrees_dir() -> Path:
	return run_dir() / "worktrees"


class Flow:
	def __init__(self, run: Run, plans: Plans, orchestration: dict, caller=None):
		self.run, self.plans, self.orchestration = run, plans, orchestration
		self.caller = caller or call
		self.started = time.monotonic()

	def save(self) -> None:
		moment = time.monotonic()
		self.run.elapsed += moment - self.started
		self.started = moment
		self.run.save()

	def run_all(self) -> Run:
		self.run.settle_interrupted()
		steps = {"prepare": self.prepare, "implement": self.implement, "verify": self.verify,
			"review": self.review, "fix": self.fix}
		while self.run.status == "작업 중":
			steps[self.run.step]()
		self.save()
		return self.run

	def ask(self, role: str, prompt: str, schema: dict, writer: bool) -> dict | None:
		"""역할 담당에게 묻는다. 실패하면 다음 요금제로 한 번만 넘어간다. 못 받으면 실행을 끝내고 None."""
		r = self.run
		for plan in role_plans(self.plans, self.orchestration, role)[:2]:
			if r.seconds_left() <= 0:
				r.finish("시간 초과", f"{role} 호출 전에 시간 한도({r.budget['seconds']}초)를 다 썼습니다. 이어 가려면 resume --minutes")
				return None
			if r.calls_left() <= 0:
				r.finish("호출 한도 소진", f"{role} 호출 전에 호출 한도({r.budget['calls']}회)를 다 썼습니다. 이어 가려면 resume --calls")
				return None
			record = {"role": role, "plan": plan.id, "tool": plan.tool, "started": now(), "ok": None}
			r.calls.append(record)
			r.agent, r.current_action = plan.tool, f"{role} · {plan.id}"
			r.event("call", role=role, plan=plan.id)
			self.save()
			try:
				reply = self.caller(plan, writer, prompt, Path(r.worktree), schema, r.seconds_left())
			except ProviderError as exc:
				record.update(ok=False, error=f"{exc.kind}: {exc}")
				r.event("call_failed", role=role, plan=plan.id, error_kind=exc.kind)
				self.save()
				continue
			record.update(ok=True, seconds=round(reply.seconds, 1), usage=reply.usage)
			self.save()
			return reply.data
		if r.status == "작업 중":
			if r.seconds_left() < 60:  # 남은 시간이 거의 없어 호출이 시간 초과로 끝난 경우
				r.finish("시간 초과", f"{role} 호출 중 시간 한도({r.budget['seconds']}초)를 다 썼습니다. 이어 가려면 resume --minutes")
			else:
				r.finish("실패", f"{role} 담당 호출이 모두 실패했습니다(요금제·로그인 확인 필요)")
		return None

	def prepare(self) -> None:
		r = self.run
		r.base_commit = checks.git(r.repo, "rev-parse", "HEAD").strip()
		r.branch = f"orch/{r.run_id}"
		worktree = worktrees_dir() / r.run_id
		if not worktree.exists():
			worktree.parent.mkdir(parents=True, exist_ok=True)
			checks.git(r.repo, "worktree", "add", "-q", "-b", r.branch, str(worktree), r.base_commit)
		r.worktree, r.step = str(worktree), "implement"
		r.event("prepare", base=r.base_commit[:12])
		self.save()

	def _schema(self) -> dict:
		return prompts.ANALYST_SCHEMA if self.run.kind == "analysis" else prompts.WORKER_SCHEMA

	def absorb(self, data: dict) -> None:
		r = self.run
		if r.kind == "analysis":
			r.document = data.get("document", "")
			(r.folder / "document.md").write_text(r.document, encoding="utf-8")
			r.handoff = {"summary": data.get("summary", ""), "blockers": data.get("blockers", [])}
		else:
			keys = ("summary", "changed_files", "verification", "blockers", "next_required_step")
			r.handoff = {key: data.get(key) for key in keys}
			_, outside = checks.commit_owned(Path(r.worktree), r.request.get("owned_paths", []),
				f"orchestrator {r.run_id} 시도 {r.fixes + 1}")
			r.handoff["outside_paths"] = outside
		r.event("implemented", attempt=r.fixes, summary=str(data.get("summary", ""))[:300])

	def implement(self) -> None:
		r = self.run
		data = self.ask("implement", prompts.implement(r.request, r.kind), self._schema(), r.kind == "code")
		if data is None:
			return
		self.absorb(data)
		r.step = "verify"
		self.save()

	def verify(self) -> None:
		r = self.run
		if r.kind == "analysis":
			r.checks = [{"name": "문서", "ok": bool(r.document.strip()), "detail": f"{len(r.document)}자"}]
		else:
			r.checks = checks.verify(Path(r.worktree), r.base_commit, r.request, r.handoff.get("outside_paths", []))
		r.event("verified", ok=checks.checks_passed(r.checks))
		r.step = "review"
		self.save()

	def review(self) -> None:
		r = self.run
		material = r.document if r.kind == "analysis" else checks.git(r.worktree, "diff", r.base_commit, "HEAD")
		if not material.strip():
			r.finish("실패", "구현 담당이 아무것도 만들지 않았습니다")
			return
		data = self.ask("review", prompts.review(r.request, r.kind, material, r.checks), prompts.REVIEW_SCHEMA, False)
		if data is None:
			return
		(r.folder / f"review-{r.fixes}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
		findings = data.get("findings", [])
		blocking = [f for f in findings if f.get("severity") in BLOCKING]
		r.handoff["minor_findings"] = [f for f in findings if f.get("severity") not in BLOCKING]
		r.review_summary = data.get("summary", "")
		r.event("reviewed", verdict=data.get("verdict"), blocking=len(blocking), total=len(findings))
		if not checks.checks_passed(r.checks):
			failed = [c["name"] for c in r.checks if not c["ok"]]
			blocking.append({"id": "checks", "severity": "blocker", "file": "", "line": 0,
				"problem": "기계 검증 실패: " + ", ".join(failed), "evidence": "실행기 검사 결과", "suggestion": "실패한 검사를 통과시킨다"})
		r.open_findings = blocking
		if not blocking:
			r.finish("승인 필요", "독립 검토와 기계 검증을 모두 통과했습니다")
		elif r.fixes >= r.budget["fixes"]:
			r.finish("실패", f"보완 {r.fixes}회 뒤에도 열린 지적 {len(blocking)}건이 남았습니다")
		else:
			r.step = "fix"
			self.save()

	def fix(self) -> None:
		r = self.run
		previous = r.document if r.kind == "analysis" else ""
		prompt = prompts.fix(r.request, r.kind, r.open_findings, r.checks, previous)
		data = self.ask("implement", prompt, self._schema(), r.kind == "code")
		if data is None:
			return
		r.fixes += 1
		self.absorb(data)
		r.step = "verify"
		self.save()
