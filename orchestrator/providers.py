"""외부 AI CLI 어댑터. 요금제(plans.toml)에 있는 도구만 부르고, 결과는 JSON 한 덩어리로 받는다.

구현 담당만 작업 폴더에 쓸 수 있다. 검토 담당은 읽기 전용으로 부른다.
세션은 저장하지 않는다(수집기가 실행기 호출을 사람의 대화로 기록하지 않게). 인계는 실행 기록이 맡는다.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from collector.config import Plan

READ_ONLY_TOOLS = "Read,Grep,Glob"
WRITE_BLOCKED_TOOLS = "Edit,Write,MultiEdit,NotebookEdit,Bash,WebFetch,WebSearch"


class ProviderError(Exception):
	"""호출 실패. kind 는 timeout·exit·empty·json·missing 중 하나. 메시지에 원문을 넣지 않는다."""

	def __init__(self, kind: str, message: str):
		super().__init__(message)
		self.kind = kind


@dataclass
class Reply:
	data: dict
	seconds: float
	usage: dict | str


def _codex(plan: Plan, writer: bool, cwd: Path, schema_file: Path, last_file: Path) -> tuple[list[str], bool]:
	argv = ["codex", "exec", "-C", str(cwd), "-s", "workspace-write" if writer else "read-only",
		"--ephemeral", "--json", "--output-schema", str(schema_file), "-o", str(last_file)]
	if plan.model:
		argv += ["-m", plan.model]
	# Codex 는 git 저장소 밖 폴더에서 신뢰 확인으로 바로 거부한다(회의 기록 폴더 등). 샌드박스는 그대로 두고 확인만 건너뛴다.
	if subprocess.run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode != 0:
		argv.insert(argv.index("--ephemeral"), "--skip-git-repo-check")
	return argv + ["-"], True


def _claude(plan: Plan, writer: bool, schema: dict) -> tuple[list[str], bool]:
	argv = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--no-session-persistence"]
	if writer:
		argv += ["--permission-mode", "acceptEdits"]
	else:
		argv += ["--allowedTools", READ_ONLY_TOOLS, "--disallowedTools", WRITE_BLOCKED_TOOLS]
	if plan.model:
		argv += ["--model", plan.model]
	return argv, True


def _agy(plan: Plan, writer: bool, schema: dict, prompt: str) -> tuple[list[str], bool]:
	argv = ["agy", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
		"--mode", "accept-edits" if writer else "plan"]
	if plan.model:
		argv += ["--model", plan.model]
	return argv, False


def extract_json(text: str) -> dict:
	"""응답에서 JSON 객체 하나를 꺼낸다. 코드 울타리(```)로 감싸도 받는다."""
	text = text.strip()
	fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
	if fenced:
		text = fenced.group(1)
	try:
		value = json.loads(text)
	except ValueError:
		start, end = text.find("{"), text.rfind("}")
		if start < 0 or end <= start:
			raise ProviderError("json", "응답에 JSON이 없습니다") from None
		try:
			value = json.loads(text[start:end + 1])
		except ValueError:
			raise ProviderError("json", "응답 JSON을 읽지 못했습니다") from None
	if not isinstance(value, dict):
		raise ProviderError("json", "응답이 JSON 객체가 아닙니다")
	return value


def _usage_in(line: str) -> dict | None:
	"""codex --json 사건 한 줄에서 토큰 사용량을 찾는다. 형식이 바뀌면 None(미측정)."""
	try:
		event = json.loads(line)
	except ValueError:
		return None
	if not isinstance(event, dict):
		return None
	for holder in (event, event.get("msg"), event.get("payload")):
		if isinstance(holder, dict):
			for key in ("usage", "info"):
				if isinstance(holder.get(key), dict):
					return holder[key]
	return None


def _unwrap(tool: str, stdout: str, last_file: Path) -> tuple[dict, dict | str]:
	"""(구조화 결과, 사용량). 도구마다 결과를 담는 자리가 다르다."""
	if tool == "codex":
		usage: dict | str = "미측정"
		for line in stdout.splitlines():
			found = _usage_in(line)
			if found is not None:
				usage = found  # 마지막 값이 누계다
		text = last_file.read_text(encoding="utf-8") if last_file.exists() else ""
		if not text.strip():
			raise ProviderError("empty", "codex 빈 응답")
		return extract_json(text), usage
	outer = extract_json(stdout)
	usage = outer.get("usage") or "미측정"
	if isinstance(outer.get("structured_output"), dict):
		return outer["structured_output"], usage
	result = outer.get("result") if "result" in outer else outer.get("response")
	if isinstance(result, dict):
		return result, usage
	if isinstance(result, str) and result.strip():
		return extract_json(result), usage
	if outer.get("is_error"):
		raise ProviderError("exit", f"{tool} 오류 응답")
	return outer, usage


def call(plan: Plan, writer: bool, prompt: str, cwd: Path, schema: dict, timeout: float) -> Reply:
	with tempfile.TemporaryDirectory(prefix="orch-") as tmp:
		schema_file, last_file = Path(tmp) / "schema.json", Path(tmp) / "last.json"
		schema_file.write_text(json.dumps(schema), encoding="utf-8")
		if plan.tool == "codex":
			argv, use_stdin = _codex(plan, writer, cwd, schema_file, last_file)
		elif plan.tool == "claude":
			argv, use_stdin = _claude(plan, writer, schema)
		elif plan.tool == "agy":
			argv, use_stdin = _agy(plan, writer, schema, prompt)
		else:
			raise ProviderError("missing", f"지원하지 않는 도구: {plan.tool}")
		started = time.monotonic()
		try:
			result = subprocess.run(argv, input=prompt if use_stdin else None, cwd=cwd,
				capture_output=True, text=True, timeout=max(30.0, timeout))
		except subprocess.TimeoutExpired:
			raise ProviderError("timeout", f"{plan.id} 시간 초과") from None
		except OSError:
			raise ProviderError("missing", f"{plan.id} 실행 파일 없음") from None
		seconds = time.monotonic() - started
		if result.returncode != 0:
			raise ProviderError("exit", f"{plan.id} 종료 코드 {result.returncode}")
		data, usage = _unwrap(plan.tool, result.stdout, last_file)
		return Reply(data, seconds, usage)
