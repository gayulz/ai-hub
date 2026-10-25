"""회차 JSON의 허용 키·자료형·길이와 내부 참조를 검사한다."""

import re
from datetime import datetime, timedelta, timezone

from . import states
from .errors import StoreError

MAX_RECORD_BYTES = 4 * 1024 * 1024
ID = re.compile(r"s-[0-9a-f]{32}\Z")
ATTEMPT_ID = re.compile(r"a-[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RUN_KEYS = {"version", "intake_id", "round", "revision", "stage", "status", "created", "updated",
	"approvals", "review", "artifacts", "attempts", "review_items", "blocked_reason"}
ATTEMPT_KEYS = {"attempt_id", "kind", "stage", "input_digest", "status", "external_id", "result_digest",
	"error_code", "created", "updated", "resolution"}
RESOLUTIONS = {"ended_ok": "succeeded", "ended_failed": "failed", "not_started": "cancelled"}
KST = timezone(timedelta(hours=9))


def now():
	return datetime.now(KST).isoformat(timespec="microseconds")


def require(condition):
	if not condition:
		raise StoreError("제작 기록의 키·형식·길이 또는 내부 참조가 올바르지 않습니다")


def exact(value, keys):
	require(type(value) is dict and set(value) == set(keys))


def text(value, maximum, *, empty=False):
	require(type(value) is str and (empty or bool(value.strip())) and len(value) <= maximum)
	require(not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))
	return value


def token(value, choices):
	text(value, 64, empty=True)
	require(value in choices)


def integer(value, low=0, high=2 ** 63 - 1):
	require(type(value) is int and low <= value <= high)


def matching(value, pattern):
	text(value, 160)
	require(pattern.fullmatch(value) is not None)
	return value


def filename(value):
	matching(value, NAME)
	require(".." not in value)
	return value


def timestamp(value, *, kst=True):
	text(value, 40)
	try:
		parsed = datetime.fromisoformat(value)
		require(parsed.utcoffset() is not None and (not kst or parsed.utcoffset() == timedelta(hours=9)))
	except (ValueError, OverflowError):
		raise StoreError("기록 시각의 형식이 올바르지 않습니다") from None
	return parsed


def rows(value):
	require(type(value) is list and len(value) <= 4096)


def validate_attempt(row):
	exact(row, ATTEMPT_KEYS)
	matching(row["attempt_id"], ATTEMPT_ID)
	token(row["stage"], states.STAGES)
	token(row["kind"], states.OPERATIONS[row["stage"]])
	matching(row["input_digest"], DIGEST)
	token(row["status"], states.ATTEMPT_STATUSES)
	if row["external_id"] is not None:
		text(row["external_id"], 200)
	if row["result_digest"] is not None:
		matching(row["result_digest"], DIGEST)
	token(row["error_code"], states.ERROR_CODES)
	require(timestamp(row["updated"]) >= timestamp(row["created"]))
	if row["status"] in ("intended", "running"):
		require(row["result_digest"] is None and row["error_code"] == "" and row["resolution"] is None)
	resolution = row["resolution"]
	if resolution is not None:
		exact(resolution, ("result", "note", "resolved_at"))
		token(resolution["result"], RESOLUTIONS)
		text(resolution["note"], 500)
		timestamp(resolution["resolved_at"])
		require(row["status"] == RESOLUTIONS[resolution["result"]])


def validate(run):
	exact(run, RUN_KEYS)
	integer(run["version"], 1, 1)
	matching(run["intake_id"], ID)
	integer(run["round"], 1, 999)
	integer(run["revision"], 1)
	token(run["stage"], states.STAGES)
	token(run["status"], states.STATUSES)
	require(timestamp(run["updated"]) >= timestamp(run["created"]))
	token(run["blocked_reason"], states.BLOCKED_REASONS)
	rows(run["artifacts"])
	names = set()
	for row in run["artifacts"]:
		exact(row, ("name", "kind", "sha256", "size", "registered_at"))
		filename(row["name"])
		require(row["name"] not in names)
		names.add(row["name"])
		token(row["kind"], states.ARTIFACT_KINDS)
		matching(row["sha256"], DIGEST)
		integer(row["size"])
		timestamp(row["registered_at"])
	exact(run["approvals"], states.APPROVAL_SCOPES)
	for stage, approval in run["approvals"].items():
		if approval is None:
			continue
		exact(approval, ("intake_id", "round", "digest", "revision", "approved_at", "source"))
		matching(approval["intake_id"], ID)
		integer(approval["round"], 1, 999)
		matching(approval["digest"], DIGEST)
		integer(approval["revision"], 1, run["revision"])
		timestamp(approval["approved_at"])
		token(approval["source"], ("explicit",))
		require(states.approval_valid(run, stage))
	if run["status"] == "running":
		if run["stage"] == "voice":
			require(states.approval_valid(run, "script"))
		if run["stage"] == "final":
			require(states.approval_valid(run, "preview"))
	if run["status"] == "succeeded":
		require(run["stage"] not in states.APPROVAL_SCOPES)
	review = run["review"]
	if review is not None:
		exact(review, ("kind", "digest", "artifacts"))
		token(review["kind"], states.APPROVAL_SCOPES)
		matching(review["digest"], DIGEST)
		rows(review["artifacts"])
		for name in review["artifacts"]:
			filename(name)
		require(run["status"] == "awaiting_user" and run["stage"] == review["kind"])
		try:
			require(review == states.review(run, review["kind"]))
		except states.TransitionError:
			raise StoreError("확인할 산출물 묶음이 올바르지 않습니다") from None
	require((run["status"] == "awaiting_user") == (review is not None))
	rows(run["attempts"])
	identities = set()
	for row in run["attempts"]:
		validate_attempt(row)
		require(row["attempt_id"] not in identities)
		identities.add(row["attempt_id"])
	require(sum(row["status"] in ("intended", "running") for row in run["attempts"]) <= 1)
	if run["status"] in ("awaiting_user", "succeeded", "cancelled"):
		require(not states.unsettled(run))
	require(type(run["review_items"]) is dict and len(run["review_items"]) <= 4096)
	for key, count in run["review_items"].items():
		text(key, 160)
		integer(count, 1, 1)
	return run
