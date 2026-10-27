"""잠금 안에서만 변경하는 프로젝트 원장. 실행과 외부 반영은 하지 않는다."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from collector.backfill_common import locked, now, private_write, safe
from collector.config import home

from .departments import BUDGET_LIMITS, ID as DEPARTMENT_ID, Department, valid_budget, valid_text

ID = re.compile(r"p-[0-9]{8}-[0-9]{3}")
STATUSES = ("접수 중", "명세 확정", "계획", "실행 중", "검수", "납품 대기", "납품 중", "완료", "막힘", "취소")
TRANSITIONS = {"명세 확정": ("계획",), "계획": ("실행 중",), "실행 중": ("검수",),
	"검수": ("실행 중", "납품 대기"), "납품 대기": ("납품 중", "실행 중"), "납품 중": ("완료",)}
TASK_STATUSES = ("대기", "실행 중", "완료", "실패", "건너뜀")
SPEC_KEYS = ("goal", "target", "out_of_scope", "done_criteria", "budget")


def _root() -> Path:
	path = home() / "projects"
	if not safe(path):
		raise ValueError("프로젝트 폴더에 심볼릭 링크를 사용할 수 없습니다")
	return path


def _folder(identity: str) -> Path:
	if not isinstance(identity, str) or not ID.fullmatch(identity) or identity.endswith("-000"):
		raise ValueError("프로젝트 id 형식이 잘못됐습니다")
	path = _root() / identity
	if not all(safe(item) for item in (path, path / "project.json", path / "events.jsonl")):
		raise ValueError("프로젝트 파일에 심볼릭 링크를 사용할 수 없습니다")
	return path


def load(identity: str) -> dict:
	path = _folder(identity) / "project.json"
	try:
		row = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError:
		raise ValueError("프로젝트가 없습니다") from None
	except (UnicodeError, ValueError):
		raise ValueError("프로젝트 원장 형식이 잘못됐습니다") from None
	if (not isinstance(row, dict) or row.get("version") != 1 or row.get("id") != identity
			or row.get("status") not in STATUSES):
		raise ValueError("프로젝트 원장 식별자 또는 상태가 잘못됐습니다")
	return row


def list_projects() -> list[dict]:
	root = _root()
	if not root.exists():
		return []
	return [load(path.name) for path in sorted(root.iterdir())
		if ID.fullmatch(path.name) and (path / "project.json").exists()]


def _write(row: dict, kind: str) -> None:
	folder = _folder(row["id"])
	row["updated"] = now().isoformat(timespec="seconds")
	try:
		text = json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
	except (ValueError, TypeError):
		raise ValueError("프로젝트 값은 올바른 JSON이어야 합니다") from None
	event = json.dumps({"at": row["updated"], "kind": kind, "status": row["status"]}, ensure_ascii=False) + "\n"
	fd = os.open(folder / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
	with os.fdopen(fd, "a", encoding="utf-8") as stream:
		if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
			raise ValueError("프로젝트 사건 기록은 일반 파일이어야 합니다")
		os.fchmod(stream.fileno(), 0o600)
		private_write(folder / "project.json", text)
		stream.write(event)
		stream.flush()
		os.fsync(stream.fileno())


@contextmanager
def _edit(identity: str, kind: str):
	_folder(identity)
	with locked("agency:" + identity):
		row = load(identity)
		yield row
		_write(row, kind)


def new(department: Department, title: str, request_text: str) -> dict:
	if not valid_text(title, 120) or not valid_text(request_text, 4000):
		raise ValueError("제목은 1~120자, 요청 글은 1~4000자여야 합니다")
	if (not isinstance(department.id, str) or not DEPARTMENT_ID.fullmatch(department.id)
			or not valid_budget(department.budget)):
		raise ValueError("부서 식별자 또는 기본 예산이 잘못됐습니다")
	root = _root()
	root.mkdir(parents=True, exist_ok=True, mode=0o700)
	root.chmod(0o700)
	day = now().strftime("%Y%m%d")
	for number in range(1, 1000):
		identity = f"p-{day}-{number:03d}"
		try:
			(root / identity).mkdir(mode=0o700)
			break
		except FileExistsError:
			continue
	else:
		raise ValueError("오늘 프로젝트 번호를 모두 사용했습니다")
	with locked("agency:" + identity):
		stamp = now().isoformat(timespec="seconds")
		row = {"version": 1, "id": identity, "department": department.id, "title": title,
			"request_text": request_text, "status": "접수 중", "spec": {"goal": "", "target": "",
				"out_of_scope": "", "done_criteria": [], "budget": dict(department.budget)},
			"spec_hash": "", "answers": [], "tasks": [], "branch": "agency/" + identity,
			"budget": dict(department.budget), "used": {"calls": 0, "seconds": 0}, "decisions": [],
			"checkpoint": {"step": "", "detail": ""}, "blocked_reason": "", "blocked_from": "",
			"created": stamp, "updated": stamp}
		_write(row, "new")
	return row


def update_spec(identity: str, **fields) -> dict:
	with _edit(identity, "spec") as row:
		if row["spec_hash"]:
			raise ValueError("확정한 명세는 수정할 수 없습니다")
		if fields.keys() - set(SPEC_KEYS):
			raise ValueError("알 수 없는 명세 항목입니다")
		row["spec"].update(fields)
	return row


def add_answer(identity: str, question: str, answer: str) -> dict:
	if any(not isinstance(value, str) or not value.strip() or "\x00" in value for value in (question, answer)):
		raise ValueError("질문과 답변에 빈 문자열을 사용할 수 없습니다")
	with _edit(identity, "answer") as row:
		row["answers"].append({"question": question, "answer": answer, "at": now().isoformat(timespec="seconds")})
	return row


def spec_missing(project: dict, department: Department) -> list[str]:
	spec = project.get("spec")
	spec = spec if isinstance(spec, dict) else {}
	missing = [key for key in SPEC_KEYS[:3] if not valid_text(spec.get(key), 1000)]
	criteria = spec.get("done_criteria")
	if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20 or any(not valid_text(item, 300) for item in criteria):
		missing.append("done_criteria")
	budget = spec.get("budget")
	budget = budget if isinstance(budget, dict) else {}
	for key, limit in BUDGET_LIMITS.items():
		if type(budget.get(key)) is not int or not 1 <= budget[key] <= limit:
			missing.append("budget." + key)
	if budget.keys() - BUDGET_LIMITS.keys():
		missing.append("budget")
	for question in department.required_questions:
		if not any(answer.get("question") == question and isinstance(answer.get("answer"), str)
				and answer["answer"].strip() for answer in project.get("answers", [])):
			missing.append(question)
	return missing


def confirm_spec(identity: str, department: Department) -> dict:
	with _edit(identity, "confirm_spec") as row:
		if row["status"] != "접수 중" or row["spec_hash"]:
			raise ValueError("접수 중인 프로젝트의 명세만 확정할 수 있습니다")
		if row["department"] != department.id:
			raise ValueError("프로젝트의 부서 설정이 다릅니다")
		if spec_missing(row, department):
			raise ValueError("명세 빈칸 또는 답하지 않은 필수 질문이 있습니다")
		row["spec_hash"] = hashlib.sha256(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
		row["budget"] = dict(row["spec"]["budget"])
		row["status"] = "명세 확정"
	return row


def transition(identity: str, status: str, *, reason: str = "") -> dict:
	with _edit(identity, "transition") as row:
		before = row["status"]
		if status not in STATUSES or before in ("완료", "취소"):
			raise ValueError("허용되지 않은 프로젝트 상태 전이입니다")
		if status == "명세 확정":
			raise ValueError("명세 확정은 confirm_spec으로만 할 수 있습니다")
		if status == "막힘":
			if not isinstance(reason, str) or not reason.strip() or "\x00" in reason:
				raise ValueError("막힘 이유가 필요합니다")
			if before != "막힘":
				row["blocked_from"] = before
			row["blocked_reason"] = reason
		elif status == "취소":
			row["blocked_from"], row["blocked_reason"] = "", ""
		elif before == "막힘":
			raise ValueError("막힌 프로젝트는 resume으로만 재개할 수 있습니다")
		elif status not in TRANSITIONS.get(before, ()):
			raise ValueError("허용되지 않은 프로젝트 상태 전이입니다")
		row["status"] = status
	return row


def resume(identity: str) -> dict:
	with _edit(identity, "resume") as row:
		previous = row["blocked_from"]
		if row["status"] != "막힘" or previous not in STATUSES or previous in ("막힘", "완료", "취소"):
			raise ValueError("재개할 수 있는 막힌 프로젝트가 아닙니다")
		row["status"] = previous
		row["blocked_from"], row["blocked_reason"] = "", ""
	return row


def cancel(identity: str) -> dict:
	return transition(identity, "취소")


def _check_tasks(tasks: list) -> None:
	previous = set()
	for number, task in enumerate(tasks, 1):
		dependencies = task["depends_on"]
		if (task["id"] != f"t{number}" or not isinstance(dependencies, list)
				or any(not isinstance(item, str) or item not in previous for item in dependencies)):
			raise ValueError("의존 작업은 같은 프로젝트의 앞선 작업이어야 하며 순환할 수 없습니다")
		previous.add(task["id"])


def add_task(identity: str, title: str, request_path: str, depends_on: list[str] | None = None) -> dict:
	if not valid_text(title, 200) or not isinstance(request_path, str) or "\x00" in request_path:
		raise ValueError("작업 제목은 1~200자이고 요청서 경로는 문자열이어야 합니다")
	with _edit(identity, "task_added") as row:
		folder, path = _folder(identity), Path(request_path)
		try:
			resolved, parent = path.resolve(), folder.resolve()
			valid = path.is_absolute() and safe(path) and resolved.is_relative_to(parent) and resolved != parent
		except (OSError, RuntimeError):
			valid = False
		if not valid:
			raise ValueError("요청서 절대 경로는 이 프로젝트 폴더 안에 있어야 합니다")
		row["tasks"].append({"id": f"t{len(row['tasks']) + 1}", "title": title, "request_path": str(resolved),
			"depends_on": [] if depends_on is None else depends_on, "status": "대기", "run_id": "", "commit": ""})
		_check_tasks(row["tasks"])
	return row


def update_task(identity: str, task_id: str, *, status=None, run_id=None, commit=None) -> dict:
	if status is not None and status not in TASK_STATUSES:
		raise ValueError("작업 상태가 잘못됐습니다")
	if any(value is not None and (not isinstance(value, str) or "\x00" in value) for value in (run_id, commit)):
		raise ValueError("작업 실행 식별자와 커밋은 문자열이어야 합니다")
	with _edit(identity, "task_updated") as row:
		task = next((task for task in row["tasks"] if task["id"] == task_id), None)
		if task is None:
			raise ValueError("프로젝트에 해당 작업이 없습니다")
		for key, value in (("status", status), ("run_id", run_id), ("commit", commit)):
			if value is not None:
				task[key] = value
	return row


def next_ready_tasks(project: dict) -> list[dict]:
	tasks = project["tasks"]
	_check_tasks(tasks)
	completed = {task["id"] for task in tasks if task["status"] == "완료"}
	return [task for task in tasks if task["status"] == "대기" and all(item in completed for item in task["depends_on"])]


def add_usage(identity: str, calls: int, seconds: float) -> dict:
	if (type(calls) is not int or calls < 0 or type(seconds) not in (int, float)
			or type(seconds) is float and not math.isfinite(seconds) or seconds < 0):
		raise ValueError("호출 수는 0 이상 정수, 시간은 0 이상 유한한 초 단위 숫자여야 합니다")
	with _edit(identity, "usage") as row:
		row["used"]["calls"] += calls
		row["used"]["seconds"] += seconds
	return row


def over_budget(project: dict) -> bool:
	return project["used"]["calls"] > project["budget"]["calls"] or project["used"]["seconds"] > project["budget"]["minutes"] * 60


def add_decision(identity: str, topic: str, chosen: str, rejected: str, assumptions: str, verification: str) -> dict:
	values = dict(topic=topic, chosen=chosen, rejected=rejected, assumptions=assumptions, verification=verification)
	if any(not valid_text(value, 1000) for value in values.values()):
		raise ValueError("결정의 각 항목은 1~1000자여야 합니다")
	with _edit(identity, "decision") as row:
		row["decisions"].append(values)
	return row


def set_checkpoint(identity: str, step: str, detail: str) -> dict:
	if any(not isinstance(value, str) or "\x00" in value for value in (step, detail)):
		raise ValueError("체크포인트 단계와 설명은 문자열이어야 합니다")
	with _edit(identity, "checkpoint") as row:
		row["checkpoint"] = {"step": step, "detail": detail}
	return row
