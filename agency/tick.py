"""진행 중인 프로젝트마다 상태에 맞는 다음 단계 하나만 실행한다."""

import fcntl
import os
import stat
from datetime import datetime, timedelta

from collector.backfill_common import now, safe
from collector.config import ConfigError, home

from . import acceptance, hooks, intake, planner, project, runner
from .departments import load_departments

RETRY_MINUTES = 60
ERRORS = (ValueError, RuntimeError, ConfigError, OSError)


def _action(row: dict) -> str:
	status = row["status"]
	if status == "접수 중":
		if intake._questions(row["id"])["open"] or row["checkpoint"] == {
				"step": "intake", "detail": str(len(row["answers"]))}:
			return "wait"
		return "intake"
	if status == "명세 확정" or status == "계획" and not row["tasks"]:
		return "plan"
	if status in ("계획", "실행 중"):
		return "run"
	if status == "검수":
		return "review"
	return "wait"


def _advance(row: dict) -> dict:
	identity = row["id"]
	result = {"id": identity, "before": row["status"], "after": row["status"], "action": "wait", "error": ""}
	try:
		department = load_departments().get(row["department"])
		if department is None or not department.external_ai:
			result["action"] = "skip"
		else:
			if row["status"] == "막힘" and "한도 소진" in row["blocked_reason"]:
				updated = datetime.fromisoformat(row["updated"])
				if updated.tzinfo is None:
					raise ValueError("프로젝트 갱신 시각에 시간대가 없습니다")
				if now() - updated >= timedelta(minutes=RETRY_MINUTES):
					result["action"] = "retry"
					row = project.resume(identity)
			action = _action(row)
			if result["action"] != "retry":
				result["action"] = action
			if action == "intake":
				count = str(len(row["answers"]))
				intake.step(identity)
				project.set_checkpoint(identity, "intake", count)
			elif action == "plan":
				planner.plan(identity)
			elif action == "run":
				runner.run(identity)
			elif action == "review":
				acceptance.review(identity)
	except ERRORS as exc:
		result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
	try:
		result["after"] = project.load(identity)["status"]
	except ERRORS as exc:
		if not result["error"]:
			result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
	if result["before"] != result["after"]:
		hooks.changed(identity, result["before"], result["after"])
	return result


def tick() -> dict:
	path = home() / "run" / "locks" / "agency-tick.lock"
	if not safe(path):
		raise ValueError("진행기 잠금에 심볼릭 링크를 사용할 수 없습니다")
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
	with os.fdopen(fd, "r+") as lock:
		if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
			raise ValueError("진행기 잠금은 일반 파일이어야 합니다")
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			return {"skipped": True}
		failures = hooks.before_tick()
		rows = sorted(project.list_projects(), key=lambda row: row["id"])
		return {"skipped": False, "hook_failures": failures, "projects": [_advance(row) for row in rows]}
