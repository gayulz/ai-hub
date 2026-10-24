"""23개 역할의 자리·외형은 고정하고 해당 역할의 신호만 상태로 바꾼다."""

import re
from pathlib import Path

from collector.config import ConfigError

from . import signals
from .common import unavailable
from .scene import scene

ROLE_NAMES = {"plan": "기획", "backend": "백엔드", "frontend": "프론트", "design": "디자인"}


def build_people(root, user_home, now, runs, plans, ledger, jobs):
	people = [{**person, "status": "대기", "engine": "", "task": "다음 작업 대기", "updated": None}
		for person in scene()["people"]]
	by_name = {person["name"]: person for person in people}

	def update(name, signal):
		by_name[name].update({k: v for k, v in signal.items() if k in ("status", "engine", "task", "updated", "project", "duration")})
		if signal["status"] == "확인 불가":
			by_name[name]["task"] = "작업 상태를 확인할 수 없습니다."

	try:
		config = signals.sources(root)
		company = [repo.id for repo in config.repos if repo.cls == "company"]
		blog = next((Path(repo.path) for repo in config.repos if Path(repo.path).name == "naver-blog-agent"), user_home / "workspace/naver-blog-agent")
	except (OSError, ValueError, KeyError, TypeError, ConfigError):
		config, company, blog = None, [], user_home / "workspace/naver-blog-agent"
	claude, codex = (signals.presence(root, tool, now) for tool in ("claude", "codex"))
	if claude.get("cls") != "company":
		update("PM", {**claude, "task": "직접 대화 중" if claude["status"] == "작업 중" else "다음 대화 대기"})
	for index, name in enumerate(("회사 개발", "레거시 분석")):
		if config is None:
			update(name, unavailable("회사 프로젝트 ID 설정을 읽을 수 없습니다"))
		elif index < len(company):
			by_name[name]["project"] = company[index]
			matches = [p for p in (claude, codex) if p.get("cls") == "company" and p.get("project") == company[index]]
			if matches:
				update(name, max(matches, key=lambda p: (p["status"] == "작업 중", p.get("updated", ""))))
			elif any(p["status"] == "확인 불가" for p in (claude, codex)):
				update(name, unavailable("회사 대화 신호를 확인할 수 없습니다"))
	# 이관 QA는 연결할 실행 신호가 아직 없어 대기한다.
	process = signals.processes()
	active = signals.collecting(root, now)
	commands = process["commands"]
	backfill = any(re.search(r"\bpython[\d.]*\s+.*?-m\s+collector\s+backfill(?:\s|$)", cmd) for cmd in commands)
	local = ledger["active"]
	collector_job = next(job for job in jobs if job["id"] == "collector")
	if collector_job["state"] == "running" or backfill or active["active"] or local["items"]:
		update("기록", {"status": "작업 중", "engine": "로컬", "task": "대화 기록 정리", "updated": active.get("updated")})
	elif collector_job["last_exit_code"] not in (None, 0):
		update("기록", {"status": "오류", "engine": "로컬", "task": "수집기 종료 상태 확인"})
	elif any(item["status"] == "확인 불가" for item in (collector_job, process, active, local)):
		update("기록", unavailable("수집 신호를 확인할 수 없습니다"))
	admin = [job for job in jobs if job["id"] in ("vaultsync", "backup")]
	status = "오류" if any(j["last_exit_code"] not in (None, 0) for j in admin) else (
		"확인 불가" if any(j["status"] == "확인 불가" for j in admin) else "작업 중" if any(j["state"] == "running" for j in admin) else "대기")
	update("총무", {"status": status, "engine": "로컬", "task": "자동 기록·백업 상태 확인",
		"updated": max((j["log"]["at"] for j in admin if j["log"]["at"]), default=None)})
	if config is None:
		update("회사 기록", unavailable("회사 기록의 분류를 확인할 수 없습니다"))
	else:
		items = [item for item in local["items"] if item["project"] in company]
		if active.get("cls") == "company" and active["active"]:
			items.append({"project": active["project"]})
		if items:
			update("회사 기록", {"status": "작업 중", "engine": "로컬", "project": items[0]["project"], "updated": active.get("updated")})
		elif local["status"] == "확인 불가" or active["status"] == "확인 불가":
			update("회사 기록", unavailable("회사 수집 신호를 확인할 수 없습니다"))
	for item in runs["items"]:
		if not item["active"]:
			continue
		name = ROLE_NAMES.get(item.get("role", "backend"), "백엔드")
		if (item.get("call") or {}).get("role") == "review":
			name = "QA"
		if by_name[name]["status"] == "작업 중":
			continue
		if item["status"] == "확인 불가" or item.get("events", {}).get("status") == "확인 불가":
			update(name, unavailable("이 역할의 실행 기록을 읽을 수 없습니다"))
			continue
		call = item.get("call")
		if item["status"] != "작업 중" or not call:
			continue
		tool = plans["tools"].get(call["plan"])
		if tool is None:
			update(name, unavailable("실행 도구 설정을 읽을 수 없습니다"))
		else:
			update(name, {"status": "작업 중", "engine": signals.ENGINES[tool], "task": item["id"] + " · " + ("검토 중" if name == "QA" else "구현 중"), "updated": call.get("at")})
	if runs["status"] == "확인 불가":
		for name in (*ROLE_NAMES.values(), "QA"):
			update(name, unavailable("실행 기록 폴더를 읽을 수 없습니다"))
	for name, kinds in (("작가", ("DRAFT", "REVIEW", "FACTCHECK", "SUMMARY")), ("클립", ("CLIP_VISIT", "CLIP_REPLY")), ("이웃", ("OUTREACH",))):
		update(name, signals.blog_logs(blog / ".blogops/logs", kinds, now))
	if any(re.search(r"\bserve_outreach(?:\s|$)", cmd) for cmd in commands):
		update("이웃", {"status": "작업 중", "engine": "로컬", "task": "이웃 작업 처리"})
	elif process["status"] == "확인 불가" and by_name["이웃"]["status"] != "작업 중":
		update("이웃", unavailable("이웃 프로세스 신호를 읽을 수 없습니다"))
	if process["status"] == "확인 불가":
		update("분석", unavailable("NotebookLM 프로세스를 조회할 수 없습니다"))
	elif any(re.search(r"/bin/nlm(?:\s|$)", cmd) for cmd in commands):
		update("분석", {"status": "작업 중", "engine": "NotebookLM", "task": "자료 분석 중"})
	# 티스토리는 상주 데몬만으로 담당 작업을 구분할 수 없어 명시적인 작업 로그만 쓴다.
	update("티스토리", signals.tistory(blog, now))
	# 네이버 블로그 편집기 올리기·발행 전 확인·검색 순위 관측
	update("네이버", signals.naver(blog, now))
	shorts = signals.shorts(user_home, now)
	if shorts["status"] == "확인 불가":
		for name in ("PD", "대본", "편집", "음악", "썸네일"):
			update(name, shorts)
	else:
		if shorts["main"]:
			update("PD", {"status": "작업 중", "engine": "Codex", "task": "영상 프로젝트 대화 중", "updated": shorts["main"]})
		for name in ("대본", "편집", "음악", "썸네일"):
			if shorts["auxiliary"]:
				update(name, {"status": "작업 중", "engine": "Codex", "task": "보조 작업 중", "updated": shorts["auxiliary"]})
			elif name in ("편집", "음악"):
				update(name, {"status": "쉬는 중", "task": "다음 보조 작업 대기"})
	for person in people:
		if person["status"] == "작업 중" and person["updated"] is None:
			person["updated"] = now.isoformat(timespec="seconds")
		if person["department"] == "company":
			person.pop("task", None)
			person.setdefault("project", "")
			person.setdefault("duration", None)
	return people


def build_engines(user_home, now, runs, plans, jobs, local):
	items = []
	for tool in ("codex", "agy", "claude"):
		status = "가동 중" if tool in plans["tools"].values() else "설정 없음"
		if plans["status"] == "확인 불가":
			status = "확인 불가"
		elif any(plans["tools"].get(f["plan"]) == tool and signals.recent(f["at"], now) for f in runs.get("failures", [])):
			status = "오류"
		items.append({"id": tool, "name": signals.ENGINES[tool], "status": status})
	items.append({"id": "ollama", "name": "로컬 모델", "status": "가동 중" if "models" in local else "확인 불가" if local["status"] == "확인 불가" else "꺼짐"})
	items.append({"id": "notebooklm", "name": "NotebookLM", **signals.notebooklm(user_home)})
	items.extend({"id": j["id"], "name": j["name"], "status": "오류" if j["status"] == "확인 필요" else j["status"]} for j in jobs)
	return items


def rooms(people):
	result = []
	for room in scene()["rooms"]:
		members = [p for p in people if p["department"] == room["id"]]
		status = next((s for s in ("오류", "작업 중", "확인 불가", "대기", "쉬는 중") if any(p["status"] == s for p in members)), "대기")
		result.append({**room, "status": status, "people": [p["id"] for p in members]})
	return result
