"""설정 파일 읽기와 원천 분류.

- `~/.ai-hub/sources.toml`: 볼트 위치, 분류 규칙(cwd → 분류), 등록 저장소
- `~/.ai-hub/plans.toml`: 요금제별 허용 분류와 라우팅

형식이 틀리면 ConfigError를 올린다. 수집기는 추측으로 값을 채우지 않는다.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CLASSES = ("company", "personal", "study", "career", "exclude")
PLAN_OWNERS = ("personal", "company")
SUPPORTED_TOOLS = ("claude", "codex", "agy")


class ConfigError(Exception):
	"""설정 파일이 없거나 형식이 틀렸을 때."""


def home() -> Path:
	"""설정·원장 폴더. 테스트에서는 AI_HUB_HOME으로 바꾼다."""
	override = os.environ.get("AI_HUB_HOME")
	return Path(override) if override else Path.home() / ".ai-hub"


def run_dir() -> Path:
	"""요약 호출을 실행하는 폴더. 수집 대상(분류 exclude)으로 막아 순환을 피한다."""
	return home() / "run"


@dataclass(frozen=True)
class Rule:
	pattern: str
	cls: str
	name: str
	is_glob: bool


@dataclass(frozen=True)
class Repo:
	path: str
	cls: str
	name: str
	doc_dirs: tuple[str, ...]


@dataclass(frozen=True)
class Sources:
	vault: Path
	rules: tuple[Rule, ...]
	repos: tuple[Repo, ...]
	ignore_repos: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
	id: str
	tool: str
	plan: str
	owner: str
	allow: tuple[str, ...]
	model: str | None
	daily_limit: int | None


@dataclass(frozen=True)
class Plans:
	plans: dict[str, Plan]
	routing: dict[str, tuple[str, ...]]


def _load_toml(path: Path) -> dict:
	if not path.exists():
		raise ConfigError(f"설정 파일이 없습니다: {path}")
	try:
		with path.open("rb") as fh:
			return tomllib.load(fh)
	except tomllib.TOMLDecodeError as exc:
		raise ConfigError(f"TOML 형식 오류 {path}: {exc}") from exc


def _expand(value: str) -> str:
	return os.path.expanduser(value).rstrip("/")


def load_sources(path: Path | None = None) -> Sources:
	path = path or home() / "sources.toml"
	data = _load_toml(path)
	if "vault" not in data:
		raise ConfigError("sources.toml에 vault 경로가 없습니다")
	rules = []
	for item in data.get("classify", []):
		cls = item.get("class")
		if cls not in CLASSES:
			raise ConfigError(f"알 수 없는 분류: {cls}")
		pattern = item.get("path") or item.get("glob")
		if not pattern:
			raise ConfigError("classify 항목에 path 또는 glob이 필요합니다")
		rules.append(Rule(_expand(pattern) if "path" in item else pattern,
			cls, item.get("name", ""), "glob" in item))
	repos = []
	for item in data.get("repo", []):
		cls = item.get("class")
		if cls not in CLASSES or cls == "exclude":
			raise ConfigError(f"저장소 분류가 잘못됐습니다: {item.get('path')}")
		repos.append(Repo(_expand(item["path"]), cls, item.get("name", Path(item["path"]).name),
			tuple(item.get("doc_dirs", []))))
	ignore = tuple(_expand(p) for p in data.get("ignore", {}).get("repos", []))
	return Sources(Path(_expand(data["vault"])), tuple(rules), tuple(repos), ignore)


def load_plans(path: Path | None = None) -> Plans:
	path = path or home() / "plans.toml"
	data = _load_toml(path)
	plans: dict[str, Plan] = {}
	for item in data.get("plans", []):
		pid = item.get("id")
		if not pid or pid in plans:
			raise ConfigError(f"plans.toml의 id가 없거나 중복됩니다: {pid}")
		if item.get("tool") not in SUPPORTED_TOOLS:
			raise ConfigError(f"{pid}: 지원하지 않는 도구(C1은 claude·codex·agy만): {item.get('tool')}")
		if item.get("owner") not in PLAN_OWNERS:
			raise ConfigError(f"{pid}: owner는 personal 또는 company")
		allow = tuple(item.get("allow", []))
		if any(c not in CLASSES[:-1] for c in allow):
			raise ConfigError(f"{pid}: allow에 알 수 없는 분류가 있습니다")
		plans[pid] = Plan(pid, item["tool"], item.get("plan", ""), item["owner"], allow,
			item.get("model") or None, item.get("daily_limit_calls"))
	routing: dict[str, tuple[str, ...]] = {}
	for cls, ids in data.get("routing", {}).items():
		if cls not in CLASSES:
			raise ConfigError(f"routing에 알 수 없는 분류: {cls}")
		missing = [i for i in ids if i not in plans]
		if missing:
			raise ConfigError(f"routing이 없는 요금제를 가리킵니다: {missing}")
		routing[cls] = tuple(ids)
	return Plans(plans, routing)


def classify(cwd: str | None, rules: tuple[Rule, ...]) -> Rule | None:
	"""작업 경로에 가장 길게 맞는 규칙. 맞는 규칙이 없으면 None(볼트에 쓰지 않는다)."""
	if not cwd:
		return None
	best: Rule | None = None
	for rule in rules:
		if rule.is_glob:
			hit = fnmatch.fnmatch(cwd, rule.pattern)
		else:
			hit = cwd == rule.pattern or cwd.startswith(rule.pattern + "/")
		if hit and (best is None or len(rule.pattern) > len(best.pattern)):
			best = rule
	return best
