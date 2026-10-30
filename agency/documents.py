"""프로젝트 안의 납품 문서만 저장하고 읽는다."""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import tomllib
from pathlib import Path

from collector.backfill_common import safe
from orchestrator import isolated_inputs

from . import inputs, periodic_results, project

PERIODIC_REVIEW_GUIDANCE = ("\n보고서가 실행기 근거와 미검증 항목을 정확히 구분하는지 검토한다. "
	"미검증 항목의 존재 자체는 정직한 보고서의 실패가 아니며 프로젝트 완료 여부는 별도 검수한다.")
def path(identity: str, task_id: str):
	if not isinstance(task_id, str) or not re.fullmatch(r"t[1-9][0-9]*", task_id):
		raise ValueError("납품 작업 id 형식이 잘못됐습니다")
	from . import periodic_recovery
	result = periodic_recovery.output_base(identity) / "deliverables" / (task_id + ".md")
	if not safe(result):
		raise ValueError("납품 문서에 심볼릭 링크를 사용할 수 없습니다")
	return result

def read(identity: str, task_id: str, *, unique: bool = False) -> bytes:
	file = path(identity, task_id)
	try:
		fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
		with os.fdopen(fd, "rb") as stream:
			info = os.fstat(stream.fileno())
			if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
					or info.st_uid != os.getuid() or unique and info.st_nlink != 1):
				raise ValueError("납품 문서는 권한 0600의 일반 파일이어야 합니다")
			return stream.read()
	except OSError:
		raise ValueError("납품 문서를 읽을 수 없습니다") from None

def save(identity: str, task_id: str, text: str) -> str:
	if not isinstance(text, str) or not text.strip():
		raise ValueError("통과한 납품 문서가 비어 있습니다")
	content = text.encode("utf-8")
	file = path(identity, task_id)
	file.parent.mkdir(mode=0o700, exist_ok=True)
	if not safe(file) or not file.parent.is_dir():
		raise ValueError("납품 문서 폴더가 잘못됐습니다")
	file.parent.chmod(0o700)
	fd, temporary = tempfile.mkstemp(prefix=".document-", dir=file.parent)
	try:
		with os.fdopen(fd, "wb") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		try:
			os.link(temporary, file, follow_symlinks=False)
		except FileExistsError:
			if read(identity, task_id) != content:
				raise ValueError("기존 납품 문서가 실행 결과와 다릅니다") from None
		if read(identity, task_id) != content:
			raise ValueError("납품 문서를 다시 읽은 결과가 다릅니다")
	finally:
		os.unlink(temporary)
	return str(file)

def manifest(row: dict) -> list[dict]:
	if (row.get("periodic_recovery") and row.get("status") == "검수"
			and any((project._folder(row["id"]) / name).exists() for name in ("report.md", "delivery.json"))):
		raise ValueError("기존 최종 검수 또는 납품 기록을 덮어쓸 수 없습니다")
	items = []
	for task in row["tasks"]:
		content = read(row["id"], task["id"])
		content.decode("utf-8")
		item = {"task": task["id"], "path": str(path(row["id"], task["id"])),
			"sha256": hashlib.sha256(content).hexdigest()}
		if row.get("audit_report_mode") == "explicit-v1":
			from . import document_run
			if document_run.verified_report(row["id"], task).encode("utf-8") != content:
				raise ValueError("점검 보고서가 검토 결과와 다릅니다")
			base = row.get("audit_base")
			if not isinstance(base, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base):
				raise ValueError("점검 기준 커밋이 없습니다")
			item.update(request_sha256=task["request_sha256"], evidence_sha256=task["report_evidence"]["sha256"],
				report_seal_sha256=task["report_seal"]["sha256"], base=base)
		items.append(item)
	return items

def periodic_manifest(row: dict) -> list[dict]:
	from . import document_run, runner
	if row.get("audit_report_mode") != "explicit-v1" or not row.get("schedule_repo"):
		raise ValueError("명시된 정기 점검이 아닙니다")
	items = []
	for task in row["tasks"]:
		request_path = Path(task["request_path"])
		if (not request_path.is_absolute() or not safe(request_path)
				or not request_path.resolve().is_relative_to(project._folder(row["id"]).resolve())):
			raise ValueError("점검 요청서의 소유 경계가 잘못됐습니다")
		raw = inputs._read(request_path, inputs.MAX_OUTPUT)
		if inputs._hash(raw) != task.get("request_sha256"):
			raise ValueError("점검 요청서의 해시가 다릅니다")
		try:
			request = tomllib.loads(raw.decode("utf-8"))["request"]
		except (KeyError, UnicodeError, tomllib.TOMLDecodeError):
			raise ValueError("점검 요청서 형식이 잘못됐습니다") from None
		if not isinstance(request, dict):
			raise ValueError("점검 요청서 형식이 잘못됐습니다")
		if request.get("kind", "code") == "code":
			runner._verify_report_dependencies(row, task, request)
			continue
		if request.get("report_mode") != "periodic-v1":
			raise ValueError("명시된 점검 보고 요청서가 아닙니다")
		content = read(row["id"], task["id"], unique=True)
		if document_run.verified_report(row["id"], task).encode("utf-8") != content:
			raise ValueError("점검 보고서가 독립 검토 결과와 다릅니다")
		items.append({"task": task["id"], "sha256": inputs._hash(content),
			"request_sha256": task["request_sha256"], "evidence_sha256": task["report_evidence"]["sha256"],
			"report_seal_sha256": task["report_seal"]["sha256"]})
	if not items:
		raise ValueError("검증된 점검 보고서가 없습니다")
	return items

def periodic_changed_paths(row: dict, repo: Path, tip: str) -> list[str]:
	from orchestrator.checks import git
	limit = project.periodic_file_limit(row)
	base = row.get("audit_base")
	if any(not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
			for value in (base, tip)):
		raise ValueError("점검 기준 커밋이 없습니다")
	if git(repo, "merge-base", base, tip).strip() != base:
		raise ValueError("점검 기준 커밋이 프로젝트 가지의 조상이 아닙니다")
	paths = [value for value in git(repo, "diff", "--name-only", "-z", "--no-renames", base, tip).split("\0") if value]
	if limit is not None and len(paths) > limit:
		raise ValueError(f"프로젝트 전체 변경 파일 {len(paths)}개가 명세의 {limit}개 제한을 넘었습니다")
	return paths

def unchanged_repo(row: dict, department):
	from orchestrator import targets
	from orchestrator.checks import git
	from . import schedule
	if (row.get("audit_report_mode") != "explicit-v1" or not row.get("schedule_repo")
			or not isinstance(row.get("branch"), str) or not isinstance(row.get("audit_base"), str)
			or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", row["audit_base"])):
		raise ValueError("점검 기준 가지 또는 커밋이 없습니다")
	selected = targets.target(schedule.target(row, department))
	git(selected.path, "check-ref-format", "--branch", row["branch"])
	tip = git(selected.path, "rev-parse", "--verify", "refs/heads/" + row["branch"], check=False).strip()
	if tip != row["audit_base"]:
		raise ValueError("보고 전용 프로젝트 가지가 바뀌었습니다")
	return selected

def evidence_path(identity: str, task_id: str) -> Path:
	if not isinstance(task_id, str) or not re.fullmatch(r"t[1-9][0-9]*", task_id):
		raise ValueError("점검 작업 id 형식이 잘못됐습니다")
	from . import periodic_recovery
	result = periodic_recovery.output_base(identity) / "evidence" / (task_id + ".json")
	if not safe(result):
		raise ValueError("점검 근거에 심볼릭 링크를 사용할 수 없습니다")
	return result

def _unverified_evidence(identity: str, task: dict) -> bytes:
	try:
		folder = project._folder(identity)
		path = Path(task["request_path"])
		if not path.is_absolute() or not safe(path) or not path.resolve().is_relative_to(folder.resolve()):
			raise ValueError
		raw = inputs._read(path, inputs.MAX_OUTPUT)
		if task.get("request_sha256") != inputs._hash(raw):
			raise ValueError
		request = tomllib.loads(raw.decode("utf-8"))["request"]
		checks = request["report_checks"]
		if (request.get("kind") != "analysis" or request.get("report_mode") != "periodic-v1"
				or request.get("report_project") != identity or request.get("report_task") != task["id"]
				or request.get("workdir") != str(folder / "work") or "inputs" in request or not isinstance(checks, list)
				or not 1 <= len(checks) <= 2 or any(item not in ("tests", "dependencies") for item in checks)
				or len(set(checks)) != len(checks)):
			raise ValueError
		value = {"version": 1, "project": identity, "task": task["id"], "request_sha256": inputs._hash(raw),
			"checks": [{"kind": item, "status": "unverified", "reason": "source_unavailable"} for item in checks]}
		from . import periodic_recovery
		if facts := periodic_recovery.facts_for_task(identity, task, request):
			value["facts"] = facts
		dependencies = request.get("report_dependencies", [])
		if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
			raise ValueError
		if dependencies:
			from . import document_run, runner
			row = project.load(identity)
			runner._verify_report_dependencies(row, task, request)
			value["dependencies"] = []
			for item in dependencies:
				previous = next(previous for previous in row["tasks"] if previous["id"] == item)
				content = document_run.verified_report(identity, previous)
				value["dependencies"].append({"task": item, "seal": previous["report_seal"]["sha256"], "document": content})
		return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
	except (KeyError, TypeError, StopIteration, UnicodeError, tomllib.TOMLDecodeError, OSError):
		raise ValueError("점검 요청서 또는 근거 형식이 잘못됐습니다") from None

def _read_private_file(file: Path, staging_prefix: str) -> bytes:
	if not safe(file):
		raise ValueError("점검 근거에 심볼릭 링크를 사용할 수 없습니다")
	try:
		fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
		with os.fdopen(fd, "rb") as stream:
			info = os.fstat(stream.fileno())
			if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
					or info.st_uid != os.getuid() or info.st_nlink not in (1, 2)):
				raise ValueError("점검 근거 파일의 소유 또는 권한이 잘못됐습니다")
			if info.st_nlink == 2:
				matches = []
				for candidate in file.parent.glob(staging_prefix + "*"):
					other = candidate.lstat()
					if (stat.S_ISREG(other.st_mode) and other.st_dev == info.st_dev and other.st_ino == info.st_ino
							and other.st_uid == info.st_uid and stat.S_IMODE(other.st_mode) == 0o600):
						matches.append(candidate)
				if len(matches) != 1:
					raise ValueError("점검 근거의 보존 링크를 확인할 수 없습니다")
			content = stream.read(inputs.MAX_OUTPUT + 1)
			if len(content) > inputs.MAX_OUTPUT:
				raise ValueError("점검 근거가 크기 한도를 넘었습니다")
			return content
	except OSError:
		raise ValueError("점검 근거를 읽을 수 없습니다") from None

def _read_evidence_file(file: Path) -> bytes:
	return _read_private_file(file, ".evidence-" + file.stem + "-")

def _review_call(execution):
	calls = [call for call in execution.calls if call.get("role") == "review" and call.get("ok") is True]
	if not calls:
		raise ValueError("성공한 독립 검토 호출이 없습니다")
	return calls[-1]

def review_prompt(execution, prompt: str) -> str:
	from orchestrator import prompts
	result = prompt + PERIODIC_REVIEW_GUIDANCE
	clipped = prompts._clip(execution.document)
	return result.replace(clipped, execution.document, 1) if clipped != execution.document else result

def pin_review(execution, prompt: str, data: dict) -> None:
	if not isinstance(prompt, str) or not isinstance(data, dict):
		raise ValueError("독립 검토 입력 또는 결과가 잘못됐습니다")
	content = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False).encode("utf-8")
	if len(content) > inputs.MAX_OUTPUT:
		raise ValueError("독립 검토 결과가 한도를 넘었습니다")
	call = _review_call(execution)
	from .document_review import cache_periodic_review
	cache_periodic_review(execution, content)
	call["periodic_review_input_sha256"] = inputs._hash(prompt.encode("utf-8"))
	call["periodic_review_output_sha256"] = inputs._hash(content)
	execution.save()

def read_review(execution) -> bytes:
	file = execution.folder / f"review-{execution.fixes}.json"
	content = _read_private_file(file, ".review-" + str(execution.fixes) + "-")
	call = _review_call(execution)
	if call.get("periodic_review_output_sha256") != inputs._hash(content):
		raise ValueError("독립 검토 결과의 기록 해시가 다릅니다")
	return content

def recover_review(execution, prompt: str):
	if not any(call.get("role") == "review" and call.get("ok") is True for call in execution.calls):
		return None
	call = _review_call(execution)
	if call.get("periodic_review_input_sha256") != inputs._hash(prompt.encode("utf-8")):
		raise ValueError("이전 독립 검토의 입력 또는 중단 상태를 확인해야 합니다")
	try:
		if not (execution.folder / f"review-{execution.fixes}.json").exists():
			from .document_review import restore_periodic_review
			restore_periodic_review(execution, call.get("periodic_review_output_sha256"))
		result = json.loads(read_review(execution))
	except (ValueError, UnicodeError):
		raise ValueError("이전 독립 검토의 결과를 확인해야 합니다") from None
	if not isinstance(result, dict):
		raise ValueError("이전 독립 검토의 결과가 잘못됐습니다")
	return result

def save_review(execution, data: dict) -> None:
	content = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False).encode("utf-8")
	if _review_call(execution).get("periodic_review_output_sha256") != inputs._hash(content):
		raise ValueError("독립 검토 결과가 호출 기록과 다릅니다")
	file = execution.folder / f"review-{execution.fixes}.json"
	if not safe(file):
		raise ValueError("독립 검토 파일에 링크를 사용할 수 없습니다")
	if not file.exists():
		fd, temporary = tempfile.mkstemp(prefix=".review-" + str(execution.fixes) + "-", dir=file.parent)
		with os.fdopen(fd, "wb") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		try:
			os.link(temporary, file, follow_symlinks=False)
		except FileExistsError:
			pass
	if read_review(execution) != content:
		raise ValueError("기존 독립 검토 기록이 다릅니다")

def link_periodic_run(identity: str, task_id: str, run_id: str) -> dict:
	from orchestrator.record import Run, runs_dir
	if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id):
		raise ValueError("점검 실행 번호가 잘못됐습니다")
	if not safe(runs_dir() / run_id) or not safe(runs_dir() / run_id / "run.json"):
		raise ValueError("점검 실행 기록에 링크를 사용할 수 없습니다")
	execution = Run.load(run_id)
	if (execution.run_id != run_id or execution.kind != "analysis" or execution.status != "작업 중"
			or execution.calls or execution.request.get("report_mode") != "periodic-v1"
			or execution.request.get("report_project") != identity or execution.request.get("report_task") != task_id
			or execution.repo != str(project._folder(identity) / "work")):
		raise ValueError("점검 실행의 소유 관계가 잘못됐습니다")
	with project._edit(identity, "periodic_run_linked") as row:
		matches = [task for task in row["tasks"] if task["id"] == task_id]
		if (row.get("audit_report_mode") != "explicit-v1" or not row.get("schedule_repo")
				or row["status"] != "실행 중" or row["checkpoint"] != {"step": "task", "detail": task_id}
				or len(matches) != 1 or matches[0]["status"] != "실행 중" or matches[0]["run_id"]
				or any(task["run_id"] == run_id for task in row["tasks"])):
			raise ValueError("점검 작업 상태가 바뀌었습니다")
		usage = row.setdefault("periodic_usage", {})
		if not isinstance(usage, dict) or run_id in usage:
			raise ValueError("점검 사용량 기록이 이미 있거나 잘못됐습니다")
		matches[0]["run_id"] = run_id
		usage[run_id] = {"calls": 0, "seconds": 0}
	return row

def _periodic_usage(execution):
	seconds = 0.0
	for call in execution.calls:
		if not isinstance(call, dict) or "started" in call and "seconds" not in call:
			raise ValueError("점검 호출 시간을 확인해야 합니다")
		value = call.get("seconds", 0)
		if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
			raise ValueError("점검 호출 시간이 잘못됐습니다")
		seconds += value
	return {"calls": len(execution.calls), "seconds": seconds}

def _periodic_previous(row, execution):
	identity, task_id = execution.request.get("report_project"), execution.request.get("report_task")
	if (identity != row.get("id") or row.get("audit_report_mode") != "explicit-v1" or not row.get("schedule_repo")
			or row["status"] != "실행 중" and not (row["status"] == "막힘" and row.get("blocked_from") == "실행 중")
			or len([task for task in row["tasks"] if task["id"] == task_id and task["run_id"] == execution.run_id]) != 1):
		raise ValueError("점검 사용량의 소유 관계를 확인할 수 없습니다")
	usage = row.get("periodic_usage")
	previous = usage.get(execution.run_id) if isinstance(usage, dict) else None
	if (not isinstance(previous, dict) or set(previous) != {"calls", "seconds"}
			or type(previous["calls"]) is not int or previous["calls"] < 0
			or type(previous["seconds"]) not in (int, float) or not math.isfinite(previous["seconds"])
			or previous["seconds"] < 0):
		raise ValueError("점검 사용량 기록이 잘못됐습니다")
	return previous

def cap_periodic_budget(row: dict, execution) -> None:
	usage, previous = _periodic_usage(execution), _periodic_previous(row, execution)
	if previous["calls"] > usage["calls"] or previous["seconds"] > usage["seconds"]:
		raise ValueError("기록된 점검 사용량이 줄었습니다")
	calls = max(0, row["budget"]["calls"] - row["used"]["calls"] - usage["calls"] + previous["calls"])
	seconds = max(0, row["budget"]["minutes"] * 60 - row["used"]["seconds"] - usage["seconds"] + previous["seconds"])
	execution.budget["calls"] = min(execution.budget["calls"], usage["calls"] + calls)
	execution.budget["seconds"] = min(execution.budget["seconds"], execution.elapsed + seconds)

def settle_periodic_usage(identity: str, execution) -> dict:
	usage = _periodic_usage(execution)
	with project._edit(identity, "periodic_run_usage") as row:
		previous = _periodic_previous(row, execution)
		if previous["calls"] > usage["calls"] or previous["seconds"] > usage["seconds"]:
			raise ValueError("기록된 점검 사용량이 줄었습니다")
		row["used"]["calls"] += usage["calls"] - previous["calls"]
		row["used"]["seconds"] += usage["seconds"] - previous["seconds"]
		row["periodic_usage"][execution.run_id] = usage
	return row

def periodic_ask(worker, role, prompt, schema, writer):
	from orchestrator import flow, providers
	from orchestrator.record import now
	run = worker.run
	plans = flow.role_plans(worker.plans, worker.orchestration, role)
	if role == "review":
		implemented = {call.get("plan") for call in run.calls if call.get("role") == "implement" and call.get("ok") is True}
		plans = [plan for plan in plans if plan.id not in implemented]
	for plan in plans[:flow.MAX_PLAN_TRIES]:
		worker.save()
		cap_periodic_budget(project.load(run.request["report_project"]), run)
		if run.calls_left() <= 0:
			run.finish("호출 한도 소진", "프로젝트에 점검 호출 예산이 남지 않았습니다")
			return None
		if run.seconds_left() < 31:
			run.finish("시간 초과", "프로젝트에 안전한 최소 호출 시간 30초가 남지 않았습니다")
			return None
		record = {"role": role, "plan": plan.id, "tool": plan.tool, "started": now(), "ok": None,
			"seconds": max(0, run.seconds_left() - 1)}
		run.calls.append(record)
		run.agent, run.current_action = plan.tool, f"{role} · {plan.id}"
		run.event("call", role=role, plan=plan.id)
		worker.save()
		timeout = run.seconds_left() - 1
		if timeout < 30:
			run.calls.pop()
			run.event("call_cancelled", role=role, plan=plan.id, reason="프로젝트 남은 시간 부족")
			run.finish("시간 초과", "프로젝트에 안전한 최소 호출 시간 30초가 남지 않았습니다")
			return None
		started = time.monotonic()
		try:
			reply = worker.caller(plan, writer, prompt, Path(run.worktree), schema, timeout)
		except providers.ProviderError as exc:
			seconds = math.ceil((time.monotonic() - started) * 1000) / 1000
			record.update(ok=False, error=f"{exc.kind}: {exc}", seconds=seconds)
			run.event("call_failed", role=role, plan=plan.id, error_kind=exc.kind)
			worker.save()
			continue
		seconds = max(time.monotonic() - started, reply.seconds)
		record.update(ok=True, seconds=math.ceil(seconds * 1000) / 1000, usage=reply.usage)
		worker.save()
		return reply.data
	if run.status == "작업 중":
		quota = any("한도 소진" in str(call.get("error", "")) for call in run.calls if call.get("role") == role)
		run.finish("실패", f"{role} 담당 호출이 모두 실패했습니다" + ("(한도 소진)" if quota else "(요금제·로그인 확인 필요)"))
	return None

def recover_report_seal(execution) -> dict:
	from orchestrator import __main__ as orchestration
	from . import document_run
	with orchestration.locked(execution):
		latest = document_run.load(execution.run_id)
		seal = document_run.report_seal(latest)
		previous = latest.handoff.get("periodic_report_seal")
		if previous is not None and previous != seal:
			raise ValueError("보고서의 검증 기록이 다릅니다")
		if previous is None:
			latest.handoff["periodic_report_seal"] = seal
			latest.save()
		return seal


def prepare_evidence(identity: str, task: dict, *, isolation_attempt: str | None = None, policy=None, command=()) -> dict:
	if task.get("status") != "실행 중" or task.get("run_id"):
		raise ValueError("시작 전 점검 작업만 근거를 준비할 수 있습니다")
	content = _unverified_evidence(identity, task)
	file = evidence_path(identity, task["id"])
	if file.exists():
		existing = _read_evidence_file(file)
		periodic_results.verify(content, existing, file.parent)
		return {"version": 1, "sha256": inputs._hash(existing)}
	if policy is not None:
		if isolation_attempt is not None:
			raise ValueError("격리 시험 기록과 정책을 동시에 지정할 수 없습니다")
		isolation_attempt = periodic_results.execute(file.parent, policy, command)
	if isolation_attempt is not None:
		content = periodic_results.attach(content, file.parent, isolation_attempt)
	file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	file.parent.chmod(0o700)
	if not file.exists():
		fd, temporary = tempfile.mkstemp(prefix=".evidence-" + task["id"] + "-", dir=file.parent)
		with os.fdopen(fd, "wb") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		try:
			os.link(temporary, file, follow_symlinks=False)
		except FileExistsError:
			pass
	if _read_evidence_file(file) != content:
		raise ValueError("점검 근거의 되읽기 결과가 다릅니다")
	return {"version": 1, "sha256": inputs._hash(content)}

def read_evidence(identity: str, task: dict) -> dict:
	content = _read_evidence_file(evidence_path(identity, task["id"]))
	if task.get("report_evidence") != {"version": 1, "sha256": inputs._hash(content)}:
		raise ValueError("점검 근거의 기록 해시가 다릅니다")
	return periodic_results.verify(_unverified_evidence(identity, task), content, evidence_path(identity, task["id"]).parent)


def render_evidence(evidence: dict) -> str:
	labels = {"tests": "시험", "dependencies": "의존성"}
	checks = evidence.get("checks") if isinstance(evidence, dict) else None
	dependencies = evidence.get("dependencies", []) if isinstance(evidence, dict) else None
	if (not isinstance(checks, list) or not checks or any(not isinstance(item, dict)
			or set(item) != {"kind", "status", "reason"} or item.get("kind") not in labels
			or item.get("status") != "unverified" or item.get("reason") != "source_unavailable" for item in checks)):
		raise ValueError("점검 근거를 보고서로 변환할 수 없습니다")
	if (not isinstance(dependencies, list) or any(not isinstance(item, dict)
			or set(item) != {"task", "seal", "document"} or not isinstance(item["task"], str)
			or not re.fullmatch(r"t[1-9][0-9]*", item["task"]) or not isinstance(item["seal"], str)
			or not re.fullmatch(r"[0-9a-f]{64}", item["seal"]) or not isinstance(item["document"], str)
			or not item["document"].strip() for item in dependencies)):
		raise ValueError("선행 보고 근거를 확인할 수 없습니다")
	lines = ["# 정기 점검 보고서", "", "## 근거", "",
		"- 요청된 점검: " + ", ".join(labels[item["kind"]] + " 점검" for item in checks), "", "## 미검증", ""]
	lines += [("- 시험: 합성 부분 시험만 실행해 전체 시험은 미검증입니다." if item["kind"] == "tests" and (evidence.get("synthetic_test") or evidence.get("facts", {}).get("partial")) else
		"- 의존성: 직접 선언을 대조했으며 전이 의존성·보안 공지·지원 상태는 미검증입니다." if item["kind"] == "dependencies" and evidence.get("facts", {}).get("dependencies") else
		"- " + labels[item["kind"]] + ": 안전한 자료와 격리 실행 근거가 없어 실행하지 않았습니다.") for item in checks]
	for item in dependencies:
		lines += ["", "## 선행 보고 " + item["task"], "", item["document"].rstrip()]
	from . import periodic_facts
	from . import periodic_retry
	return ("\n".join(lines) + periodic_results.summary(evidence)
		+ (periodic_facts.summary(evidence["facts"]) if evidence.get("facts") else "")
		+ periodic_retry.isolation_summary(evidence) + "\n")
