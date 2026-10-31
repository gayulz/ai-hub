"""의존 순서로 실행하고 검증을 통과한 결과를 프로젝트 가지에 쌓는다."""

import re
import subprocess
import tomllib
from datetime import date
from pathlib import Path

from collector.backfill_common import locked, safe
from collector.config import ConfigError
from orchestrator import __main__ as orchestration, targets, test_isolation, isolated_inputs
from orchestrator.branch_apply import apply_to_branch
from orchestrator.checks import git
from orchestrator.flow import worktrees_dir
from orchestrator.record import Run, runs_dir

from . import document_run, documents, inputs, meeting, project, schedule, periodic_results
from .departments import load_departments


def _result(row: dict) -> dict:
	return {"status": row["status"], "done": [task["id"] for task in row["tasks"] if task["status"] == "완료"],
		"blocked_reason": row["blocked_reason"]}


def _validate(request: dict, repo: Path, branch: str, *, document=False) -> None:
	if document:
		if (request.get("kind") != "analysis" or request.get("workdir") != str(repo)
				or any(key in request for key in ("repo", "base_branch", "owned_paths")) or not safe(repo)):
			raise ValueError("문서 요청서는 프로젝트의 읽기 전용 작업 폴더를 사용해야 합니다")
		return
	if (Path(request.get("repo", ".")).resolve() != repo or request.get("base_branch") != branch
			or request.get("kind", "code") != "code"):
		raise ValueError("작업 요청서는 부서의 저장소와 프로젝트 가지를 기준으로 한 코드 작업이어야 합니다")


def _task_is_document(row: dict, task: dict, project_document: bool) -> bool:
	if project_document:
		return True
	if not row.get("schedule_repo") or row.get("audit_report_mode") != "explicit-v1":
		return False
	path = Path(task["request_path"])
	if (not path.is_absolute() or not safe(path)
			or not path.resolve().is_relative_to(project._folder(row["id"]).resolve())):
		raise ValueError("점검 작업 요청서의 소유 경계가 잘못됐습니다")
	raw = inputs._read(path, inputs.MAX_OUTPUT)
	if task.get("request_sha256") != inputs._hash(raw):
		raise ValueError("점검 작업 요청서가 계획 뒤 바뀌었습니다")
	try:
		request = tomllib.loads(raw.decode("utf-8"))["request"]
		if not isinstance(request, dict):
			raise ValueError
		kind = request.get("kind", "code")
	except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
		raise ValueError("점검 작업 요청서 형식이 잘못됐습니다") from None
	if kind not in ("analysis", "code"):
		raise ValueError("점검 작업 종류가 잘못됐습니다")
	return kind == "analysis"


def _verify_report_dependencies(row: dict, task: dict, request: dict) -> None:
	fields = {key: request[key] for key in ("report_project", "report_task", "report_dependencies") if key in request}
	if not row.get("schedule_repo") or row.get("audit_report_mode") != "explicit-v1":
		if fields:
			raise ValueError("명시되지 않은 보고 의존 관계입니다")
		return
	try:
		project._check_tasks(row["tasks"])
		if len(set(task["depends_on"])) != len(task["depends_on"]):
			raise ValueError("선행 보고의 의존 관계가 중복됐습니다")
		matches = [item for item in row["tasks"] if item["id"] == task["id"]]
		if len(matches) != 1 or matches[0] != task:
			raise ValueError
		reports = []
		for dependency in task["depends_on"]:
			previous = next(item for item in row["tasks"] if item["id"] == dependency)
			if previous["status"] != "완료":
				raise ValueError
			if _task_is_document(row, previous, False):
				document_run.verified_report(row["id"], previous)
				reports.append(dependency)
		expected = ({"report_project": row["id"], "report_task": task["id"]}
			if reports or request.get("report_mode") == "periodic-v1" else {})
		if reports:
			expected["report_dependencies"] = reports
		if fields != expected:
			raise ValueError
	except (KeyError, TypeError, StopIteration):
		raise ValueError("선행 보고의 의존 관계가 잘못됐습니다") from None


def _usage(identity: str, execution: Run, previous_usage: dict | None = None) -> Run:
	# 재개·반영 재시도로 같은 호출을 두 번 청구하지 않는다.
	with orchestration.locked(execution):
		latest = document_run.load(execution.run_id) if execution.kind == "analysis" else Run.load(execution.run_id)
		if latest.run_id != execution.run_id:
			raise ValueError("실행 기록의 번호가 다릅니다")
		if latest.kind == "code" and orchestration.unmeasured_call_time(latest.calls):
			raise ValueError("실행의 호출 시간을 확인해야 합니다")
		usage = {"calls": len(latest.calls), "seconds": sum(call.get("seconds", 0) for call in latest.calls)}
		if latest.request.get("report_mode") == "periodic-v1":
			documents.settle_periodic_usage(identity, latest)
		else:
			owner = latest.handoff.get("agency_owner")
			if owner is not None:
				if not isinstance(owner, dict) or set(owner) != {"project", "task"} or owner["project"] != identity:
					raise ValueError("실행 사용량의 프로젝트 소유 관계가 다릅니다")
				project.settle_run_usage(identity, owner["task"], latest.run_id, usage["calls"], usage["seconds"])
			elif latest.kind == "analysis":
				previous = previous_usage if previous_usage is not None else latest.handoff.get("agency_usage", {"calls": 0, "seconds": 0})
				project.add_usage(identity, usage["calls"] - previous["calls"], usage["seconds"] - previous["seconds"])
			else:
				row = project.load(identity)
				matches = [task for task in row["tasks"] if task["run_id"] == latest.run_id]
				if len(matches) != 1:
					raise ValueError("실행 사용량의 프로젝트 작업을 확인할 수 없습니다")
				_request_matches(identity, matches[0], Path(latest.repo), row["branch"], latest)
				previous = previous_usage if previous_usage is not None else latest.handoff.get("agency_usage", {"calls": 0, "seconds": 0})
				project.settle_run_usage(identity, matches[0]["id"], latest.run_id, usage["calls"], usage["seconds"], legacy_previous=previous)
		latest.handoff["agency_usage"] = usage
		latest.save()
		return latest


def _request_matches(identity: str, task: dict, repo: Path, branch: str, execution: Run) -> None:
	path = Path(task["request_path"])
	if not path.is_absolute() or not safe(path) or not path.resolve().is_relative_to(project._folder(identity).resolve()):
		raise ValueError("프로젝트 작업 요청서 경로가 올바르지 않습니다")
	data = orchestration.read_request(str(path))
	copy = execution.folder / "request.toml"
	if not safe(copy) or not safe(execution.folder / "run.json"):
		raise ValueError("실행 기록에 심볼릭 링크를 사용할 수 없습니다")
	with copy.open("rb") as stream:
		copied = tomllib.load(stream)
	if data != copied or execution.request != data["request"]:
		raise ValueError("새 실행과 프로젝트 작업의 요청서가 다릅니다")
	_validate(execution.request, repo, branch)


def _link(identity: str, task_id: str, repo: Path, branch: str, execution: Run) -> None:
	row = project.load(identity)
	matches = [task for task in row["tasks"] if task["id"] == task_id]
	if (len(matches) != 1 or row["status"] != "실행 중" or row["branch"] != branch
			or matches[0]["status"] != "실행 중" or matches[0]["run_id"] or matches[0]["commit"]
			or execution.status != "작업 중" or execution.kind != "code" or execution.repo != str(repo)
			or execution.step != "prepare" or execution.worktree or execution.calls or execution.handoff):
		raise ValueError("새 실행과 프로젝트 작업의 소유 관계가 다릅니다")
	_request_matches(identity, matches[0], repo, branch, execution)
	project.cap_run_budget(row, execution, new=True)
	execution.handoff["agency_owner"] = {"project": identity, "task": task_id}
	execution.save()
	project.link_run(identity, task_id, execution.run_id, branch)


def _worktrees(repo: Path) -> list[tuple[str, str]]:
	result = []
	for entry in git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
		if not entry:
			continue
		fields = entry.split("\0")
		branches = [field[7:] for field in fields if field.startswith("branch ")]
		if not fields[0].startswith("worktree ") or len(branches) > 1:
			raise RuntimeError("Git 작업 폴더 목록을 확인할 수 없습니다")
		result.append((fields[0][9:], branches[0] if branches else ""))
	return result


def _branch_exists(repo: Path, ref: str) -> bool:
	refs = git(repo, "for-each-ref", "--format=%(refname)", ref).splitlines()
	result = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref],
		capture_output=True)
	if result.returncode not in (0, 1) or (ref in refs) != (result.returncode == 0):
		raise RuntimeError("Git 가지 참조를 확인할 수 없습니다")
	return result.returncode == 0


def _cleanup_previous(identity: str, task: dict, repo: Path, branch: str) -> None:
	run_id = task["run_id"]
	if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id):
		raise ValueError("직전 실행 번호 형식이 잘못됐습니다")
	try:
		date.fromisoformat(run_id[:10])
		if run_id.endswith("-000"):
			raise ValueError
	except ValueError:
		raise ValueError("직전 실행 번호 형식이 잘못됐습니다") from None
	folder = runs_dir() / run_id
	for path in (folder, *(folder / name for name in ("run.json", "request.toml", "report.md", "events.jsonl", ".lock"))):
		if not safe(path):
			raise ValueError("직전 실행 기록에 심볼릭 링크를 사용할 수 없습니다")
	if not folder.is_dir() or not all((folder / name).is_file() for name in ("run.json", "request.toml")):
		raise ValueError("직전 실행 기록 또는 요청서가 없습니다")
	if (folder / ".lock").exists() and not (folder / ".lock").is_file():
		raise ValueError("직전 실행 잠금은 일반 파일이어야 합니다")
	execution = Run.load(run_id)
	if execution.run_id != run_id:
		raise ValueError("직전 실행 기록의 번호가 다릅니다")
	with orchestration.locked(execution):
		execution = Run.load(run_id)
		row = project.load(identity)
		matches = [item for item in row["tasks"] if item["id"] == task["id"]]
		if (len(matches) != 1 or matches[0] != task or task["status"] != "대기" or row["branch"] != branch
				or any(item["id"] != task["id"] and item["run_id"] == run_id for item in row["tasks"])):
			raise ValueError("대기 작업의 직전 실행이 바뀌었습니다")
		if (execution.run_id != run_id or execution.status not in
				("실패", "시간 초과", "호출 한도 소진", "예산 소진", "사용자 입력 필요")
				or execution.approval_pending or not isinstance(execution.end_reason, str)
				or "(한도 소진)" not in execution.end_reason):
			raise ValueError("직전 실행은 정리할 수 있는 한도 대기 상태가 아닙니다")
		request_path = Path(task["request_path"])
		if (not request_path.is_absolute() or not safe(request_path)
				or not request_path.resolve().is_relative_to(project._folder(identity).resolve())):
			raise ValueError("대기 작업의 요청서 경로가 잘못됐습니다")
		data = orchestration.read_request(str(request_path))
		with (folder / "request.toml").open("rb") as stream:
			previous_request = tomllib.load(stream)
		if previous_request != data or execution.request != data["request"] or execution.repo != str(repo):
			raise ValueError("직전 실행과 대기 작업의 요청 또는 저장소가 다릅니다")
		request_repo = data["request"].get("repo", ".")
		if not isinstance(request_repo, str) or not safe(Path(request_repo)):
			raise ValueError("요청서의 저장소 경로에 심볼릭 링크를 사용할 수 없습니다")
		_validate(data["request"], repo, branch)
		_validate({**execution.request, "repo": execution.repo, "kind": execution.kind}, repo, branch)
		worktree = worktrees_dir() / run_id
		expected_branch = "orch/" + run_id
		ref = "refs/heads/" + expected_branch
		if worktree == repo:
			raise ValueError("현재 저장소는 정리할 수 없습니다")
		if branch == expected_branch:
			raise ValueError("프로젝트 기준 가지는 정리할 수 없습니다")
		if (not worktree.is_absolute() or not safe(worktree) or execution.branch not in ("", expected_branch)
				or execution.worktree not in ("", str(worktree))):
			raise ValueError("직전 실행의 작업 폴더 또는 가지가 다릅니다")
		entries = _worktrees(repo)
		registered = [item for item in entries if item[0] == str(worktree)]
		if (len(registered) > 1 or any(path != str(worktree) and name == ref for path, name in entries)
				or registered and (registered[0][1] != ref or not execution.worktree or not execution.branch)
				or worktree.exists() and (not registered or not worktree.is_dir())):
			raise ValueError("직전 실행의 Git 작업 폴더 소유 관계가 다릅니다")
		if worktree.exists():
			if not safe(worktree / ".git") or not (worktree / ".git").is_file():
				raise ValueError("직전 실행의 Git 작업 폴더가 올바르지 않습니다")
			common = git(worktree, "rev-parse", "--git-common-dir").strip()
			repo_common = git(repo, "rev-parse", "--git-common-dir").strip()
			if ((worktree / common).resolve() != (repo / repo_common).resolve()
					or git(worktree, "symbolic-ref", "-q", "HEAD").strip() != ref
					or git(worktree, "rev-parse", "--show-toplevel").strip() != str(worktree)):
				raise ValueError("직전 실행의 Git 작업 폴더가 대상 저장소와 다릅니다")
		branch_exists = _branch_exists(repo, ref)
		if branch_exists and not execution.branch:
			raise ValueError("직전 실행의 가지 소유 관계를 확인할 수 없습니다")
		if Run.load(run_id) != execution:
			raise RuntimeError("정리 중 직전 실행 기록이 바뀌었습니다")
		if registered:
			git(repo, "worktree", "remove", "--force", str(worktree))
		if worktree.exists() or any(path == str(worktree) or name == ref for path, name in _worktrees(repo)):
			raise RuntimeError("직전 실행의 Git 작업 폴더가 남았습니다")
		branch_exists = _branch_exists(repo, ref)
		if Run.load(run_id) != execution:
			raise RuntimeError("정리 중 직전 실행 기록이 바뀌었습니다")
		if branch_exists:
			git(repo, "branch", "-D", expected_branch)
		if not safe(worktree) or worktree.exists() or _branch_exists(repo, ref) or Run.load(run_id) != execution:
			raise RuntimeError("직전 실행의 Git 정리가 완료되지 않았습니다")
		project.update_task(identity, task["id"], status="실행 중", run_id="", commit="")


def run(identity: str) -> dict:
	row = project.load(identity)
	department = load_departments().get(row["department"])
	if department is None:
		raise ValueError("프로젝트의 부서 설정이 없습니다")
	document = department.deliverable == "document"
	if document and not department.external_ai:
		if department.prepare and row["status"] in ("계획", "실행 중") and row["tasks"]:
			try:
				inputs.collect(identity, department, include_materials=False)
			except inputs.InputError as exc:
				return _result(project.transition(identity, "막힘", reason=str(exc)))
		raise ValueError("외부 AI 금지 부서는 문서 실행 호출을 쓸 수 없습니다")
	selected = None if document else targets.target(schedule.target(row, department))
	repo = project._folder(identity) / "work" if document else selected.path
	with locked("agency-repo:" + str(repo)):
		row = project.load(identity)
		# 작업이 0건이면 아래 '모두 완료' 검사가 참이 되어 빈 프로젝트가 검수로 넘어간다. 상태를 바꾸기 전에 거부한다.
		if not row["tasks"]:
			raise ValueError("등록된 작업이 없습니다. 먼저 python3 -m agency plan 으로 계획하세요")
		missing_inputs = (selected is not None and (selected.id == "blog" or test_isolation.requires(str(selected.path)))
			and not getattr(selected, "isolated_inputs", ()))
		if missing_inputs and not all(task["status"] == "완료" for task in row["tasks"]):
			try:
				next_task = next((task for task in row["tasks"] if task["status"] == "실행 중"), None)
				next_task = next_task or next(iter(project.next_ready_tasks(row)), None)
				is_report = next_task is not None and _task_is_document(row, next_task, document)
			except (KeyError, OSError, ValueError):
				is_report = False
			if not is_report:
				return _result(project.transition(identity, "막힘", reason="합성 소스와 격리 실행 경로가 준비되지 않았습니다"))
		if row["status"] == "막힘":
			row = project.resume(identity)
		if row["status"] == "계획":
			row = project.transition(identity, "실행 중")
		if row["status"] != "실행 중":
			raise ValueError("계획 또는 실행 중인 프로젝트만 실행할 수 있습니다")
		try:
			meeting.ensure(identity, department)
		except Exception as exc:
			message = str(exc)
			if "외부 AI 금지" in message:
				reason = "외부 AI 금지 부서는 회의를 호출할 수 없습니다"
			elif "중단" in message or "불확실" in message:
				reason = "회의 호출 중단 상태를 확인해야 합니다"
			elif "예산" in message:
				reason = "회의를 위한 프로젝트 예산이 부족하거나 소진됐습니다"
			else:
				reason = "회의 자문·결정 또는 설정을 확인해야 합니다"
			# 직접 만든 오류 문구만 붙인다. 시스템 오류 문구에는 로컬 경로가 들어갈 수 있다.
			reason += ": " + (message[:200] if isinstance(exc, (ValueError, ConfigError)) else type(exc).__name__)
			if (project.load(identity).get("meeting") or {}).get("status") in ("failed", "uncertain"):
				reason += f" (확인 뒤 python3 -m agency meeting-retry {identity})"
			return _result(project.transition(identity, "막힘", reason=reason))
		row = project.load(identity)
		branch = row["branch"]
		ref = "refs/heads/" + branch
		if not document and not git(repo, "rev-parse", "--verify", ref, check=False).strip():
			if any(task["status"] == "대기" and task["run_id"] for task in row["tasks"]):
				raise ValueError("직전 실행의 프로젝트 기준 가지가 없습니다")
			head = git(repo, "rev-parse", "HEAD").strip()
			git(repo, "branch", branch, head)
			project.set_checkpoint(identity, "branch", head)
		row = project.load(identity)
		if not document and row.get("audit_report_mode") == "explicit-v1" and not row.get("audit_base"):
			tip = git(repo, "rev-parse", "--verify", ref).strip()
			if tip != git(repo, "rev-parse", "HEAD").strip():
				return _result(project.transition(identity, "막힘", reason="점검 기준 가지가 본 가지와 다릅니다"))
			try:
				project.set_audit_base(identity, branch, tip)
			except ValueError:
				return _result(project.transition(identity, "막힘", reason="점검 기준 커밋을 안전하게 고정할 수 없습니다"))
		while True:
			row = project.load(identity)
			if row["status"] != "실행 중":
				raise ValueError("프로젝트가 실행 중이 아닙니다")
			if project.over_budget(row):
				return _result(project.transition(identity, "막힘", reason="프로젝트 예산을 넘었습니다"))
			if all(task["status"] == "완료" for task in row["tasks"]):
				return _result(project.transition(identity, "검수"))
			task = next((task for task in row["tasks"] if task["status"] == "실행 중"), None)
			if task and not task["run_id"]:
				row = project.update_task(identity, task["id"], status="대기")
				task = None
			previous_usage = None
			if task is None:
				if not document and project.budget_exhausted(row):
					return _result(project.transition(identity, "막힘", reason="프로젝트 예산을 모두 사용했습니다"))
				ready = project.next_ready_tasks(row)
				if not ready:
					return _result(project.transition(identity, "막힘", reason="실행할 수 있는 대기 작업이 없습니다"))
				task = ready[0]
				try:
					task_document = _task_is_document(row, task, document)
				except (OSError, ValueError):
					return _result(project.transition(identity, "막힘", reason="점검 작업 요청서를 확인할 수 없습니다"))
				if missing_inputs and not task_document:
					return _result(project.transition(identity, "막힘", reason="합성 소스와 격리 실행 경로가 준비되지 않았습니다"))
				task_repo = project._folder(identity) / "work" if task_document else repo
				if not task_document or not document:
					try:
						_verify_report_dependencies(row, task, orchestration.read_request(task["request_path"])["request"])
					except (OSError, ValueError):
						return _result(project.transition(identity, "막힘", reason="선행 보고 또는 의존 관계를 확인할 수 없습니다"))
				if task["run_id"]:
					if task_document:
						document_run.retry(identity, task, document_run.load(task["run_id"]), task_repo)
						_validate(orchestration.read_request(task["request_path"])["request"], task_repo, branch, document=True)
					else:
						_cleanup_previous(identity, task, repo, branch)
				else:
					_validate(orchestration.read_request(task["request_path"])["request"], task_repo, branch, document=task_document)
					project.update_task(identity, task["id"], status="실행 중", run_id="", commit="")
				if task_document and not document:
					try:
						task = next(item for item in project.load(identity)["tasks"] if item["id"] == task["id"])
						if not task.get("report_evidence"):
							anchor = periodic_results.prepare(identity, task, selected,
								git(repo, "rev-parse", "refs/heads/" + branch).strip())
							project.update_task(identity, task["id"], report_evidence=anchor)
							task = next(item for item in project.load(identity)["tasks"] if item["id"] == task["id"])
						documents.read_evidence(identity, task)
					except (OSError, ValueError):
						project.update_task(identity, task["id"], status="대기", run_id="")
						return _result(project.transition(identity, "막힘", reason="점검 근거를 확인할 수 없습니다"))
				project.set_checkpoint(identity, "task", task["id"])
				try:
					execution = (document_run.start(identity, task, department, task_repo) if task_document
						else orchestration.start_run(task["request_path"], on_created=lambda run:
							_link(identity, task["id"], repo, branch, run)))
				except (OSError, ValueError, RuntimeError):
					if not task_document:
						raise
					current = next(item for item in project.load(identity)["tasks"] if item["id"] == task["id"])
					if not current["run_id"]:
						project.update_task(identity, task["id"], status="대기")
					return _result(project.transition(identity, "막힘", reason="문서 실행 준비 또는 기록에 실패했습니다"))
				if document:
					project.update_task(identity, task["id"], run_id=execution.run_id)
				elif not any(item["id"] == task["id"] and item["run_id"] == execution.run_id
						for item in project.load(identity)["tasks"]):
					raise ValueError("새 실행 번호가 프로젝트 작업에 연결되지 않았습니다")
			else:
				try:
					task_document = _task_is_document(row, task, document)
				except (OSError, ValueError):
					return _result(project.transition(identity, "막힘", reason="점검 작업 요청서를 확인할 수 없습니다"))
				if missing_inputs and not task_document:
					return _result(project.transition(identity, "막힘", reason="합성 소스와 격리 실행 경로가 준비되지 않았습니다"))
				task_repo = project._folder(identity) / "work" if task_document else repo
				if not task_document or not document:
					try:
						_verify_report_dependencies(row, task, orchestration.read_request(task["request_path"])["request"])
					except (OSError, ValueError):
						return _result(project.transition(identity, "막힘", reason="선행 보고 또는 의존 관계를 확인할 수 없습니다"))
				if task_document and not document:
					try:
						documents.read_evidence(identity, task)
					except (OSError, ValueError):
						return _result(project.transition(identity, "막힘", reason="점검 근거가 바뀌었습니다"))
				execution = document_run.load(task["run_id"]) if task_document else Run.load(task["run_id"])
				if task_document:
					_validate(execution.request, task_repo, branch, document=True)
					document_run.check(identity, task, execution, task_repo)
				else:
					_validate({**execution.request, "repo": execution.repo, "kind": execution.kind}, repo, branch)
				if execution.status not in ("승인 필요", "완료"):
					previous_usage = execution.handoff.get("agency_usage")
					if task_document:
						if document and execution.status != "작업 중" and "(한도 소진)" not in execution.end_reason:
							_usage(identity, execution, previous_usage)
							project.update_task(identity, task["id"], status="실패")
							return _result(project.transition(identity, "막힘",
								reason="실패 문서는 document-retry 명령으로 재시도해야 합니다"))
						try:
							document_run.resume(identity, task, execution, department, task_repo)
						except inputs.InputError:
							return _result(project.transition(identity, "막힘", reason="입력 자료 또는 권한을 확인해야 합니다"))
					else:
						try:
							execution = _usage(identity, execution, previous_usage)
						except (OSError, ValueError):
							return _result(project.transition(identity, "막힘", reason="실행 사용량을 확인하지 못했습니다. 이전 기록을 대조해야 합니다"))
						if project.over_budget(project.load(identity)):
							return _result(project.transition(identity, "막힘", reason="프로젝트 예산을 넘었습니다"))
						previous_usage = execution.handoff.get("agency_usage")
						orchestration.cmd_resume(execution.run_id, [])
			try:
				execution = _usage(identity, execution, previous_usage)
			except (OSError, ValueError):
				return _result(project.transition(identity, "막힘", reason="실행 사용량을 확인하지 못했습니다. 이전 기록을 대조해야 합니다"))
			reason = ""
			if execution.status in ("승인 필요", "완료"):
				try:
					report_seal = None
					if task_document:
						if not document:
							report_seal = documents.recover_report_seal(execution)
						documents.save(identity, task["id"], execution.document)
						commit = ""
					else:
						current = project.load(identity)
						if current.get("audit_report_mode") == "explicit-v1":
							expected = "orch/" + execution.run_id
							if execution.status == "승인 필요" and execution.branch != expected:
								raise ValueError("점검 실행 가지가 작업과 다릅니다")
							candidate_ref = "refs/heads/" + expected if execution.status == "승인 필요" else ref
							tip = git(repo, "rev-parse", "--verify", candidate_ref).strip()
							documents.periodic_changed_paths(current, repo, tip)
						commit = (apply_to_branch(execution.run_id, branch) if execution.status == "승인 필요"
							else git(repo, "rev-parse", ref).strip())
				except (RuntimeError, ValueError, OSError) as exc:
					message = (str(exc) if not task_document or type(exc) is ValueError else "납품 문서 저장 실패")
					reason = f"작업 {task['id']} 실패: " + message[:200]
					project.update_task(identity, task["id"], status="실패")
				else:
					options = {"report_seal": report_seal} if report_seal is not None else {}
					project.update_task(identity, task["id"], status="완료", commit=commit, **options)
			else:
				quota = "(한도 소진)" in execution.end_reason
				project.update_task(identity, task["id"], status="대기" if quota else "실패")
				reason = ("한도 소진으로 대기: " if quota else f"작업 {task['id']} 실패: ") + execution.end_reason[:200]
				if task_document and project.budget_exhausted(project.load(identity)):
					reason = "프로젝트 예산을 모두 사용했습니다: " + reason
			if project.over_budget(project.load(identity)):
				reason = "프로젝트 예산을 넘었습니다"
			if reason:
				return _result(project.transition(identity, "막힘", reason=reason))
