"""부서가 허용한 요금제로 PM 역할(접수 되묻기·계획)의 AI 를 부른다. 외부 AI 금지 부서는 이 길을 쓰지 않는다."""

from __future__ import annotations

import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from collector.config import Plan, home, load_plans
from orchestrator import providers

from .departments import Department

MAX_TRIES = 3
# AGY 는 읽기 전용 모드에서도 파일을 만들었다(10-25 시험 6회 중 6회). 저장소 폴더 대신 빈 임시 폴더에서 부른다.
WRITE_UNSAFE_TOOLS = ("agy",)


@dataclass(frozen=True)
class Answer:
	data: dict
	plan_id: str
	seconds: float


def pm_plans(department: Department) -> list[Plan]:
	"""[orchestration] 의 pm → planner → implementer 중 처음 있는 목록에서 부서가 허용한 요금제만 남긴다."""
	if not department.external_ai:
		raise ValueError("외부 AI 금지 부서는 PM 호출을 쓸 수 없습니다")
	with (home() / "plans.toml").open("rb") as stream:
		config = tomllib.load(stream).get("orchestration", {})
	order = config.get("pm") or config.get("planner") or config.get("implementer") or []
	plans, models = load_plans().plans, config.get("models", {})
	found = []
	for plan_id in order:
		plan = plans.get(plan_id)
		if plan is None or plan_id not in department.allow_plans or "personal" not in plan.allow:
			continue
		found.append(replace(plan, model=models[plan_id] or None) if plan_id in models else plan)
	return found


def ask(department: Department, prompt: str, schema: dict, repo: Path | None = None, timeout: float = 570) -> Answer:
	"""읽기 전용으로 앞 요금제부터 최대 3개까지 부른다. 실패 메시지에는 요금제와 원인 분류만 담는다."""
	plans = pm_plans(department)[:MAX_TRIES]
	if not plans:
		raise RuntimeError("부서가 허용한 PM 요금제가 없습니다")
	causes = []
	for plan in plans:
		with tempfile.TemporaryDirectory(prefix="agency-") as empty:
			cwd = Path(empty) if repo is None or plan.tool in WRITE_UNSAFE_TOOLS else repo
			try:
				reply = providers.call(plan, writer=False, prompt=prompt, cwd=cwd, schema=schema, timeout=timeout)
			except providers.ProviderError as exc:
				causes.append(str(exc))
				continue
		return Answer(reply.data, plan.id, reply.seconds)
	raise RuntimeError("PM 호출이 모두 실패했습니다: " + " / ".join(causes))
