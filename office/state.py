"""허용된 로컬 자료를 읽기만 해서 화면용 JSON 한 덩어리를 만든다."""

from __future__ import annotations

import socket
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from collector.config import ConfigError, home
from orchestrator import targets

from . import agents, chats, drafts, meetings, projects, roles, shorts, tasks, telegram_bridge
from .common import identifier, normal, read_toml, text, unavailable
from .jobs import read_jobs
from .ledger import read_action_activity, read_ledger, read_r1
from .schedule import build_schedule


def read_plans(root: Path) -> dict:
	try:
		data = read_toml(root / "plans.toml", root)
		tools = {}
		for plan in data["plans"]:
			pid, tool = identifier(plan["id"]), plan.get("type", plan.get("tool"))
			if pid in tools or tool not in ("codex", "agy", "claude", "ollama"):
				raise ValueError
			tools[pid] = tool
		result = normal(tools=tools, orchestration={"implementer": None, "reviewer": None})
		for role in result["orchestration"]:
			try:
				values = data.get("orchestration", {}).get(role, [])
				if isinstance(values, list) and values:
					pid = identifier(values[0])
					if pid in tools:
						result["orchestration"][role] = pid
			except (ValueError, TypeError, AttributeError):
				pass
		try:
			limit = data["backfill"]["daily_limit_calls"]
			if type(limit) is not int or limit < 0:
				raise ValueError
			result["limit"] = normal(daily_limit_calls=limit)
		except (ValueError, KeyError, TypeError):
			result["limit"] = unavailable("기록 정리 하루 호출 한도 설정이 없습니다")
		return result
	except (OSError, ValueError, TypeError, KeyError, AttributeError):
		return unavailable("요금제 설정이 없거나 형식이 잘못됐습니다", tools={},
			limit=unavailable("기록 정리 하루 호출 한도를 확인할 수 없습니다"))


def read_decisions(root: Path, today: date) -> dict:
	decisions, schedule = [], []
	try:
		data = read_toml(root / "decisions.toml", root)
		items = data.get("item", [])
		if not isinstance(items, list):
			raise ValueError
	except (OSError, ValueError):
		return {key: unavailable("결정 설정이 없거나 형식이 잘못됐습니다", items=[]) for key in ("decisions", "schedule")}
	broken = False
	for item in items:
		try:
			if item["status"] == "closed":
				continue
			if item["status"] != "open" or item["kind"] not in ("decision", "schedule"):
				raise ValueError
			due = item["due"]
			if due:
				date.fromisoformat(due)
			elif due != "":
				raise ValueError
			row = {"id": identifier(item["id"]), "title": text(item["title"]), "reply": text(item["reply"]), "due": due}
			if item["kind"] == "decision":
				decisions.append(row)
			elif due and due >= today.isoformat():
				schedule.append(row)
		except (ValueError, TypeError, KeyError):
			broken = True
	schedule.sort(key=lambda row: (row["due"], row["id"]))
	group = unavailable("일부 결정 항목의 형식을 확인할 수 없습니다") if broken else normal()
	return {"decisions": {**group, "items": decisions}, "schedule": {**group, "items": schedule}}


def dashboard(root: Path) -> dict:
	try:
		value = read_toml(root / "office.toml", root).get("blog", {}).get("dashboard_url")
		if value is None:
			return normal(url=None)
		url = urlsplit(value)
		if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
			raise ValueError
		return normal(url=value)
	except (OSError, ValueError, TypeError, AttributeError):
		return unavailable("대시보드 링크가 없거나 안전한 HTTP 주소가 아닙니다", url=None)


def blog_dashboard() -> str:
	try:
		with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
			return "켜짐"
	except Exception:
		return "꺼짐"


def build_state(now: datetime | None = None) -> dict:
	now = (now or datetime.now().astimezone()).astimezone()
	root, user_home = home(), Path.home()
	plans = read_plans(root)
	try:
		dev_targets = [{"id": item.id, "label": item.id} for item in targets.targets().values()]
	except (ConfigError, OSError, ValueError):
		dev_targets = [{"id": "ai-hub", "label": "ai-hub"}]
	ledger = read_ledger(root, now.date().isoformat())
	runs = agents.read_runs(root)
	decisions = read_decisions(root, now.date())
	task_state, personal_tasks, decisions["decisions"] = tasks.read_state(root, decisions["decisions"])
	jobs = read_jobs(root, user_home, now)
	people = agents.orchestration(runs, plans) + [{"id": "gemini", "result": "대화 내용은 읽지 않음"},
		agents.ollama(ledger["active"]), agents.blog()]
	for person in people:
		if (person["id"] in ("claude", "codex", "gemini") and "run_id" not in person
				and person.get("status") != "확인 불가"):
			person.update(agents.presence(root, person["id"], now))
		aid, status = person["id"], person["status"]
		person["name"] = agents.NAMES[aid]
		person["room"] = "검토실" if status == "검토 중" else "집중 업무 존" if status == "집중 중" else (
			"비서실" if status in ("상태 미확인", "확인 불가") else "라운지" if status in ("쉬는 중", "꺼짐") else "개발실")
		if ledger["calls"]["status"] == "정상" and plans["status"] == "정상" and aid not in ("gemini", "blog"):
			tool = "ollama" if aid == "gemma" else aid
			person["calls"] = sum(row["count"] for row in ledger["calls"]["items"] if plans["tools"].get(row["plan"]) == tool)
		else:
			person["calls"] = None
	by_id = {person["id"]: person for person in people}
	by_id["blog"]["dashboard"] = dashboard(root)
	approvals = [{"id": item["id"], "status": item["status"], "reply": f'python3 -m orchestrator apply {item["id"]}'}
		for item in runs["items"] if item["status"] == "승인 필요"]
	run_errors = runs["status"] == "확인 불가" or any(item["status"] == "확인 불가" for item in runs["items"])
	run_group = unavailable("일부 실행의 반영 대기를 확인할 수 없습니다", items=approvals) if run_errors else normal(items=approvals)
	activity = runs["activity"] + read_action_activity(root) + [{"at": item["log"]["at"], "text": item["name"] + " · " + item["log"]["last_line"],
		"source": item["id"]} for item in jobs if item["log"]["at"]]
	activity.sort(key=lambda item: datetime.fromisoformat(item["at"]), reverse=True)
	role_people = roles.build_people(root, user_home, now, runs, plans, ledger, jobs)
	engines = roles.build_engines(user_home, now, runs, plans, jobs, by_id["gemma"])
	usage = {"c4": ledger["usage"], "limit": plans["limit"], "plans": ledger["calls"],
		"notebooklm": {"status": "미측정", "count": None}}
	schedule = build_schedule(root, decisions["schedule"], jobs, ledger["followups"])
	meeting_state = meetings.read_state(root)
	chat_state, chat_questions = chats.snapshot(root)
	question_items = [{**item, "kind": "meeting"} for item in meeting_state["questions"]["items"]]
	question_items += [{**item, "kind": "chat"} for item in chat_questions]
	question_status = "확인 불가" if "확인 불가" in (meeting_state["questions"]["status"], chat_state["chats"]["status"]) else "정상"
	board_items = ledger["records"]["items"] + [{"kind": "meeting", "title": item["id"],
		"text": item.get("text") or "오케스트레이션 · " + item["status"], "at": item["at"]}
		for item in runs["items"] if item.get("public") and item.get("at")]
	board_items.sort(key=lambda item: item["at"], reverse=True)
	return {"updated": now.isoformat(timespec="seconds"), "agents": people, "jobs": jobs, "dev_targets": dev_targets,
		"projects": projects.read_projects(),
		"shorts": shorts.read_state(root), "tasks": task_state,
		**meeting_state, **chat_state, "questions": {**meeting_state["questions"], "status": question_status, "items": question_items},
		"blog_dashboard": blog_dashboard(), "telegram": telegram_bridge.status(),
		"dev_plans": plans.get("orchestration", {"implementer": None, "reviewer": None}),
		"people": role_people, "rooms": roles.rooms(role_people), "engines": engines, "usage": usage, "schedule": schedule,
		"approvals": {"c4": ledger["batches"], "runs": run_group, "r1": read_r1(root),
			"questions": {"status": question_status, "count": len(question_items)},
			"drafts": drafts.read_drafts(root), "decisions": decisions["decisions"], "tasks": personal_tasks},
		"board": {"items": board_items[:20], "status": "확인 불가" if runs["status"] == "확인 불가" else ledger["records"]["status"],
			"progress": ledger["progress"], "usage": usage,
			"schedule": {"decisions": {**decisions["schedule"], "items": decisions["schedule"]["items"][:3]}, "jobs": [{"id": item["id"], "title": item["name"],
				**item["schedule"]} for item in jobs if item["schedule"]["next_run"] is None
				or datetime.fromisoformat(item["schedule"]["next_run"]).date() == now.date()]}}, "activity": activity[:20]}
