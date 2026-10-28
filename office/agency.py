"""사무실에서 대행사 질문과 납품 묶음을 확인하고 처리한다."""

import json
import os
import re
import unicodedata

from agency import delivery, intake, project
from collector.backfill_common import locked
from collector.config import ConfigError

from .common import action_id

ACTIONS = ("agency_answer", "agency_approve", "agency_reject")
KEYS = {"agency_answer": {"action", "id", "no", "text"},
	"agency_approve": {"action", "id", "hash"},
	"agency_reject": {"action", "id", "hash", "text"}}


def validate(payload):
	action = payload["action"]
	if set(payload) != KEYS[action]:
		raise ValueError("요청 항목이 잘못됐습니다")
	action_id(action, payload["id"])
	if action == "agency_answer" and (type(payload["no"]) is not int or not 1 <= payload["no"] <= 2147483647):
		raise ValueError("질문 번호가 잘못됐습니다")
	if "hash" in payload and (not isinstance(payload["hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["hash"])):
		raise ValueError("납품 묶음 hash가 잘못됐습니다")
	if "text" in payload:
		value = payload["text"]
		if (not isinstance(value, str) or not value.strip() or len(value) > 1000
				or any(unicodedata.category(char) in ("Cc", "Cs") for char in value)):
			raise ValueError("글은 제어 문자 없이 1~1000자여야 합니다")


def detail(identity, view):
	action_id("agency_answer", identity)
	row = project.load(identity)
	if view == "questions" and row["status"] == "접수 중":
		return {"id": identity, "title": row["title"], "questions": [
			{"no": item["no"], "question": item["question"]} for item in intake._questions(identity)["open"]]}
	if view != "delivery" or row["status"] != "납품 대기":
		raise ValueError("해당 상태의 프로젝트가 아닙니다")
	bundle = delivery.load_bundle(identity)
	tests = bundle["tests"]
	return {"id": identity, "title": row["title"], "hash": bundle["hash"],
		"criteria": [{"criterion": item["criterion"], "verdict": item["verdict"]} for item in bundle["criteria"]],
		"tests": {"ok": tests.get("ok") is True, "detail": str(tests.get("summary", tests.get("detail", "")))},
		"changed": len(bundle["changed"]), "actions": bundle["actions"], "restart_notice": bundle["restart_notice"]}


def reserve(root, identity, digest):
	folder = root / "run" / "agency-receipts"
	if folder.is_symlink():
		raise ValueError("처리 기록 폴더는 링크일 수 없습니다")
	folder.mkdir(mode=0o700, exist_ok=True)
	directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
	try:
		os.fchmod(directory, 0o700)
		fd = os.open(identity + "-" + digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
		try:
			os.fsync(fd)
		finally:
			os.close(fd)
		os.fsync(directory)
	finally:
		os.close(directory)


def execute(root, payload, audit):
	action, identity = payload["action"], payload["id"]
	status, outcome, response = 409, "거부", {"ok": False, "message": "상태가 바뀌었거나 이미 처리됐습니다. 다시 확인해 주세요."}
	try:
		row = project.load(identity)
		if action == "agency_answer":
			questions = intake._questions(identity)["open"] if row["status"] == "접수 중" else []
			question = next((item for item in questions if item["no"] == payload["no"]), None)
			if question is None or any(item.get("question") == question["question"] for item in row["answers"]):
				raise ValueError("열린 질문이 없거나 이미 답했습니다")
			updated = intake.answer(identity, payload["no"], payload["text"])
			status, outcome, response = 200, "완료", {"ok": True, "message": "답변을 저장했습니다.", "status": updated["status"]}
		else:
			if row["status"] != "납품 대기":
				raise ValueError("납품 상태 또는 hash가 달라졌습니다")
			bundle = delivery.load_bundle(identity)
			if bundle["hash"] != payload["hash"]:
				raise ValueError("납품 상태 또는 hash가 달라졌습니다")
			if action == "agency_approve":
				reserve(root, identity, payload["hash"])
				result = delivery.approve(identity, payload["hash"])
				ok = result["status"] == "완료"
				reason = project.load(identity).get("blocked_reason", "") if not ok else ""
				status, outcome = (200, "완료") if ok else (409, "실패")
				response = {"ok": ok, "message": "납품을 마쳤습니다." if ok else "납품에 실패했습니다.",
					"status": result["status"], "reason": reason, "pushed": result.get("pushed", False),
					"remote_matches": result.get("remote_matches", False), "restart_notice": result.get("restart_notice", "")}
			else:
				# shortcut: 반려 함수에 hash 인자가 없어 저장소 잠금으로 보완, 대행사에서 직접 재검증 필요.
				with locked("agency-repo:" + bundle["repo"]):
					if delivery.load_bundle(identity)["hash"] != payload["hash"]:
						raise ValueError("납품 묶음이 바뀌었습니다")
					reserve(root, identity, payload["hash"])
					result = delivery.reject(identity, payload["text"])
				status, outcome, response = 200, "완료", {"ok": True, "message": "납품을 반려했습니다.",
					"status": result["status"], "reason": result.get("blocked_reason", "")}
	except FileExistsError:
		response = {"ok": False, "message": "이미 처리했거나 결과가 불확실한 납품 묶음입니다. 프로젝트 상태를 확인하세요."}
	except (ValueError, ConfigError):
		pass
	except (OSError, RuntimeError, TypeError, KeyError):
		status, outcome = 500, "실패"
		response = {"ok": False, "message": "처리 결과를 확인하지 못했습니다. 다시 시도하기 전에 프로젝트 상태를 확인하세요."}
	try:
		audit.write(json.dumps({"action": action, "id": identity, "result": outcome}, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		return 500, {"ok": False, "message": "활동 기록을 저장하지 못했습니다. 다시 시도하기 전에 상태를 확인하세요."}
	return status, response
