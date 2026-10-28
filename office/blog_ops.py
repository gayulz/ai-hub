"""사무실의 확인된 동작만 로컬 블로그 운영 창구로 전달한다."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from collector.config import ConfigError

from . import signals

PORT = 8765
TIMEOUT = 3
MAX_RESPONSE = 1024 * 1024
LABELS = {"blog_state": "블로그 상태", "blog_stage_retry": "임시저장 재시도",
	"blog_redraft": "초안 다시 쓰기", "blog_clip_scan": "클립 스캔"}
ACTIONS = tuple(LABELS)
RESULTS = ("조회 완료", "접수", "거부", "실패", "결과 불명")
KINDS = frozenset(("GENERATE", "STAGE", "CLIP_SCAN", "CLIP_REPLY", "CLIP_FOLLOW",
	"DEVLOG_SOURCES", "DEVLOG_CARD", "DEVLOG_DRAFT", "DEVLOG_IMAGES", "NX_TISTORY_UPDATE",
	"NX_TABLE_PREP", "NX_SYNC", "STORE_SOURCES", "STORES", "NEWS_AM", "NEWS_PM", "PURGE",
	"COLLECT", "BLOG_RANK", "SPORTS_SYNC", "NEIGHBOR_SYNC", "NEIGHBOR_VISIT",
	"OUTREACH_ENRICH", "OUTREACH", "PROSPECT_COLLECT", "AI_TRACK", "TISTORY_POST",
	"NAVER_TRANSFER", "UNKNOWN"))
STATES = ("PLANNED", "AWAITING_EXPERIENCE", "DRAFTING", "GATE_CHECK", "POLISHING",
	"REVIEWING", "RENDERED", "STAGING", "STAGED", "PUBLISHED", "ARCHIVED", "FAILED")


def token(root: Path) -> str:
	try:
		config = signals.sources(root)
	except (OSError, ValueError, KeyError, TypeError, ConfigError):
		config = None
	matches = [repo for repo in config.repos if Path(repo.path).name == "naver-blog-agent"] if config else []
	if len(matches) > 1 or any(repo.cls != "personal" for repo in matches):
		raise ValueError("블로그 원천 설정 오류")
	blog = Path(matches[0].path) if matches else Path.home() / "workspace/naver-blog-agent"
	folder = blog / ".blogops"
	if not folder.is_absolute() or ".." in folder.parts:
		raise ValueError("블로그 경로 오류")
	flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
	directory = os.open(folder.anchor, flags)
	try:
		for part in folder.parts[1:]:
			next_dir = os.open(part, flags, dir_fd=directory)
			os.close(directory)
			directory = next_dir
		info = os.fstat(directory)
		if info.st_uid != os.getuid() or info.st_mode & 0o022:
			raise ValueError("블로그 기록 폴더 권한 오류")
		fd = os.open("ops-token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
		try:
			info = os.fstat(fd)
			if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
					or stat.S_IMODE(info.st_mode) != 0o600):
				raise ValueError("운영 인증 파일 권한 오류")
			value = os.read(fd, 129)
			current = os.stat("ops-token", dir_fd=directory, follow_symlinks=False)
			if ((info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
					or stat.S_IMODE(current.st_mode) != 0o600 or not re.fullmatch(rb"[A-Za-z0-9_-]{43}", value)):
				raise ValueError("운영 인증 파일 형식 오류")
			return value.decode("ascii")
		finally:
			os.close(fd)
	finally:
		os.close(directory)


def number(value) -> int:
	if type(value) is not int or value < 0 or value > 2**63 - 1:
		raise ValueError("상태 건수 오류")
	return value


def timestamp(value):
	if value is None:
		return None
	if not isinstance(value, str) or len(value) > 40:
		raise ValueError("상태 시각 오류")
	return datetime.fromisoformat(value).isoformat()


def state(data) -> dict:
	if not isinstance(data, dict) or not isinstance(data.get("running_jobs"), list) or not isinstance(data.get("recent_failures"), list):
		raise ValueError("상태 형식 오류")
	if len(data["running_jobs"]) > 2048 or len(data["recent_failures"]) > 10:
		raise ValueError("상태 건수 오류")
	def kind(value):
		return value if isinstance(value, str) and value in KINDS else "UNKNOWN"
	running = [{"id": number(row["id"]), "kind": kind(row["kind"]), "started_at": timestamp(row["started_at"])}
		for row in data["running_jobs"]]
	failures = [{"kind": kind(row["kind"]), "finished_at": timestamp(row["finished_at"])}
		for row in data["recent_failures"]]
	circuits = {}
	for name in ("STAGE", "GENERATE"):
		row = data["circuits"][name]
		if type(row["open"]) is not bool:
			raise ValueError("서킷 상태 오류")
		circuits[name] = {"open": row["open"], "consecutive_failures": number(row["consecutive_failures"])}
	counts = {name: number(data["post_counts"][name]) for name in STATES}
	return {"running_jobs": running, "recent_failures": failures, "circuits": circuits, "post_counts": counts}


def object_pairs(pairs):
	value = {}
	for key, item in pairs:
		if key in value:
			raise ValueError("중복 응답 항목")
		value[key] = item
	return value


def request(action: str, identity: str, secret: str):
	body = None
	if action != "blog_state":
		field = "kind" if action == "blog_clip_scan" else "slug"
		body = json.dumps({"action": action[5:], field: identity}, ensure_ascii=True).encode("utf-8")
	headers = {"X-Ops-Token": secret, "Connection": "close"}
	if body is not None:
		headers["Content-Type"] = "application/json"
	connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=TIMEOUT)
	try:
		connection.request("GET" if body is None else "POST", "/api/ops/state" if body is None else "/api/ops", body, headers)
		response = connection.getresponse()
		raw = response.read(MAX_RESPONSE + 1)
		if len(raw) > MAX_RESPONSE:
			raise ValueError("응답 크기 오류")
		lengths = response.headers.get_all("Content-Length", [])
		if (len(lengths) > 1 or (lengths and
				(not re.fullmatch(r"[0-9]{1,10}", lengths[0]) or int(lengths[0]) != len(raw)))):
			raise ValueError("응답 길이 오류")
		return response.status, json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
	finally:
		connection.close()


def execute(root: Path, action: str, identity: str, audit) -> tuple[int, dict]:
	status, outcome = 503, "실패"
	message, extra = "블로그 운영 인증을 안전하게 확인할 수 없습니다.", {}
	try:
		secret = token(root)
	except (OSError, ValueError, TypeError, KeyError, ConfigError):
		pass
	else:
		try:
			code, data = request(action, identity, secret)
			if not isinstance(data, dict):
				raise ValueError("응답 형식 오류")
			if code == 200 and data.get("ok") is True:
				if action == "blog_state":
					extra["state"] = state(data["state"])
					status, outcome, message = 200, "조회 완료", "블로그 상태를 조회했습니다."
				else:
					job = number(data["job_id"])
					if not job:
						raise ValueError("작업 번호 오류")
					extra["job_id"] = job
					status, outcome, message = 200, "접수", "요청을 접수했습니다. 작업 완료 여부는 상태를 확인하세요."
			elif code in (400, 401, 403, 404, 409, 415) and data.get("ok") is False and "job_id" in data and data["job_id"] is None:
				status, outcome, message = 409, "거부", "블로그 운영 창구가 요청을 거부했습니다. 상태를 확인하세요."
			else:
				raise ValueError("처리 결과 불명")
		except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, RecursionError):
			status, outcome = 502, "결과 불명" if action != "blog_state" else "실패"
			message = "처리 결과를 확인할 수 없습니다. 다시 요청하기 전에 블로그 상태를 확인하세요."
	try:
		row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "result": outcome}
		audit.write(json.dumps(row, ensure_ascii=False) + "\n")
		audit.flush()
	except (OSError, ValueError, TypeError):
		return 500, {"ok": False, "message": "활동 기록을 저장하지 못했습니다. 블로그 상태를 확인하세요.",
			"output": "", "restart_needed": False, "result": "결과 불명"}
	return status, {"ok": outcome in ("조회 완료", "접수"), "message": message,
		"output": "", "restart_needed": False, "result": outcome, **extra}
