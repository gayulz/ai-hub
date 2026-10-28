"""대행사 원장에서 사무실에 필요한 요약만 읽는다."""

from __future__ import annotations

import math

from agency import intake, project
from agency.departments import ID, valid_budget, valid_text

from .common import normal, unavailable


def read_projects() -> dict:
	items = []
	try:
		for row in project.list_projects():
			title, department = row["title"], row["department"]
			tasks, budget, used = row["tasks"], row["budget"], row["used"]
			reason = row.get("blocked_reason", "")
			if (not valid_text(title, 200) or not isinstance(department, str) or not ID.fullmatch(department)
					or not isinstance(reason, str) or len(reason) > 1000 or "\x00" in reason
					or not isinstance(tasks, list) or any(not isinstance(t, dict) or t.get("status") not in project.TASK_STATUSES for t in tasks)
					or not valid_budget(budget) or not isinstance(used, dict) or type(used.get("calls")) is not int
					or used["calls"] < 0 or type(used.get("seconds")) not in (int, float)
					or not math.isfinite(used["seconds"]) or used["seconds"] < 0):
				raise ValueError
			waiting = bool(intake._questions(row["id"])["open"]) if row["status"] == "접수 중" else False
			items.append({"id": row["id"], "title": title, "department": department, "status": row["status"],
				"completed": sum(t["status"] == "완료" for t in tasks), "total": len(tasks),
				"blocked_reason": reason if row["status"] == "막힘" else "",
				"budget": {"calls": budget["calls"], "minutes": budget["minutes"]},
				"used": {"calls": used["calls"], "seconds": used["seconds"]}, "question_waiting": waiting})
		return normal(items=items)
	except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
		return unavailable("대행사 프로젝트 원장을 확인할 수 없습니다", items=items)
