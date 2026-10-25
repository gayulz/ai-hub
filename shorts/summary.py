"""경로·외부 식별자·오류 원문 없이 화면에 필요한 정보만 돌려준다."""

import re

from vaultfilter.public import Rules

from . import states

ATTEMPT_LABELS = {"intended": "호출 전 기록됨", "running": "외부 작업 실행 중", "succeeded": "외부 작업 종료 확인",
	"failed": "외부 작업 실패 확인", "cancelled": "외부 작업 취소 확인", "needs_check": "외부 작업 결과 미상"}
NEXT_ACTIONS = {"waiting": "현재 단계의 작업을 시작해 주세요", "running": "작업 결과를 기다려 주세요",
	"awaiting_user": "표시된 산출물을 확인하고 승인하거나 수정을 요청해 주세요",
	"blocked": "차단 사유를 확인해 주세요", "failed": "실패 원인을 확인한 뒤 다시 진행해 주세요",
	"cancelling": "외부 작업과 하위 프로세스의 종료를 확인해 주세요", "cancelled": "새 제작 회차를 시작할 수 있습니다",
	"succeeded": "완료된 산출물을 확인해 주세요", "needs_check": "외부 작업을 대조한 뒤 결과를 명시해 주세요"}
EMAIL = re.compile(r"[^\s@]+@[^\s@]+")


def summary(run):
	try:
		rules = Rules()
	except (OSError, ValueError, TypeError, KeyError):
		rules = None

	def safe(value):
		if isinstance(value, str):
			if (rules is None or rules.kinds(value) or EMAIL.search(value) or "/" in value or "\\" in value
					or "~" in value or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
				return "[가림]"
			return value
		if isinstance(value, list):
			return [safe(row) for row in value]
		if isinstance(value, dict):
			return {key: safe(item) for key, item in value.items()}
		return value

	stage, status = run["stage"], run["status"]
	bundle = run.get("review")
	wanted = set(bundle["artifacts"]) if bundle else set()
	approval_status = "awaiting_user" if bundle else ("approved" if any(run["approvals"].values()) else "none")
	return safe({"round": run["round"], "stage": stage, "stage_label": states.STAGE_LABELS.get(stage, "알 수 없는 단계"),
		"status": status, "status_label": states.STATUS_LABELS.get(status, "확인이 필요한 상태"),
		"approval_status": approval_status,
		"attempt_status": [{"status": row["status"], "description": ATTEMPT_LABELS.get(row["status"], "확인 필요")}
			for row in run["attempts"]],
		"artifacts": [{"name": row["name"], "kind": row["kind"], "identifier": row["sha256"][:12]}
			for row in run["artifacts"] if row["name"] in wanted],
		"blocked_reason": states.BLOCKED_REASONS.get(run["blocked_reason"], "차단 사유를 확인해 주세요"),
		"next_action": NEXT_ACTIONS.get(status, "상태를 확인해 주세요")})
