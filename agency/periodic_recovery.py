"""검토 거절된 정기 점검을 원본을 보존한 별도 보고 시도로 전환한다."""

import json
import os
import re
import stat
import tomllib
from pathlib import Path

from collector.backfill_common import locked, safe
from orchestrator import checks, draft, flow, prompts, targets
from orchestrator.__main__ import read_request
from vaultfilter.public import Rules
from . import documents, document_run, inputs, periodic_facts, project, schedule
from .departments import load_departments


def _snapshot(path):
	path = Path(path)
	if not path.is_absolute() or not safe(path):
		raise ValueError("원본 보존 경로가 잘못됐습니다")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "rb") as stream:
		info = os.fstat(stream.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink not in (1, 2):
			raise ValueError("원본 보존 자료는 소유한 일반 파일이어야 합니다")
		raw = stream.read(inputs.MAX_OUTPUT + 1)
		if len(raw) > inputs.MAX_OUTPUT:
			raise ValueError("원본 보존 자료가 크기 한도를 넘었습니다")
		if info.st_nlink == 2:
			if re.fullmatch(r"t[1-9][0-9]*\.json", path.name):
				prefix = ".evidence-" + path.stem + "-"
			elif re.fullmatch(r"review-[0-9]+\.json", path.name):
				prefix = ".review-" + path.stem.removeprefix("review-") + "-"
			else:
				raise ValueError("원본 보존 자료의 링크를 확인할 수 없습니다")
			if documents._read_private_file(path, prefix) != raw:
				raise ValueError("원본 보존 자료가 읽는 중 바뀌었습니다")
		return raw


def _write(path, data, *, readonly=False):
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "wb") as stream:
		stream.write(data)
		stream.flush()
		os.fsync(stream.fileno())
		if readonly:
			os.fchmod(stream.fileno(), 0o400)


def _inside(path, folder):
	path = Path(path)
	return path.is_absolute() and safe(path) and path.resolve().is_relative_to(folder.resolve())


def _request(original, task, reports, identity, folder, manifest_hash):
	request = original["request"]
	if request.get("kind", "code") == "code":
		request = {key: request[key] for key in ("role", "goal", "done", "forbidden", "details")}
		request["goal"] = "기존 코드 작업의 확인된 사실과 미검증 범위를 보고한다"
		request["done"] = ["오류 여부와 수정 완료를 추측하지 않고 확인된 사실과 미검증 범위를 구분한다"]
	else:
		request = {key: request[key] for key in ("role", "goal", "done", "forbidden", "details")}
	request["details"] += "\n이 보고는 기존 실패를 보존하고 오류 존재 및 수정 완료를 추측하지 않는다."
	request.update(kind="analysis", workdir=str(folder / "work"), report_mode="periodic-v1",
		report_project=identity, report_task=task["id"], report_checks=reports[task["id"]],
		report_recovery_sha256=manifest_hash)
	dependencies = [item for item in task["depends_on"] if item in reports]
	if dependencies:
		request["report_dependencies"] = dependencies
	return {"request": request, "budget": original["budget"]}


def _failed(identity, task, original, sources, *, previous_run=None):
	if previous_run is None and task.get("status") == "대기":
		if task.get("run_id") or task.get("commit") or task.get("report_seal"):
			raise ValueError("시작되지 않은 작업의 실행 기록이 잘못됐습니다")
		if task.get("report_evidence"):
			documents.read_evidence(identity, task)
		return
	if task.get("status") != "실패" or not task.get("run_id") or task.get("commit") or task.get("report_seal"):
		raise ValueError("검토 거절 또는 미시작 작업만 복구할 수 있습니다")
	execution = document_run.load(previous_run or task["run_id"])
	run_raw = _snapshot(execution.folder / "run.json")
	if vars(execution) != vars(document_run.load(execution.run_id)):
		raise ValueError("이전 실행 기록이 읽는 중 바뀌었습니다")
	if previous_run is None:
		document_run.check(identity, task, execution, project._folder(identity) / "work")
	elif (execution.repo != str(project._folder(identity) / "work") or execution.kind != "analysis"
			or tomllib.loads(_snapshot(execution.folder / "request.toml").decode("utf-8")) != original):
		raise ValueError("이전 보고 시도와 요청서가 다릅니다")
	if (execution.status != "실패" or execution.request != original["request"]
			or execution.request.get("report_mode") != "periodic-v1"):
		raise ValueError("이전 실패가 같은 프로젝트의 보고 실행이 아닙니다")
	evidence = documents.read_evidence(identity, task)
	if (execution.document != documents.render_evidence(evidence)
			or execution.handoff.get("periodic_evidence_sha256") != task["report_evidence"]["sha256"]):
		raise ValueError("이전 보고와 봉인 근거가 다릅니다")
	review = periodic_facts.parse(documents.read_review(execution))
	prompt = documents.review_prompt(execution, prompts.review(execution.request, execution.kind,
		execution.document, execution.checks))
	if (documents._review_call(execution).get("periodic_review_input_sha256") != inputs._hash(prompt.encode())
			or not isinstance(review, dict) or review.get("verdict") not in ("pass", "changes")
			or not isinstance(review.get("findings"), list)
			or review.get("verdict") == "pass" and not any(
				isinstance(item, dict) and item.get("severity") in flow.BLOCKING
				for item in review["findings"])):
		raise ValueError("이전 독립 검토가 거절된 기록이 아닙니다")
	files = [execution.folder / "run.json", execution.folder / "request.toml",
		execution.folder / ("review-" + str(execution.fixes) + ".json"), documents.evidence_path(identity, task["id"])]
	files += [execution.folder / name for name in ("events.jsonl", "report.md") if (execution.folder / name).exists()]
	if evidence.get("synthetic_test"):
		files.append(documents.evidence_path(identity, task["id"]).parent / "isolation" / evidence["synthetic_test"]["name"])
	private = execution.handoff.get("private_document")
	if isinstance(private, dict) and isinstance(private.get("sha256"), str):
		files.append(execution.folder / "documents" / (private["sha256"] + ".md"))
	for path in files:
		raw = _snapshot(path)
		if path.name == "run.json" and raw != run_raw:
			raise ValueError("이전 실행 기록이 검토 중 바뀌었습니다")
		sources[str(path)] = raw


def replan(identity, choice):
	reports = choice["reports"]
	row = project.load(identity)
	if (row.get("status") != "막힘" or row.get("blocked_from") != "실행 중" or not row.get("schedule_repo")
			or row.get("audit_report_mode") != "explicit-v1" or row.get("periodic_recovery")
			or not 1 <= len(row.get("tasks", [])) <= 4 or not any(task.get("status") == "실패" for task in row["tasks"])):
		raise ValueError("막힌 정기 점검의 검토 거절만 복구할 수 있습니다")
	project._check_tasks(row["tasks"])
	if set(reports) != {task["id"] for task in row["tasks"]}:
		raise ValueError("복구 시 모든 작업을 명시적인 보고로 지정해야 합니다")
	if inputs._hash(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode()) != row.get("spec_hash"):
		raise ValueError("확정 명세가 바뀌었습니다")
	department = load_departments().get(row["department"])
	if department is None or department.deliverable != "code" or not department.external_ai:
		raise ValueError("코드 점검 부서를 확인할 수 없습니다")
	selected = targets.target(schedule.target(row, department))
	base = row.get("audit_base")
	if not isinstance(base, str) or not periodic_facts.COMMIT.fullmatch(base):
		raise ValueError("점검 기준 커밋이 없습니다")
	tip = checks.git(selected.path, "rev-parse", "--verify", "refs/heads/" + row["branch"], check=False).strip()
	if tip != base:
		raise ValueError("기존 프로젝트 가지가 기준 커밋과 다릅니다")
	folder = project._folder(identity)
	if any((folder / name).exists() for name in ("report.md", "delivery.json")):
		raise ValueError("기존 최종 검수 또는 납품 기록이 있어 덮어쓸 수 없습니다")
	facts_path, record_path = Path(choice["facts"]), Path(choice["isolation_record"])
	if not _inside(record_path, folder):
		raise ValueError("격리 기록은 같은 프로젝트에 속해야 합니다")
	facts_raw, record_raw = periodic_facts.read_file(facts_path), periodic_facts.read_file(record_path)
	periodic_facts.validate(facts_raw, record_raw, record_path.name, identity, base, selected, Rules())
	ledger = _snapshot(folder / "project.json")
	if periodic_facts.parse(ledger) != row:
		raise ValueError("프로젝트 원장이 읽는 중 바뀌었습니다")
	sources = {str(facts_path): facts_raw, str(record_path): record_raw}
	originals = []
	for task in row["tasks"]:
		path = Path(task["request_path"])
		if not _inside(path, folder):
			raise ValueError("이전 요청서가 같은 프로젝트에 속하지 않습니다")
		raw = inputs._read(path, inputs.MAX_OUTPUT)
		if task.get("request_sha256") != inputs._hash(raw):
			raise ValueError("이전 요청서가 변경됐습니다")
		original = tomllib.loads(raw.decode("utf-8"))
		request = original["request"]
		if (not isinstance(request, dict) or not isinstance(original.get("budget"), dict)
				or any(key not in request for key in ("role", "goal", "done", "forbidden", "details"))):
			raise ValueError("이전 요청서 형식이 잘못됐습니다")
		if request.get("kind", "code") == "code":
			if request.get("repo") != str(selected.path) or request.get("base_branch") != row["branch"]:
				raise ValueError("이전 코드 요청의 대상이 다릅니다")
		elif (request.get("kind") != "analysis" or request.get("report_mode") != "periodic-v1"
				or request.get("report_project") != identity or request.get("report_task") != task["id"]):
			raise ValueError("이전 보고 요청의 대상이 다릅니다")
		sources[str(path)] = raw
		_failed(identity, task, original, sources)
		old_document = documents.path(identity, task["id"])
		if old_document.exists():
			sources[str(old_document)] = _snapshot(old_document)
		old_evidence = documents.evidence_path(identity, task["id"])
		if old_evidence.exists():
			sources[str(old_evidence)] = _snapshot(old_evidence)
		originals.append(original)
	usage = row.get("periodic_usage", {})
	if not isinstance(usage, dict):
		raise ValueError("이전 점검 실행 이력이 잘못됐습니다")
	known = {task["run_id"] for task in row["tasks"] if task.get("run_id")}
	for run_id in usage:
		if run_id in known:
			continue
		execution = document_run.load(run_id)
		matches = [(task, original) for task, original in zip(row["tasks"], originals)
			if execution.request.get("report_project") == identity and execution.request.get("report_task") == task["id"]]
		if len(matches) != 1:
			raise ValueError("이전 점검 실행이 이 프로젝트의 작업에 속하지 않습니다")
		_failed(identity, matches[0][0], matches[0][1], sources, previous_run=run_id)
	previous = row.get("periodic_replan")
	if previous:
		entries = [{"path": previous["path"], "sha256": previous["sha256"]}, *previous.get("sources", [])]
		for entry in entries:
			path = Path(entry["path"])
			if not _inside(path, folder):
				raise ValueError("이전 재계획 보존본이 다른 프로젝트에 속합니다")
			raw = _snapshot(path)
			if inputs._hash(raw) != entry["sha256"]:
				raise ValueError("이전 재계획 보존본이 변경됐습니다")
			sources[str(path)] = raw
	destination = folder / ("periodic-recovery-" + os.urandom(16).hex())
	if not safe(destination):
		raise ValueError("복구 보존 폴더에 링크를 사용할 수 없습니다")
	destination.mkdir(mode=0o700)
	_write(destination / "original-project.json", ledger, readonly=True)
	_write(destination / "facts.json", facts_raw, readonly=True)
	_write(destination / "isolation-record.json", record_raw, readonly=True)
	preserved = []
	for number, (path, raw) in enumerate(sources.items()):
		name = "original-" + str(number) + ".bin"
		_write(destination / name, raw, readonly=True)
		preserved.append({"source": path, "copy": name, "sha256": inputs._hash(raw)})
	manifest = {"version": 1, "project": identity, "base_commit": base, "reports": reports,
		"original_project_sha256": inputs._hash(ledger), "facts_sha256": inputs._hash(facts_raw),
		"isolation_record_sha256": inputs._hash(record_raw), "isolation_record_name": record_path.name,
		"facts_source": str(facts_path), "isolation_record_source": str(record_path),
		"preserved": preserved}
	manifest_raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
	_write(destination / "manifest.json", manifest_raw, readonly=True)
	manifest_hash = inputs._hash(manifest_raw)
	rewritten = []
	for task, original in zip(row["tasks"], originals):
		prepared = _request(original, task, reports, identity, folder, manifest_hash)
		content = draft.serialize(prepared).encode()
		path = destination / (task["id"] + ".toml")
		_write(path, content)
		if read_request(str(path)) != prepared:
			raise ValueError("새 보고 요청서를 다시 읽을 수 없습니다")
		rewritten.append({"id": task["id"], "title": task["title"], "depends_on": task["depends_on"],
			"status": "대기", "run_id": "", "commit": "", "request_path": str(path),
			"request_sha256": inputs._hash(content)})
	with locked("agency-repo:" + str(selected.path)):
		with project._edit(identity, "periodic_recovery") as current:
			if current != row or checks.git(selected.path, "rev-parse", "--verify", "refs/heads/" + row["branch"]).strip() != base:
				raise ValueError("복구 중 원장 또는 기준 가지가 바뀌었습니다")
			if any(_snapshot(Path(path)) != data for path, data in sources.items()):
				raise ValueError("복구 중 원본 자료가 바뀌었습니다")
			if any(inputs._hash(inputs._read(Path(task["request_path"]), inputs.MAX_OUTPUT)) != task["request_sha256"] for task in rewritten):
				raise ValueError("복구 중 새 요청서가 바뀌었습니다")
			current["tasks"] = rewritten
			current["periodic_recovery"] = {"path": str(destination / "manifest.json"), "sha256": manifest_hash}
	return {"tasks": [{key: task[key] for key in ("id", "title", "depends_on")} for task in rewritten],
		"size": "small" if len(rewritten) == 1 else "medium", "plan": "explicit-recovery", "meeting": False}


def state(identity):
	folder = project._folder(identity)
	if not (folder / "project.json").exists():
		return None
	row = project.load(identity)
	anchor = row.get("periodic_recovery")
	if anchor is None:
		return None
	if (not isinstance(anchor, dict) or set(anchor) != {"path", "sha256"}
			or not isinstance(anchor["sha256"], str) or not periodic_facts.HASH.fullmatch(anchor["sha256"])):
		raise ValueError("복구 보존 기록이 잘못됐습니다")
	path = Path(anchor["path"])
	if not _inside(path, folder) or path.name != "manifest.json" or not path.parent.name.startswith("periodic-recovery-"):
		raise ValueError("복구 보존 기록이 프로젝트 밖입니다")
	raw = periodic_facts.read_file(path)
	if inputs._hash(raw) != anchor["sha256"]:
		raise ValueError("복구 보존 기록이 바뀌었습니다")
	manifest = periodic_facts.parse(raw)
	if (not isinstance(manifest, dict) or manifest.get("project") != identity
			or manifest.get("base_commit") != row.get("audit_base") or manifest.get("version") != 1):
		raise ValueError("복구 기록의 프로젝트 또는 기준 커밋이 다릅니다")
	return row, path.parent, manifest, anchor["sha256"]


def output_base(identity):
	result = state(identity)
	return result[1] if result else project._folder(identity)


def facts_for_task(identity, task, request):
	result = state(identity)
	if result is None:
		if "report_recovery_sha256" in request:
			raise ValueError("복구 기록이 없습니다")
		return None
	row, folder, manifest, anchor = result
	if request.get("report_recovery_sha256") != anchor:
		raise ValueError("보고 요청과 복구 기록이 다릅니다")
	try:
		ledger = periodic_facts.read_file(folder / "original-project.json")
		facts_raw = periodic_facts.read_file(folder / "facts.json")
		record_raw = periodic_facts.read_file(folder / "isolation-record.json")
		if (inputs._hash(ledger) != manifest["original_project_sha256"] or inputs._hash(facts_raw) != manifest["facts_sha256"]
				or inputs._hash(record_raw) != manifest["isolation_record_sha256"]):
			raise ValueError("복구 보존본이 바뀌었습니다")
		if (inputs._hash(periodic_facts.read_file(manifest["facts_source"])) != manifest["facts_sha256"]
				or inputs._hash(periodic_facts.read_file(manifest["isolation_record_source"])) != manifest["isolation_record_sha256"]):
			raise ValueError("원래 사실 자료 또는 격리 기록이 바뀌었습니다")
		for entry in manifest["preserved"]:
			copy = folder / entry["copy"]
			if copy.parent != folder or inputs._hash(periodic_facts.read_file(copy)) != entry["sha256"]:
				raise ValueError("복구 원본 보존본이 바뀌었습니다")
			if inputs._hash(_snapshot(Path(entry["source"]))) != entry["sha256"]:
				raise ValueError("이전 원본 자료가 바뀌었습니다")
		old = periodic_facts.parse(ledger)
		if any(row.get(key) != old.get(key) for key in
				("spec", "spec_hash", "request_text", "answers", "schedule_repo", "branch", "department", "periodic_replan")):
			raise ValueError("원래 프로젝트의 요청 또는 보존 기록이 바뀌었습니다")
		if ([{key: item[key] for key in ("id", "title", "depends_on")} for item in row["tasks"]]
				!= [{key: item[key] for key in ("id", "title", "depends_on")} for item in old["tasks"]]):
			raise ValueError("복구 작업의 의존 관계가 바뀌었습니다")
		previous = next(item for item in old["tasks"] if item["id"] == task["id"])
		if (task["depends_on"] != previous["depends_on"] or task["title"] != previous["title"]
				or Path(task["request_path"]) != folder / (task["id"] + ".toml")):
			raise ValueError("복구 작업의 의존 관계가 바뀌었습니다")
		original = tomllib.loads(inputs._read(Path(previous["request_path"]), inputs.MAX_OUTPUT).decode("utf-8"))
		expected = _request(original, previous, manifest["reports"], identity, project._folder(identity), anchor)
		actual = tomllib.loads(periodic_facts.read_file(Path(task["request_path"])).decode("utf-8"))
		if actual != expected or actual["request"] != request:
			raise ValueError("복구 보고 요청서가 바뀌었습니다")
		facts = periodic_facts.parse(facts_raw)
		record = periodic_facts.parse(record_raw)
		if (facts["project"] != identity or facts["base_commit"] != row["audit_base"]
				or facts["tests"]["partial"]["record"]["sha256"] != inputs._hash(record_raw)
				or facts["tests"]["partial"]["record"]["name"] != manifest["isolation_record_name"]
				or record["tests"] != facts["tests"]["partial"]["counts"]):
			raise ValueError("복구 사실과 격리 기록이 다릅니다")
		return periodic_facts.evidence(facts, request["report_checks"])
	except (KeyError, TypeError, StopIteration, UnicodeError, tomllib.TOMLDecodeError):
		raise ValueError("복구 사실 또는 보존 기록을 확인할 수 없습니다") from None
