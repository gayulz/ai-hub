"""호출 전 의도 기록과 명시적인 결과 대조. 실제 호출과 자동 재시도는 없다."""

import copy
import os
import uuid

from . import schema, slot, states
from .errors import Conflict, TransitionError


def find(run, attempt_id):
	schema.matching(attempt_id, schema.ATTEMPT_ID)
	for row in run["attempts"]:
		if row["attempt_id"] == attempt_id:
			return row
	raise Conflict("해당 시도 기록이 없습니다")


def intend(store, intake_id, number, kind, input_digest):
	schema.matching(input_digest, schema.DIGEST)
	check_owner(store)
	with store.locked(intake_id, number) as record:
		run = record.run
		schema.token(kind, states.OPERATIONS[run["stage"]])
		if run["status"] != "running":
			raise TransitionError("실행 중인 제작 단계에서만 호출 의도를 기록할 수 있습니다")
		states.require_settled(run)
		stamp = schema.now()
		row = {"attempt_id": "a-" + uuid.uuid4().hex, "kind": kind, "stage": run["stage"],
			"input_digest": input_digest, "status": "intended", "external_id": None, "result_digest": None,
			"error_code": "", "created": stamp, "updated": stamp, "resolution": None}
		value = slot.acquire(store, intake_id, number, row["attempt_id"])
		run["attempts"].append(row)
		try:
			record.save()
		except Exception:
			# 아직 호출 권한을 반환하지 않았으므로 이 경로에서는 외부 작업이 시작되지 않았다.
			slot.release(store, value, confirmed_end=True)
			raise
		return copy.deepcopy(row)


def mark_running(store, intake_id, number, attempt_id, *, external_id=None):
	if external_id is not None:
		schema.text(external_id, 200)
	with store.locked(intake_id, number) as record:
		row = find(record.run, attempt_id)
		value = slot.owner(store)
		if (record.run["status"] != "running" or row["status"] != "intended"
				or not slot.for_run(value, record.run) or value["attempt_id"] != attempt_id):
			raise TransitionError("호출 의도와 실행 슬롯이 일치해야 합니다")
		row.update(status="running", external_id=external_id, updated=schema.now())
		return record.save()


def finish(store, intake_id, number, attempt_id, status, *, confirmed_end=False,
		result_digest=None, error_code=""):
	if confirmed_end is not True:
		raise TransitionError("실제 작업과 하위 프로세스의 종료 확인이 필요합니다")
	schema.token(status, ("succeeded", "failed", "cancelled"))
	schema.token(error_code, states.ERROR_CODES)
	if result_digest is not None:
		schema.matching(result_digest, schema.DIGEST)
	with store.locked(intake_id, number) as record:
		row = find(record.run, attempt_id)
		if row["status"] != "running":
			raise TransitionError("실행 중인 시도만 종료 기록을 남길 수 있습니다")
		value = slot.owner(store)
		if not slot.for_run(value, record.run) or value["attempt_id"] != attempt_id:
			raise Conflict("시도에 해당하는 실행 슬롯이 없습니다", owner=value)
		row.update(status=status, result_digest=result_digest, error_code=error_code, updated=schema.now())
		result = record.save()
		slot.release(store, value, confirmed_end=True)
		return result


def recover(store, intake_id, number, *, reason="interrupted"):
	schema.token(reason, ("interrupted", "owner_lost", "unknown_delivery"))
	with store.locked(intake_id, number) as record:
		run, changed = record.run, False
		value = slot.owner(store)
		for row in run["attempts"]:
			owned = slot.for_run(value, run) and row["attempt_id"] == value["attempt_id"]
			if row["status"] in ("intended", "running") or (reason == "owner_lost" and owned):
				row.update(status="needs_check", error_code=reason, updated=schema.now(), resolution=None)
				changed = True
		orphan = slot.for_run(value, run) and not any(row["attempt_id"] == value["attempt_id"] for row in run["attempts"])
		if changed or orphan:
			run["status"], run["review"] = "needs_check", None
			run["blocked_reason"] = "unknown_attempt" if orphan else ("owner_missing" if reason == "owner_lost" else "attempt_uncertain")
			return record.save()
		return copy.deepcopy(run)


def check_owner(store):
	value = slot.owner(store)
	if value is None:
		return None
	try:
		os.kill(value["pid"], 0)
	except ProcessLookupError:
		recover(store, value["intake_id"], value["round"], reason="owner_lost")
	except PermissionError:
		pass
	return value


def resolve(store, intake_id, number, attempt_id, result, note):
	schema.matching(attempt_id, schema.ATTEMPT_ID)
	schema.token(result, schema.RESOLUTIONS)
	schema.text(note, 500)
	with store.locked(intake_id, number) as record:
		run = record.run
		value = slot.owner(store)
		matching_owner = slot.for_run(value, run) and value["attempt_id"] == attempt_id
		row = next((a for a in run["attempts"] if a["attempt_id"] == attempt_id), None)
		if row is None:
			# 슬롯 예약 직후, 의도 기록을 저장하기 전에 중단된 경우도 자동 반환하지 않는다.
			if not matching_owner or run["status"] != "needs_check" or result != "not_started":
				raise Conflict("대조할 시도 기록이 없습니다")
		else:
			if row["status"] != "needs_check":
				raise TransitionError("결과 미상인 시도에만 명시적인 대조를 기록할 수 있습니다")
			row.update(status=schema.RESOLUTIONS[result], updated=schema.now(),
				error_code="" if result == "ended_ok" else ("not_started" if result == "not_started" else "tool_failed"),
				resolution={"result": result, "note": note, "resolved_at": schema.now()})
		if not states.unsettled(run):
			states.transition(run, "waiting")
			run["blocked_reason"] = ""
		result_run = record.save()
		if matching_owner:
			slot.release(store, value, confirmed_end=True)
		return result_run


def request_cancel(store, intake_id, number):
	with store.locked(intake_id, number) as record:
		states.transition(record.run, "cancelling")
		record.run["blocked_reason"] = "cancel_pending"
		return record.save()


def confirm_cancelled(store, intake_id, number, *, confirmed_end=False):
	if confirmed_end is not True:
		raise TransitionError("실제 작업과 하위 프로세스의 종료 확인이 필요합니다")
	with store.locked(intake_id, number) as record:
		run = record.run
		states.transition(run, "cancelled", confirmed_end=True)
		for row in run["attempts"]:
			if row["status"] in states.UNSETTLED:
				row.update(status="cancelled", error_code="cancelled", updated=schema.now(), resolution=None)
		run["blocked_reason"] = ""
		value = slot.owner(store)
		result = record.save()
		if slot.for_run(value, run):
			slot.release(store, value, confirmed_end=True)
		return result


def consume_auto_fix(store, intake_id, number, review_key):
	schema.text(review_key, 160)
	with store.locked(intake_id, number) as record:
		if record.run["review_items"].get(review_key, 0) >= 1:
			raise Conflict("같은 장면·결함의 자동 수정은 회차당 한 번만 허용합니다")
		record.run["review_items"][review_key] = 1
		return record.save()
