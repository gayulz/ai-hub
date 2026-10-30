"""봉인된 부분 시험 근거와 문서 재시도의 경계를 확인한다."""

import hashlib
import json
import re
from pathlib import Path

from collector.backfill_common import locked
from orchestrator import prompts
from . import documents, inputs, periodic_facts, periodic_recovery, project
from .departments import load_departments


def isolation_summary(evidence):
	partial = evidence.get("facts", {}).get("partial")
	if not partial:
		return ""
	identity = evidence.get("project")
	state = periodic_recovery.state(identity)
	if state is None:
		raise ValueError("부분 시험의 복구 기록이 없습니다")
	row, folder, manifest, _ = state
	facts_raw = periodic_facts.read_file(folder / "facts.json")
	record_raw = periodic_facts.read_file(folder / "isolation-record.json")
	if (inputs._hash(facts_raw) != manifest.get("facts_sha256")
			or inputs._hash(record_raw) != manifest.get("isolation_record_sha256")
			or inputs._hash(record_raw) != partial.get("record", {}).get("sha256")):
		raise ValueError("부분 시험의 복구 사실 또는 격리 기록이 바뀌었습니다")
	facts, record = periodic_facts.parse(facts_raw), periodic_facts.parse(record_raw)
	policy = facts["tests"]["partial"]["policy"]
	if (facts["tests"]["partial"]["record"] != partial["record"]
			or inputs._hash(json.dumps(policy, sort_keys=True).encode()) != record.get("policy_sha256")):
		raise ValueError("부분 시험의 정책과 기록이 다릅니다")
	lines = ["", "## 합성 부분 시험 격리 근거", ""]
	args = ("-m", "unittest", "discover", "-s", "tests")
	commands = []
	for executable in policy.get("exec", []):
		if isinstance(executable, str) and re.fullmatch(r"python3(?:\.[0-9]+)?", Path(executable).name):
			command = (executable, *args)
			if hashlib.sha256(json.dumps(command).encode()).hexdigest() == record.get("command_sha256"):
				commands.append(Path(executable).name + " " + " ".join(args))
	lines.append("- 부분 시험 실행 명령: " + ("`" + commands[0] + "` (기록의 명령 해시와 일치)"
		if len(commands) == 1 else "미확인(기록에는 명령 해시만 있음)"))
	guard = record.get("guard", {})
	process = record.get("guard_process", {})
	if not isinstance(guard, dict) or not isinstance(process, dict):
		raise ValueError("합성 차단 검증 형식이 잘못됐습니다")
	controls = process.get("status") == "passed" and process.get("controls") == "synthetic-operations-allowed"
	department = load_departments().get(row["department"])
	selected = documents.unchanged_repo(row, department) if department is not None else None
	protected = policy.get("protected", [])
	boundaries = (isinstance(protected, list) and selected is not None
		and str(Path.home().resolve()) in protected and str(selected.path.resolve()) in protected)
	file_keys = ("read", "read_link", "read_unlisted", "read_volume_alias", "write", "hardlink")
	file_guard = controls and all(guard.get(key) == "denied" for key in file_keys)
	lines.append("- 보호 경계: " + ("실제 홈과 대상 저장소를 정책에 포함했고 합성 보호 파일의 읽기·쓰기와 연결 접근 차단을 확인했다."
		if boundaries and file_guard else "실제 홈·대상 보호 정책 또는 합성 보호 파일 차단 결과가 미확인이다."))
	workspace = controls and all(guard.get(key) == value for key, value in
		(("source_read", "allowed"), ("source_write", "denied"), ("scratch_write", "allowed")))
	lines.append("- 임시 HOME과 작업 영역: " + ("실행기는 임시 HOME을 사용하며 합성 소스 읽기와 임시 쓰기를 허용하고 소스 쓰기는 차단했다."
		if workspace else "임시 HOME의 작업 영역 차단 검증이 미확인이다."))
	network = controls and policy.get("network") == "deny-all" and all(guard.get(key) == "denied"
		for key in ("tcp", "unix", "child_tcp", "child_unix"))
	lines.append("- 네트워크: " + ("전체 차단 정책과 합성 TCP·Unix 및 합성 자식의 연결 차단을 확인했다."
		if network else "네트워크 정책 또는 합성 연결 차단 결과가 미확인이다."))
	nofork = record.get("no_fork_guard", {})
	no_process = record.get("no_fork_process", {})
	execution = controls and all(guard.get(key) == "denied" for key in
		("binary", "binary_link", "script", "script_link", "child_binary"))
	execution = (execution and isinstance(nofork, dict) and isinstance(no_process, dict)
		and no_process.get("status") == "passed" and all(nofork.get(key) == "denied"
			for key in ("fork", "child", "posix_spawn", "posix_spawnp")))
	lines.append("- 실행과 자식 프로세스: " + ("합성 바이너리·스크립트와 새 프로세스 생성 차단을 확인했다."
		if execution else "합성 실행 또는 자식 프로세스 생성 차단 결과가 미확인이다."))
	lines.append("- 브라우저: 실제 브라우저는 실행하지 않았으며, 브라우저별 실행 차단은 별도로 검증하지 않았다.")
	lines.append("- 자손 소속: 격리 상속 정책은 있으나 각 자손의 실제 소속은 별도로 관찰하지 않아 미확인이다.")
	return "\n".join(lines) + "\n"


def verify(identity, task, execution, workdir):
	from . import document_run
	document_run.check(identity, task, execution, workdir)
	if (execution.status != "실패" or execution.approval_pending
			or execution.request.get("report_mode") != "periodic-v1" or task.get("report_seal")):
		raise ValueError("독립 검토에서 거절된 정기 보고만 재시도할 수 있습니다")
	evidence = documents.read_evidence(identity, task)
	if execution.handoff.get("periodic_evidence_sha256") != task["report_evidence"]["sha256"]:
		raise ValueError("이전 실행과 봉인된 보고 근거가 다릅니다")
	review = periodic_facts.parse(documents.read_review(execution))
	prompt = documents.review_prompt(execution, prompts.review(execution.request, execution.kind,
		execution.document, execution.checks))
	if (documents._review_call(execution).get("periodic_review_input_sha256") != inputs._hash(prompt.encode())
			or not isinstance(review, dict) or review.get("verdict") != "changes"):
		raise ValueError("이전 독립 검토의 거절을 확인할 수 없습니다")
	if documents.render_evidence(evidence) == execution.document:
		raise ValueError("보고 내용이 바뀌지 않아 같은 검토를 반복하지 않습니다")


def retry(identity, task, execution, workdir):
	from orchestrator import __main__ as orchestration
	from . import document_run
	with orchestration.locked(execution):
		execution = document_run.load(execution.run_id)
		document_run.check(identity, task, execution, workdir)
		if (execution.status not in ("실패", "시간 초과", "호출 한도 소진", "예산 소진", "사용자 입력 필요")
				or execution.approval_pending):
			raise ValueError("직전 문서 실행은 한도 대기 상태가 아닙니다")
		if "(한도 소진)" not in execution.end_reason:
			verify(identity, task, execution, workdir)
		project.update_task(identity, task["id"], status="실행 중", run_id="", commit="")


def prepare(identity, task_id):
	from . import document_run
	row = project.load(identity)
	department = load_departments().get(row["department"])
	if (row.get("status") != "막힘" or row.get("blocked_from") != "실행 중"
			or not row.get("periodic_recovery") or department is None):
		raise ValueError("막힌 복구 보고만 재시도할 수 있습니다")
	selected = documents.unchanged_repo(row, department)
	with locked("agency-repo:" + str(selected.path)):
		row = project.load(identity)
		if (row.get("status") != "막힘" or row.get("blocked_from") != "실행 중"
				or not row.get("periodic_recovery") or documents.unchanged_repo(row, department).path != selected.path
				or any((project._folder(identity) / name).exists() for name in ("report.md", "delivery.json"))):
			raise ValueError("재검토 전 프로젝트 상태 또는 기존 납품 기록이 바뀌었습니다")
		task = next((item for item in row["tasks"] if item["id"] == task_id), None)
		if task is None or task.get("status") != "실패" or not task.get("run_id"):
			raise ValueError("실패한 보고 작업을 지정해야 합니다")
		execution = document_run.load(task["run_id"])
		verify(identity, task, execution, project._folder(identity) / "work")
		with project._edit(identity, "periodic_report_retry") as current:
			if current != row:
				raise ValueError("재검토 준비 중 프로젝트가 바뀌었습니다")
			next(item for item in current["tasks"] if item["id"] == task_id)["status"] = "대기"
	return {"tasks": [{key: item[key] for key in ("id", "title", "depends_on")} for item in row["tasks"]],
		"size": "small", "plan": "explicit-report-retry", "meeting": False}
