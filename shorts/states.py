"""파일 접근 없이 단계·전이·승인 범위를 정의한다."""

import hashlib
import json

from .errors import TransitionError

STAGES = ("intake", "prepare", "script", "voice", "scene_code", "preview", "final", "deliver")
STAGE_LABELS = dict(zip(STAGES, ("접수 검증", "제작 준비", "대본·장면", "음성·받아쓰기·박자",
	"장면 코드", "실제 프리뷰", "최종 영상", "완료·저장")))
STATUSES = ("waiting", "running", "awaiting_user", "blocked", "failed", "cancelling",
	"cancelled", "succeeded", "needs_check")
STATUS_LABELS = dict(zip(STATUSES, ("작업을 기다립니다", "작업 중입니다", "사용자 확인을 기다립니다",
	"진행이 막혔습니다", "작업에 실패했습니다", "종료 확인을 기다립니다", "취소가 확인됐습니다",
	"작업이 끝났습니다", "실제 실행 결과를 확인해야 합니다")))
TRANSITIONS = {
	"waiting": frozenset(("running", "blocked", "cancelling")),
	"running": frozenset(("succeeded", "awaiting_user", "blocked", "failed", "cancelling", "needs_check")),
	"awaiting_user": frozenset(("waiting", "cancelling")),
	"blocked": frozenset(("waiting", "cancelling", "needs_check")),
	"failed": frozenset(("waiting", "cancelling", "needs_check")),
	"cancelling": frozenset(("cancelled", "needs_check")),
	"cancelled": frozenset(),
	"succeeded": frozenset(("waiting",)),
	"needs_check": frozenset(("waiting", "cancelling")),
}
STAGE_TRANSITIONS = dict(zip(STAGES, (*STAGES[1:], None)))
APPROVAL_SCOPES = {"script": frozenset(("script",)),
	"preview": frozenset(("script", "voice", "scene_code", "preview"))}
ARTIFACT_KINDS = frozenset(("script", "voice", "scene_code", "preview", "final", "report"))
OPERATIONS = {
	"intake": frozenset(),
	"prepare": frozenset(("prepare_workspace", "check_environment")),
	"script": frozenset(),
	"voice": frozenset(("generate_voice", "transcribe", "analyze_beats", "probe_media")),
	"scene_code": frozenset(),
	"preview": frozenset(("render_preview", "capture_frames", "probe_media")),
	"final": frozenset(("render_final", "capture_frames", "probe_media")),
	"deliver": frozenset(("copy_outputs",)),
}
ATTEMPT_STATUSES = frozenset(("intended", "running", "succeeded", "failed", "cancelled", "needs_check"))
UNSETTLED = frozenset(("intended", "running", "needs_check"))
ERROR_CODES = frozenset(("", "tool_failed", "cancelled", "interrupted", "owner_lost",
	"unknown_delivery", "validation_failed", "not_started"))
BLOCKED_REASONS = {
	"": "", "review_required": "산출물 확인이 필요합니다", "attempt_uncertain": "외부 작업 결과가 미상입니다",
	"owner_missing": "실행 소유자가 사라져 결과 대조가 필요합니다", "slot_busy": "다른 제작 작업이 실행 중입니다",
	"stage_failed": "작업 실패 원인을 확인해 주세요", "environment_failed": "제작 환경을 확인해 주세요",
	"cancel_pending": "외부 작업과 하위 프로세스의 종료 확인이 필요합니다",
	"auto_fix_limit": "이 결함의 자동 수정 기회를 이미 사용했습니다",
	"unknown_attempt": "실행 슬롯과 시도 기록을 대조해 주세요",
}


def manifest(artifacts, kinds=None):
	pairs = sorted((row["name"], row["sha256"]) for row in artifacts if kinds is None or row["kind"] in kinds)
	return hashlib.sha256(json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def approval_valid(run, stage):
	approval = run["approvals"].get(stage)
	return bool(approval and approval["intake_id"] == run["intake_id"] and approval["round"] == run["round"]
		and approval["digest"] == manifest(run["artifacts"], APPROVAL_SCOPES[stage]))


def unsettled(run):
	return any(row["status"] in UNSETTLED for row in run["attempts"])


def require_settled(run):
	if unsettled(run):
		raise TransitionError("외부 작업의 종료 또는 결과 대조가 먼저 필요합니다")


def check_transition(before, after):
	if (type(before) is not str or type(after) is not str
			or before not in TRANSITIONS or after not in TRANSITIONS[before]):
		raise TransitionError("허용되지 않은 실행 상태 전이입니다")


def transition(run, status, *, confirmed_end=False):
	check_transition(run["status"], status)
	if status == "running":
		require_settled(run)
		if ((run["stage"] == "voice" and not approval_valid(run, "script"))
				or (run["stage"] == "final" and not approval_valid(run, "preview"))):
			raise TransitionError("현재 산출물에 대한 사용자 승인이 필요합니다")
	if status == "awaiting_user":
		if run["stage"] not in APPROVAL_SCOPES:
			raise TransitionError("이 단계에는 사용자 확인 지점이 없습니다")
		require_settled(run)
	if status == "succeeded":
		if run["stage"] in APPROVAL_SCOPES:
			raise TransitionError("확인 단계의 성공은 사용자 확인 대기로 기록해야 합니다")
		require_settled(run)
	if status == "cancelled" and confirmed_end is not True:
		raise TransitionError("외부 작업과 하위 프로세스의 종료 확인이 필요합니다")
	if run["status"] == "needs_check" and status == "waiting":
		require_settled(run)
	run["status"] = status
	if status != "awaiting_user":
		run["review"] = None


def advance(run):
	if run["status"] != "succeeded" or run["stage"] in APPROVAL_SCOPES:
		raise TransitionError("끝난 일반 단계만 다음 단계로 넘어갈 수 있습니다")
	following = STAGE_TRANSITIONS[run["stage"]]
	if following is not None:
		transition(run, "waiting")
		run["stage"] = following


def review(run, stage):
	if stage not in APPROVAL_SCOPES or not any(a["kind"] == stage for a in run["artifacts"]):
		raise TransitionError("확인할 산출물이 필요합니다")
	return {"kind": stage, "digest": manifest(run["artifacts"], APPROVAL_SCOPES[stage]),
		"artifacts": sorted(a["name"] for a in run["artifacts"] if a["kind"] in APPROVAL_SCOPES[stage])}
