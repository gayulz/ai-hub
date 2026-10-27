"""부서별 허용 저장소·요금제·스킬·예산 설정을 읽는다."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

from collector.config import ConfigError, home, load_plans
from orchestrator.targets import targets

from .skills import NAME

ID = re.compile(r"[a-z][a-z0-9-]{0,30}")
KEYS = {"id", "name", "scope", "repos", "allow_plans", "external_ai", "playbook", "skills",
	"review_skill", "outward", "required_questions", "budget"}
OUTWARD = ("merge", "push", "restart", "publish")
BUDGET_LIMITS = {"calls": 200, "minutes": 1440}


def valid_text(value, maximum: int) -> bool:
	return isinstance(value, str) and 1 <= len(value) <= maximum and bool(value.strip()) and "\x00" not in value


def valid_budget(value) -> bool:
	return (isinstance(value, dict) and value.keys() == BUDGET_LIMITS.keys()
		and all(type(value[key]) is int and 1 <= value[key] <= limit for key, limit in BUDGET_LIMITS.items()))


@dataclass(frozen=True)
class Department:
	id: str
	name: str
	scope: str
	repos: tuple[str, ...]
	allow_plans: tuple[str, ...]
	external_ai: bool
	playbook: str
	skills: tuple[str, ...]
	review_skill: str
	outward: tuple[str, ...]
	required_questions: tuple[str, ...]
	budget: dict[str, int]


def _invalid(label, reason: str) -> ConfigError:
	return ConfigError(f"departments.toml: {label}: {reason}")


def _department(row, index, repos, plans) -> Department:
	identity = row.get("id") if isinstance(row, dict) else None
	label = identity if isinstance(identity, str) and ID.fullmatch(identity) else index
	if not isinstance(row, dict) or row.keys() - KEYS:
		raise _invalid(label, "알 수 없는 설정 항목 또는 잘못된 부서 형식입니다")
	if label == index:
		raise _invalid(label, "id 형식이 잘못됐습니다")
	for key, limit in (("name", 40), ("scope", 300)):
		if not valid_text(row.get(key), limit):
			raise _invalid(label, f"{key}는 1~{limit}자여야 합니다")
	for key, allowed in (("repos", repos), ("allow_plans", plans)):
		values = row.get(key)
		if (not isinstance(values, list) or not values
				or any(not isinstance(value, str) or value not in allowed for value in values)):
			raise _invalid(label, f"{key}에 등록된 식별자를 1개 이상 지정해야 합니다")
	external = row.get("external_ai", True)
	if type(external) is not bool:
		raise _invalid(label, "external_ai는 참 또는 거짓이어야 합니다")
	if not external and any(plans[identity].tool != "ollama" for identity in row["allow_plans"]):
		raise _invalid(label, "외부 AI 금지 부서는 로컬 ollama 요금제만 허용합니다")
	playbook, skill_names, review = row.get("playbook"), row.get("skills", []), row.get("review_skill", "")
	if not isinstance(playbook, str) or not NAME.fullmatch(playbook):
		raise _invalid(label, "playbook 스킬 이름이 잘못됐습니다")
	if (not isinstance(skill_names, list)
			or any(not isinstance(value, str) or not NAME.fullmatch(value) for value in skill_names)):
		raise _invalid(label, "skills는 올바른 스킬 이름 목록이어야 합니다")
	if not isinstance(review, str) or review != "" and not NAME.fullmatch(review):
		raise _invalid(label, "review_skill은 스킬 이름 또는 빈 문자열이어야 합니다")
	outward = row.get("outward", [])
	if not isinstance(outward, list) or any(value not in OUTWARD for value in outward):
		raise _invalid(label, "outward는 merge·push·restart·publish의 목록이어야 합니다")
	questions = row.get("required_questions", [])
	if (not isinstance(questions, list) or len(questions) > 10
			or any(not valid_text(value, 200) for value in questions)):
		raise _invalid(label, "required_questions는 각 1~200자의 질문 0~10개여야 합니다")
	if not valid_budget(row.get("budget")):
		raise _invalid(label, "budget은 calls 1~200, minutes 1~1440 정수만 지정해야 합니다")
	return Department(identity, row["name"], row["scope"], tuple(row["repos"]), tuple(row["allow_plans"]),
		external, playbook, tuple(skill_names), review, tuple(outward), tuple(questions), dict(row["budget"]))


def load_departments() -> dict[str, Department]:
	try:
		with (home() / "departments.toml").open("rb") as stream:
			data = tomllib.load(stream)
	except FileNotFoundError:
		return {}
	except (OSError, UnicodeError, ValueError, RecursionError):
		raise _invalid(1, "설정을 읽을 수 없거나 TOML 형식이 잘못됐습니다") from None
	if data.keys() - {"department"} or not isinstance(data.get("department", []), list):
		raise _invalid(1, "최상위에는 department 목록만 허용합니다")
	rows = data.get("department", [])
	if not rows:
		return {}
	try:
		repos, plans = targets(), load_plans().plans
	except (ConfigError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
		raise _invalid(1, "저장소 또는 요금제 참조 설정이 잘못됐습니다") from None
	found = {}
	for index, row in enumerate(rows, 1):
		department = _department(row, index, repos, plans)
		if department.id in found:
			raise _invalid(department.id, "부서 id가 중복됩니다")
		found[department.id] = department
	return found
