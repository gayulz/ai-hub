"""의존 순서로 실행하고 검증을 통과한 결과를 프로젝트 가지에 쌓는다."""

from pathlib import Path

from collector.backfill_common import locked
from orchestrator import __main__ as orchestration, targets
from orchestrator.branch_apply import apply_to_branch
from orchestrator.checks import git
from orchestrator.record import Run

from . import project
from .departments import load_departments


def _result(row: dict) -> dict:
	return {"status": row["status"], "done": [task["id"] for task in row["tasks"] if task["status"] == "완료"],
		"blocked_reason": row["blocked_reason"]}


def _validate(request: dict, repo: Path, branch: str) -> None:
	if (Path(request.get("repo", ".")).resolve() != repo or request.get("base_branch") != branch
			or request.get("kind", "code") != "code"):
		raise ValueError("작업 요청서는 부서의 저장소와 프로젝트 가지를 기준으로 한 코드 작업이어야 합니다")


def _usage(identity: str, execution: Run) -> None:
	# 재개·반영 재시도로 같은 호출을 두 번 청구하지 않는다.
	previous = execution.handoff.get("agency_usage", {"calls": 0, "seconds": 0})
	usage = {"calls": len(execution.calls), "seconds": sum(call.get("seconds", 0) for call in execution.calls)}
	project.add_usage(identity, usage["calls"] - previous["calls"], usage["seconds"] - previous["seconds"])
	execution.handoff["agency_usage"] = usage
	execution.save()


def run(identity: str) -> dict:
	row = project.load(identity)
	department = load_departments().get(row["department"])
	if department is None:
		raise ValueError("프로젝트의 부서 설정이 없습니다")
	repo = targets.target(department.repos[0]).path
	with locked("agency-repo:" + str(repo)):
		row = project.load(identity)
		# 작업이 0건이면 아래 '모두 완료' 검사가 참이 되어 빈 프로젝트가 검수로 넘어간다. 상태를 바꾸기 전에 거부한다.
		if not row["tasks"]:
			raise ValueError("등록된 작업이 없습니다. 먼저 python3 -m agency plan 으로 계획하세요")
		if row["status"] == "막힘":
			row = project.resume(identity)
		if row["status"] == "계획":
			row = project.transition(identity, "실행 중")
		if row["status"] != "실행 중":
			raise ValueError("계획 또는 실행 중인 프로젝트만 실행할 수 있습니다")
		branch = row["branch"]
		ref = "refs/heads/" + branch
		if not git(repo, "rev-parse", "--verify", ref, check=False).strip():
			head = git(repo, "rev-parse", "HEAD").strip()
			git(repo, "branch", branch, head)
			project.set_checkpoint(identity, "branch", head)
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
			if task is None:
				ready = project.next_ready_tasks(row)
				if not ready:
					return _result(project.transition(identity, "막힘", reason="실행할 수 있는 대기 작업이 없습니다"))
				task = ready[0]
				_validate(orchestration.read_request(task["request_path"])["request"], repo, branch)
				project.update_task(identity, task["id"], status="실행 중", run_id="", commit="")
				project.set_checkpoint(identity, "task", task["id"])
				execution = orchestration.start_run(task["request_path"])
				project.update_task(identity, task["id"], run_id=execution.run_id)
			else:
				execution = Run.load(task["run_id"])
				_validate({**execution.request, "repo": execution.repo, "kind": execution.kind}, repo, branch)
				if execution.status not in ("승인 필요", "완료"):
					usage = execution.handoff.get("agency_usage")
					orchestration.cmd_resume(execution.run_id, [])
					execution = Run.load(execution.run_id)
					if usage is not None:
						execution.handoff["agency_usage"] = usage
			_usage(identity, execution)
			reason = ""
			if execution.status in ("승인 필요", "완료"):
				try:
					commit = (apply_to_branch(execution.run_id, branch) if execution.status == "승인 필요"
						else git(repo, "rev-parse", ref).strip())
				except RuntimeError as exc:
					reason = f"작업 {task['id']} 실패: " + str(exc)[:200]
					project.update_task(identity, task["id"], status="실패")
				else:
					project.update_task(identity, task["id"], status="완료", commit=commit)
			else:
				quota = "(한도 소진)" in execution.end_reason
				project.update_task(identity, task["id"], status="대기" if quota else "실패")
				reason = ("한도 소진으로 대기: " if quota else f"작업 {task['id']} 실패: ") + execution.end_reason[:200]
			if project.over_budget(project.load(identity)):
				reason = "프로젝트 예산을 넘었습니다"
			if reason:
				return _result(project.transition(identity, "막힘", reason=reason))
