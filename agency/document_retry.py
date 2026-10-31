"""실패 문서의 원본과 사용량을 보존하며 명시적인 재시도를 준비한다."""

import copy
import json
import os
import stat
import tomllib
import unicodedata
from contextlib import nullcontext
from pathlib import Path

from collector.backfill_common import locked, safe
from collector.config import load_plans
from jobs import storage
from orchestrator import __main__ as orchestration

from . import documents, inputs, project
from .departments import load_departments

FAILED = ("실패", "시간 초과", "호출 한도 소진", "예산 소진", "사용자 입력 필요")


def approved(row, department):
	# 필드가 없는 이전 프로젝트만 기존 부서 승인을 상속한다. 명시된 빈 범위는 거부한다.
	values = row.get("private_plans") if "private_plans" in row else [
		value for value in department.allow_plans if value in load_plans().plans
		and load_plans().plans[value].tool in ("codex", "claude")
		and "personal" in load_plans().plans[value].allow
		and (not department.inputs or "career" in load_plans().plans[value].allow)]
	if (not isinstance(values, list) or not values or len(set(values)) != len(values)
			or any(not isinstance(value, str) or value not in department.allow_plans for value in values)):
		raise ValueError("프로젝트의 비공개 입력 승인 요금제를 확인해야 합니다")
	plans = load_plans().plans
	if any(value not in plans or plans[value].tool not in ("codex", "claude")
			or "personal" not in plans[value].allow
			or department.inputs and "career" not in plans[value].allow for value in values):
		raise ValueError("프로젝트의 비공개 입력 승인 요금제를 확인해야 합니다")
	return values


def independent(plans, calls):
	used = [call for call in calls if call.get("role") == "implement" and call.get("ok") is True]
	return [plan for plan in plans if not any(call.get("plan") == plan.id
		or call.get("tool") == plan.tool and (not call.get("model") or not plan.model or call["model"] == plan.model)
		for call in used)]


def call(plan, writer, prompt, cwd, schema, timeout, *, private=False):
	import tempfile
	from orchestrator import flow
	from . import ai, private_provider
	caller = private_provider.call if private else flow.call
	if private or plan.tool in ai.WRITE_UNSAFE_TOOLS:
		with tempfile.TemporaryDirectory(prefix="agency-document-") as empty:
			return caller(plan, writer, prompt, Path(empty), schema, timeout)
	return caller(plan, writer, prompt, cwd, schema, timeout)


def configuration(department, *, private=False, identity=None, loader=load_plans):
	if not department.external_ai:
		raise ValueError("외부 AI 금지 부서는 문서 실행 호출을 쓸 수 없습니다")
	config = orchestration.orchestration_config()
	config = {**config, **{key: [value for value in config.get(key, []) if value in department.allow_plans]
		for key in ("implementer", "reviewer")}}
	row = project.load(identity) if identity is not None else None
	if department.prepare or department.inputs or private or row is not None and "private_plans" in row:
		plans = loader().plans
		allowed = [value for value in department.allow_plans if value in plans
			and plans[value].tool in ("codex", "claude") and "personal" in plans[value].allow
			and (not department.inputs or "career" in plans[value].allow)]
		if identity is not None:
			allowed = [value for value in allowed if value in approved(row, department)]
		config["implementer"] = [value for value in config["implementer"] if value in allowed]
		config["reviewer"] = list(dict.fromkeys([value for value in config["reviewer"] if value in allowed] + allowed))
	if not config["implementer"] or not config["reviewer"] and identity is None:
		raise ValueError("부서가 허용한 도구 제한 구현·독립 검토 요금제가 필요합니다")
	if (identity is None or row is not None and "private_plans" not in row) and (department.prepare or department.inputs or private):
		from orchestrator import flow
		loaded = loader()
		writers = flow.role_plans(loaded, config, "implement")
		reviewers = flow.role_plans(loaded, config, "review")
		if not any(independent(reviewers, [{"role": "implement", "ok": True,
				"plan": writer.id, "tool": writer.tool, "model": writer.model or ""}]) for writer in writers):
			raise ValueError("부서가 허용한 독립 검토 요금제가 필요합니다")
	return config


def cap_budget(row, execution, *, new=False):
	# 일반 문서의 이전 정산은 실행 인계에 있다. 원장을 바꾸지 않고 예산 계산에만 사용한다.
	if not new and execution.run_id not in row.get("execution_usage", {}):
		previous = execution.handoff.get("agency_usage", {"calls": 0, "seconds": 0})
		if not isinstance(previous, dict) or set(previous) != {"calls", "seconds"}:
			raise ValueError("이전 문서 사용량을 확인해야 합니다")
		project._usage_values(previous["calls"], previous["seconds"])
		if any(previous[key] > row["used"][key] for key in previous):
			raise ValueError("이전 문서 사용량이 프로젝트 사용량과 다릅니다")
		row = copy.deepcopy(row)
		row.setdefault("execution_usage", {})[execution.run_id] = previous
	project.cap_run_budget(row, execution, new=new)


def read_details(filename):
	"""기존 안전 읽기로 제한된 파일만 읽고 오류에는 내용을 넣지 않는다."""
	try:
		path = Path(filename)
		content = inputs._read(path, 16000)
		if path.stat().st_uid != os.getuid():
			raise ValueError
		return content.decode("utf-8")
	except (OSError, ValueError, TypeError):
		raise ValueError("작성 지침 파일의 소유·권한·형식·크기를 확인해야 합니다") from None


def revised_request(raw, details):
	# 일반 지침만 받는다. 입력 내용과 과거 막힘은 읽거나 개정에 복사하지 않는다.
	if (not isinstance(details, str) or not 1 <= len(details.strip()) <= 4000
			or any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in details)):
		raise ValueError("개인 자료를 제외한 새로운 일반 작성 지침이 필요합니다")
	from orchestrator import draft
	from vaultfilter.public import Rules
	try:
		draft.safe_text(details, Rules())
	except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
		raise ValueError("작성 지침의 민감값 검사를 통과하지 못했습니다") from None
	data = tomllib.loads(raw.decode("utf-8"))
	if details.strip() == str(data["request"].get("details", "")).strip():
		raise ValueError("이전과 다른 일반 작성 지침이 필요합니다")
	previous = str(data["request"].get("details", ""))
	data["request"]["details"] = previous + ("\n\n" if previous else "") + "추가 일반 작성 지침:\n" + details.strip()
	return draft.serialize(data).encode("utf-8")


def validate_run_id(run_id):
	import re
	from datetime import date
	if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id):
		raise ValueError("문서 실행 번호 형식이 잘못됐습니다")
	try:
		date.fromisoformat(run_id[:10])
		if run_id.endswith("-000"):
			raise ValueError
	except ValueError:
		raise ValueError("문서 실행 번호 형식이 잘못됐습니다") from None


def fingerprint(execution):
	folder = execution.folder
	if not safe(folder):
		raise ValueError("실행 기록 경로를 확인해야 합니다")
	info = folder.stat()
	if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
			or stat.S_IMODE(info.st_mode) != 0o700):
		raise ValueError("실행 기록 폴더의 소유 또는 권한이 잘못됐습니다")
	path = folder / "run.json"
	if not safe(path):
		raise ValueError("실행 기록에 링크를 사용할 수 없습니다")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "rb") as stream:
		info = os.fstat(stream.fileno())
		if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
				or stat.S_IMODE(info.st_mode) & 0o022):
			raise ValueError("실행 기록의 소유·종류·권한이 잘못됐습니다")
		content = stream.read(inputs.MAX_OUTPUT + 1)
		if len(content) > inputs.MAX_OUTPUT:
			raise ValueError("실행 기록이 크기 한도를 넘었습니다")
	return {"run.json": inputs._hash(content),
		"request.toml": inputs._hash(inputs._read(folder / "request.toml", inputs.MAX_OUTPUT))}


def private_blockers(execution, blockers):
	content = json.dumps({"blockers": blockers}, ensure_ascii=False, allow_nan=False).encode("utf-8")
	folder = execution.folder / "private-blockers"
	if not safe(folder):
		raise ValueError("비공개 막힘 저장 경로를 확인해야 합니다")
	folder.mkdir(mode=0o700, exist_ok=True)
	info = folder.stat()
	if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
		raise ValueError("비공개 막힘 저장 권한을 확인해야 합니다")
	inputs._save(folder / (inputs._hash(content) + ".json"), content, inputs.MAX_OUTPUT)


def validate_history(task):
	from . import document_run
	history = task.get("document_retries", [])
	if not isinstance(history, list):
		raise ValueError("문서 재시도 연결 기록이 잘못됐습니다")
	for entry in history:
		if not isinstance(entry, dict):
			raise ValueError("문서 재시도 연결 기록이 잘못됐습니다")
		previous = document_run.load(entry["run_id"])
		if previous.status not in FAILED or fingerprint(previous) != entry["files"]:
			raise ValueError("이전 실패 실행 기록이 바뀌었습니다")
		path = Path(entry["request_path"])
		if inputs._hash(inputs._read(path, inputs.MAX_OUTPUT)) != entry["request_sha256"]:
			raise ValueError("이전 요청 개정이 바뀌었습니다")


def prepare(identity, task_id, plans, *, details=None):
	from . import document_run
	workdir = project._folder(identity) / "work"
	with locked("agency-repo:" + str(workdir)):
		row = project.load(identity)
		department = load_departments().get(row["department"])
		if (department is None or department.deliverable != "document" or not department.external_ai
				or row["status"] != "막힘" or row.get("blocked_from") != "실행 중"
				or row.get("schedule_repo") or any(item["status"] == "실행 중" for item in row["tasks"])
				or any((project._folder(identity) / name).exists() for name in ("delivery.json", "report.md"))):
			raise ValueError("실패한 일반 문서 프로젝트만 재시도할 수 있습니다")
		project._check_tasks(row["tasks"])
		tasks = [item for item in row["tasks"] if item["id"] == task_id]
		if len(tasks) != 1 or tasks[0]["status"] != "실패" or not tasks[0]["run_id"] or tasks[0]["commit"]:
			raise ValueError("실패한 문서 작업을 지정해야 합니다")
		task = tasks[0]
		candidate = {**row, "private_plans": list(dict.fromkeys(plans))}
		approved(candidate, department)
		execution = document_run.load(task["run_id"])
		with orchestration.locked(execution):
			execution = document_run.load(task["run_id"])
			document_run.check(identity, task, execution, workdir)
			validate_history(task)
			if execution.status not in FAILED or execution.approval_pending:
				raise ValueError("실패한 문서 실행만 재시도할 수 있습니다")
			with storage.transaction() if department.inputs else nullcontext(None) as root:
				inputs.verify(identity, department, execution.request, root)
				usage = documents._periodic_usage(execution)
				previous = row.get("execution_usage", {}).get(execution.run_id,
					execution.handoff.get("agency_usage", {"calls": 0, "seconds": 0}))
				if not isinstance(previous, dict) or set(previous) != {"calls", "seconds"}:
					raise ValueError("이전 사용량 기록을 확인해야 합니다")
				project._usage_values(previous["calls"], previous["seconds"])
				if any(previous[key] > row["used"][key] for key in previous):
					raise ValueError("이전 사용량이 프로젝트 사용량과 다릅니다")
				for key in ("calls", "seconds"):
					if previous[key] > usage[key]:
						raise ValueError("이전 사용량 기록이 줄었습니다")
				raw = inputs._read(Path(task["request_path"]), inputs.MAX_OUTPUT)
				digest = inputs._hash(raw)
				if task.get("request_sha256", digest) != digest:
					raise ValueError("실패 요청서 해시가 다릅니다")
				revision = len(task.get("document_retries", [])) + 1
				path = project._folder(identity) / f"{task_id}-retry-{revision}.toml"
				content = revised_request(raw, details)
				inputs._save(path, content, inputs.MAX_OUTPUT)
				entry = {"run_id": execution.run_id, "request_path": task["request_path"],
					"request_sha256": digest, "files": fingerprint(execution), "usage": usage,
					"revision": revision}
				with project._edit(identity, "document_retry") as current:
					if current != row or fingerprint(execution) != entry["files"]:
						raise ValueError("재시도 준비 중 기록이 바뀌었습니다")
					for key in ("calls", "seconds"):
						current["used"][key] += usage[key] - previous[key]
					current.setdefault("execution_usage", {})[execution.run_id] = dict(usage)
					item = next(item for item in current["tasks"] if item["id"] == task_id)
					item.setdefault("document_retries", []).append(copy.deepcopy(entry))
					item.update(status="대기", run_id="", request_path=str(path), request_sha256=inputs._hash(content))
					current["private_plans"] = candidate["private_plans"]
		return {"status": "막힘", "task": task_id, "revision": revision}
