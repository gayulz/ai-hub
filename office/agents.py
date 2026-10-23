"""실행 사건·파일 시각·로컬 프로세스의 상태만 읽는다."""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from urllib.error import URLError

from .common import identifier, normal, regular, stamp, unavailable

RUN_STATUSES = {"작업 중", "승인 필요", "완료", "실패", "시간 초과", "호출 한도 소진", "사용자 입력 필요", "취소"}
EVENTS = {"start": "실행 시작", "resume": "실행 재개", "call": "AI 호출", "call_failed": "AI 호출 실패",
	"prepare": "준비 완료", "implemented": "구현 완료", "verified": "기계 검사", "reviewed": "독립 검토", "finish": "실행 종료"}
NAMES = {"codex": "Codex", "agy": "AGY", "claude": "Claude", "gemini": "Gemini CLI", "gemma": "로컬 모델", "blog": "블로그 에이전트"}


def held_lock(folder: Path, root: Path) -> bool:
	try:
		path = regular(folder / ".lock", root)
	except FileNotFoundError:
		return False
	with path.open("rb") as source:
		try:
			fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			return True
		fcntl.flock(source, fcntl.LOCK_UN)
	return False


def read_runs(root: Path) -> dict:
	items, activity = [], []
	try:
		folder = root / "runs"
		if folder.is_symlink():
			raise ValueError
		folders = sorted(folder.iterdir(), key=lambda p: p.name, reverse=True)
	except (OSError, ValueError):
		return unavailable("실행 기록 폴더가 없거나 읽을 수 없습니다", items=[], activity=[])
	for folder in folders:
		if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{3}", folder.name):
			continue
		try:
			with regular(folder / "run.json", root).open(encoding="utf-8") as source:
				data = json.load(source)
			status = data["status"]
			if status not in RUN_STATUSES:
				raise ValueError
			item = {"id": folder.name, "status": status, "active": held_lock(folder, root)}
		except (OSError, ValueError, TypeError, KeyError):
			items.append(unavailable("실행 상태·잠금을 읽을 수 없습니다", id=folder.name, active=False))
			continue
		last_call = None
		try:
			with regular(folder / "events.jsonl", root).open(encoding="utf-8") as source:
				for line in source:
					if not line.strip():
						continue
					event = json.loads(line)
					name = event.get("event")
					if name not in EVENTS:
						continue
					at = stamp(event["at"])
					activity.append({"at": at, "text": f"{folder.name} · {EVENTS[name]}", "source": "run"})
					if name == "call":
						if event["role"] not in ("implement", "review"):
							raise ValueError
						last_call = {"role": event["role"], "plan": identifier(event["plan"])}
			item["events"] = normal()
		except (OSError, ValueError, TypeError, KeyError, AttributeError):
			item["events"] = unavailable("실행 사건 파일이 없거나 형식이 잘못됐습니다")
			last_call = None
		item["call"] = last_call
		items.append(item)
	return normal(items=items, activity=activity)


def orchestration(runs: dict, plans: dict) -> list[dict]:
	items = runs["items"]
	latest = f'{items[0]["id"]} · {items[0]["status"]}' if items else "최근 실행 없음"
	result = []
	for aid in ("codex", "agy", "claude"):
		status = "상태 미확인" if aid == "claude" else "대기 중"
		agent = {"id": aid, "status": status, "task": "오케스트레이션 역할 없음", "result": latest}
		if runs["status"] == "확인 불가" or plans["status"] == "확인 불가":
			agent.update(unavailable("실행 기록 또는 요금제 설정을 확인할 수 없습니다"))
		else:
			for item in items:
				if item["status"] == "확인 불가" or (item["active"] and item["events"]["status"] == "확인 불가"):
					agent.update(unavailable("일부 실행의 상태·사건을 확인할 수 없습니다"))
					continue
				call = item.get("call")
				if not (item["active"] and item["status"] == "작업 중" and call):
					continue
				tool = plans["tools"].get(call["plan"])
				if tool is None:
					agent.update(unavailable("실행 요금제에 대응하는 도구가 없습니다"))
				elif tool == aid:
					status = "구현 중" if call["role"] == "implement" else "검토 중"
					agent.update(status=status, task=f'{item["id"]} · {status}', run_id=item["id"])
					break
		result.append(agent)
	return result


def gemini(user_home: Path, now: datetime) -> dict:
	agent = {"id": "gemini", "result": "대화 내용은 읽지 않음"}
	try:
		root = user_home / ".gemini/tmp"
		if root.is_symlink() or root.parent.is_symlink() or not root.is_dir():
			raise ValueError
		times = [regular(path, user_home).stat().st_mtime for path in root.glob("*/chats/*")
			if not path.is_dir() and not any(part.startswith("🔐") for part in path.relative_to(root).parts)]
		modified = max(times)
		age = now.timestamp() - modified
		if age < 0:
			raise ValueError
		agent.update(status="일하는 중" if age <= 600 else "쉬는 중",
			task="최근 대화 파일 수정 시각 기준", last_seen=datetime.fromtimestamp(modified).astimezone().isoformat(timespec="seconds"))
	except (OSError, ValueError):
		agent.update(unavailable("대화 파일의 수정 시각을 확인할 수 없습니다"), task="수정 시각 확인 불가")
	return agent


class NoRedirect(HTTPRedirectHandler):
	def redirect_request(self, request, fp, code, msg, headers, newurl):
		return None


def ollama(active: dict) -> dict:
	agent = {"id": "gemma", "result": "로컬 모델 상태만 조회"}
	try:
		opener = build_opener(ProxyHandler({}), NoRedirect())
		with opener.open("http://127.0.0.1:11434/api/ps", timeout=1) as response:
			data = json.loads(response.read(1024 * 1024))
		models = [identifier(model.get("name", model.get("model"))) for model in data["models"]]
		if active["status"] == "확인 불가":
			agent.update(unavailable("로컬 호출 예약을 원장에서 확인할 수 없습니다"), task="호출 상태 확인 불가")
		elif active["items"]:
			item = active["items"][0]
			agent.update(status="집중 중", task=f'{item["project"]} · {item["position"]}/{item["total"]}건째 · 예약 {len(active["items"])}건')
		else:
			agent.update(status="대기 중", task="로컬 호출 예약 없음")
		agent["models"] = models
		agent["result"] = ", ".join(models) if models else "올라온 모델 없음"
	except (OSError, URLError, HTTPException):
		agent.update(status="꺼짐", reason="Ollama에 연결할 수 없습니다", task="Ollama 응답 없음")
	except (ValueError, KeyError, TypeError, AttributeError):
		agent.update(unavailable("Ollama 상태 응답의 형식을 확인할 수 없습니다"), task="모델 상태 확인 불가")
	return agent


def blog() -> dict:
	agent = {"id": "blog", "result": "상주 프로세스 유무만 조회"}
	try:
		result = subprocess.run(["pgrep", "-f", r"(^|[ /])blogops[.]daemon([ \t]|$)"], capture_output=True, text=True, timeout=2)
		if result.returncode not in (0, 1):
			raise ValueError
		agent.update(status="일하는 중" if result.returncode == 0 else "꺼짐", task="블로그 상주 프로세스 확인")
	except (OSError, ValueError, subprocess.TimeoutExpired):
		agent.update(unavailable("블로그 프로세스를 조회할 수 없습니다"), task="프로세스 확인 불가")
	return agent
