"""명시적인 확인 요청·승인과 산출물 변경에 따른 롤백."""

from . import schema, slot, states
from .errors import Conflict, TransitionError


def start_stage(store, intake_id, number):
	with store.locked(intake_id, number) as record:
		states.transition(record.run, "running")
		record.run["blocked_reason"] = ""
		return record.save()


def complete_stage(store, intake_id, number):
	with store.locked(intake_id, number) as record:
		run = record.run
		states.require_settled(run)
		if run["stage"] in states.APPROVAL_SCOPES:
			bundle = states.review(run, run["stage"])
			states.transition(run, "awaiting_user")
			run["review"] = bundle
			run["blocked_reason"] = "review_required"
		else:
			states.transition(run, "succeeded")
			states.advance(run)
			run["blocked_reason"] = ""
		slot.release_settled(store, run)
		return record.save()


def approve(store, intake_id, number, stage, digest, expected_revision):
	with store.locked(intake_id, number) as record:
		run = record.run
		bundle = run["review"]
		if (type(expected_revision) is not int or expected_revision != run["revision"]
				or type(stage) is not str or stage not in states.APPROVAL_SCOPES
				or run["stage"] != stage or run["status"] != "awaiting_user" or bundle is None
				or type(digest) is not str or digest != bundle["digest"]
				or digest != states.manifest(run["artifacts"], states.APPROVAL_SCOPES[stage])):
			raise Conflict("확인한 산출물 묶음이나 기록 판이 달라졌습니다. 다시 확인해 주세요")
		states.require_settled(run)
		run["approvals"][stage] = {"intake_id": intake_id, "round": number, "digest": digest,
			"revision": expected_revision, "approved_at": schema.now(), "source": "explicit"}
		states.transition(run, "waiting")
		run["stage"] = states.STAGE_TRANSITIONS[stage]
		run["blocked_reason"] = ""
		return record.save()


def request_changes(store, intake_id, number, stage):
	with store.locked(intake_id, number) as record:
		run = record.run
		if run["status"] != "awaiting_user" or run["stage"] != stage:
			raise TransitionError("확인 대기 중인 단계에만 수정을 요청할 수 있습니다")
		states.transition(run, "waiting")
		run["approvals"][stage] = None
		run["blocked_reason"] = ""
		return record.save()


def invalidate(run, kind):
	"""산출물 등록과 같은 잠금·저장 안에서 호출한다."""
	rollback = None
	if kind == "script" and any(run["approvals"].values()):
		run["approvals"] = {"script": None, "preview": None}
		rollback = "script"
	elif kind in states.APPROVAL_SCOPES["preview"] and run["approvals"]["preview"] is not None:
		run["approvals"]["preview"] = None
		rollback = "preview"
	if rollback:
		states.require_settled(run)
		if run["status"] in ("cancelled", "cancelling", "needs_check"):
			raise TransitionError("종료 또는 결과 대조가 필요한 회차는 수정할 수 없습니다")
		run["stage"], run["status"] = rollback, "awaiting_user"
		run["blocked_reason"] = "review_required"
	if run["status"] == "awaiting_user" and kind in states.APPROVAL_SCOPES[run["stage"]]:
		run["review"] = states.review(run, run["stage"])
