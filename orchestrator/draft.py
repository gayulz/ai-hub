"""읽기 전용 기획 호출의 JSON을 검사하고 고정된 저장소의 요청서로 만든다."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path, PureWindowsPath

from collector.config import ConfigError, home, load_plans
from collector.redact_pii import EMAIL
from vaultfilter.public import Rules
from vaultfilter.verify import AI_SIGNATURE

from . import prompts, providers
from .checks import DEFAULT_TEST
from .flow import role_plans

REPO = Path(__file__).resolve().parent.parent
ROLES = ("plan", "backend", "frontend", "design")
DEFAULT_FORBIDDEN = [
	"브라우저를 열지 않는다(화면·headless 모두 금지).",
	"실제 ~/.ai-hub·볼트·launchd를 읽거나 쓰지 않는다. 테스트는 임시 폴더의 합성 자료만 쓴다.",
	"회사 이름·실제 경로·비밀값·이메일을 코드·테스트·출력에 넣지 않는다.",
	"새 의존성을 넣지 않는다.",
	"owned_paths 밖의 파일을 수정하지 않는다.",
	"git push를 하지 않는다. 반영(apply)은 사용자가 따로 승인한다.",
]
FIELDS = ("goal", "done", "owned_paths", "forbidden", "details", "commit_message")
LIMITS = {"calls": (2, 8), "minutes": (10, 120), "fixes": (0, 2)}
# 사무실은 표준 오류의 마지막 줄이 이 머리로 시작할 때만 이유를 화면에 보인다
FAILURE = "초안 작성 실패: "


def string(value, field: str, maximum: int, minimum: int = 1) -> str:
	if (not isinstance(value, str) or not minimum <= len(value) <= maximum
			or (minimum and not value.strip()) or "\x00" in value):
		raise ValueError(f"{field}: 문자열 길이·형식을 확인하세요(최대 {maximum}자)")
	value.encode("utf-8")
	return value


def safe_text(value: str, rules: Rules) -> None:
	if rules.kinds(value) or EMAIL.search(value):
		raise ValueError("회사·서비스 이름, 비밀값, 이메일 또는 홈 경로가 포함돼 거부했습니다")


def user_text(value: str, rules: Rules) -> str:
	value = string(value, "사용자 글", 2000)
	safe_text(value, rules)
	return value


def strings(value, field: str, count: int, minimum: int = 1) -> list[str]:
	if not isinstance(value, list) or not minimum <= len(value) <= count:
		raise ValueError(f"{field}: 항목 수는 {minimum}~{count}개여야 합니다")
	return [string(item, field, 500) for item in value]


def owned_path(value: str) -> None:
	parts = value.split("/")
	if (Path(value).is_absolute() or PureWindowsPath(value).drive or "\\" in value or "~" in value
			or ".." in value or ".git" in parts or any(part in ("", ".") for part in parts)
			or all(part in ("*", "**") for part in parts) or any(ord(c) < 32 or ord(c) == 127 for c in value)
			or any(part.startswith("🔐") for part in parts)):
		raise ValueError("owned_paths: 저장소 안의 구체적인 상대 경로·글롭만 허용합니다")


def validate(data, role: str, rules: Rules) -> dict:
	if role not in ROLES or not isinstance(data, dict):
		raise ValueError("역할 또는 초안 JSON 형식을 확인하세요")
	request = {"repo": str(REPO), "kind": "code", "role": role,
		"goal": string(data.get("goal"), "goal", 300),
		"done": strings(data.get("done"), "done", 10),
		"owned_paths": strings(data.get("owned_paths"), "owned_paths", 20),
		"forbidden": strings(data.get("forbidden"), "forbidden", 40, 0),
		"details": string(data.get("details"), "details", 10000, 0),
		"test_command": DEFAULT_TEST,
		"commit_message": string(data.get("commit_message"), "commit_message", 200)}
	for path in request["owned_paths"]:
		owned_path(path)
	if AI_SIGNATURE.search(request["commit_message"]):
		raise ValueError("commit_message: AI 서명은 허용하지 않습니다")
	for field in FIELDS:
		value = request[field]
		safe_text("\n".join(value) if isinstance(value, list) else value, rules)
	budget = data.get("budget")
	if not isinstance(budget, dict) or any(type(budget.get(key)) is not int for key in LIMITS):
		raise ValueError("budget: calls·minutes·fixes는 정수여야 합니다")
	budget = {key: max(low, min(budget[key], high)) for key, (low, high) in LIMITS.items()}
	request["forbidden"] += DEFAULT_FORBIDDEN
	return {"request": request, "budget": budget}


def read_draft(path: Path, rules: Rules | None = None) -> dict:
	from .__main__ import read_request
	data = read_request(str(path))
	request, budget = data["request"], data.get("budget")
	if (request.get("repo") != str(REPO) or request.get("kind") != "code"
			or request.get("test_command") != DEFAULT_TEST):
		raise ValueError("초안의 저장소·종류·테스트 명령이 고정값과 다릅니다")
	forbidden = request.get("forbidden")
	if not isinstance(forbidden, list) or forbidden[-len(DEFAULT_FORBIDDEN):] != DEFAULT_FORBIDDEN:
		raise ValueError("초안에 기본 금지 사항이 필요합니다")
	checked = validate({**request, "forbidden": forbidden[:-len(DEFAULT_FORBIDDEN)], "budget": budget},
		request.get("role"), rules or Rules())
	if data != checked:
		raise ValueError("초안 필드 또는 예산이 허용된 형식·범위와 다릅니다")
	return checked


def quote(value: str) -> str:
	# JSON과 TOML의 공통 문자열 이스케이프만 쓴다(DEL은 TOML에서 별도로 이스케이프).
	return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def serialize(data: dict) -> str:
	lines = ["[request]"]
	for key, value in data["request"].items():
		encoded = "[" + ", ".join(quote(item) for item in value) + "]" if isinstance(value, list) else quote(value)
		lines.append(f"{key} = {encoded}")
	lines += ["", "[budget]"] + [f"{key} = {value}" for key, value in data["budget"].items()]
	return "\n".join(lines) + "\n"


def summary(data: dict, plan: str) -> dict:
	request = data["request"]
	return {key: request[key] for key in ("goal", "done", "owned_paths", "forbidden", "role")} | {
		"budget": data["budget"], "plan": plan}


def create(role: str, text_file: Path, out: Path, previous: Path | None = None) -> dict:
	rules = Rules()
	text = user_text(text_file.read_text(encoding="utf-8"), rules)
	prior = read_draft(previous, rules) if previous else None
	if prior and prior["request"]["role"] != role:
		raise ValueError("이전 초안과 요청 역할이 다릅니다")
	with (home() / "plans.toml").open("rb") as stream:
		config = tomllib.load(stream).get("orchestration", {})
	plans = role_plans(load_plans(), config, "draft")
	if not plans:
		raise ValueError("기획 역할에 personal 허용 요금제가 없습니다")
	previous_fields = {key: prior["request"][key] for key in FIELDS} | {"budget": prior["budget"]} if prior else None
	reply = providers.call(plans[0], writer=False, prompt=prompts.draft(role, text, previous_fields),
		cwd=REPO, schema=prompts.DRAFT_SCHEMA, timeout=570)
	data = validate(reply.data, role, rules)
	content = serialize(data)
	if tomllib.loads(content) != data:
		raise ValueError("TOML 직렬화 결과가 초안과 다릅니다")
	if any(path.is_symlink() for path in (out, *out.parents)):
		raise ValueError("초안 출력 경로는 링크일 수 없습니다")
	fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
		read_draft(out, rules)
	except Exception:
		out.unlink(missing_ok=True)
		raise
	return summary(data, plans[0].id)


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--role", required=True, choices=ROLES)
	parser.add_argument("--text-file", required=True, type=Path)
	parser.add_argument("--out", required=True, type=Path)
	parser.add_argument("--previous", type=Path)
	args = parser.parse_args(argv)
	try:
		value = create(args.role, args.text_file, args.out, args.previous)
		print(json.dumps(value, ensure_ascii=False))
		return 0
	except (OSError, ValueError, TypeError, KeyError, ConfigError, providers.ProviderError, RecursionError) as exc:
		if isinstance(exc, providers.ProviderError):
			message = f"AI 호출 실패({exc.kind}). 요금제 한도·로그인·네트워크를 확인하세요"
		else:
			message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else type(exc).__name__
		print(FAILURE + message, file=sys.stderr)
		return 2
