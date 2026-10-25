"""디스크의 단일 실행 슬롯. 시간이나 PID만으로 반환하지 않는다."""

import os

from . import files, schema, states
from .errors import Conflict, StoreError, TransitionError

OWNER_KEYS = ("intake_id", "round", "attempt_id", "pid", "started")


def validate_owner(value):
	schema.exact(value, OWNER_KEYS)
	schema.matching(value["intake_id"], schema.ID)
	schema.integer(value["round"], 1, 999)
	schema.matching(value["attempt_id"], schema.ATTEMPT_ID)
	schema.integer(value["pid"], 1)
	schema.timestamp(value["started"])
	return value


def _owner(parent):
	if ".slot" not in os.listdir(parent):
		return None
	return validate_owner(files.read_json(parent, ".slot", maximum=2048))


def owner(store):
	try:
		with store.base() as parent, files.locked(parent, ".slot.lock", create=True):
			return _owner(parent)
	except OSError:
		raise StoreError("전역 실행 슬롯을 확인할 수 없습니다") from None


def acquire(store, intake_id, number, attempt_id):
	value = validate_owner({"intake_id": intake_id, "round": number, "attempt_id": attempt_id,
		"pid": os.getpid(), "started": schema.now()})
	try:
		with store.base() as parent, files.locked(parent, ".slot.lock", create=True):
			existing = _owner(parent)
			if existing is not None:
				raise Conflict("다른 제작 작업이 실행 슬롯을 사용하고 있습니다", owner=existing)
			files.write_new(parent, ".slot", files.encoded(value))
			return value
	except OSError:
		raise StoreError("전역 실행 슬롯을 안전하게 얻을 수 없습니다") from None


def release(store, expected_owner, *, confirmed_end=False):
	if confirmed_end is not True:
		raise TransitionError("실제 작업과 하위 프로세스의 종료 확인 뒤에만 슬롯을 반환합니다")
	validate_owner(expected_owner)
	try:
		with store.base() as parent, files.locked(parent, ".slot.lock", create=True):
			existing = _owner(parent)
			if existing != expected_owner:
				raise Conflict("실행 슬롯의 소유자가 달라졌습니다", owner=existing)
			os.unlink(".slot", dir_fd=parent)
			os.fsync(parent)
	except OSError:
		raise StoreError("전역 실행 슬롯을 안전하게 반환할 수 없습니다") from None


def for_run(value, run):
	return value is not None and (value["intake_id"], value["round"]) == (run["intake_id"], run["round"])


def release_settled(store, run):
	states.require_settled(run)
	value = owner(store)
	if for_run(value, run):
		if not any(row["attempt_id"] == value["attempt_id"] and row["status"] not in states.UNSETTLED
				for row in run["attempts"]):
			raise TransitionError("슬롯의 시도 기록과 실제 종료를 대조해야 합니다")
		release(store, value, confirmed_end=True)
