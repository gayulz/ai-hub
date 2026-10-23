"""허용된 로컬 자료를 읽기만 해서 화면용 JSON 한 덩어리를 만든다."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from collector.config import home

from . import agents
from .common import identifier, normal, read_toml, text, unavailable
from .jobs import read_jobs
from .ledger import read_ledger


def read_plans(root: Path) -> dict:
	try:
		data = read_toml(root / "plans.toml", root)
		tools = {}
		for plan in data["plans"]:
			pid, tool = identifier(plan["id"]), plan.get("type", plan.get("tool"))
			if pid in tools or tool not in ("codex", "agy", "claude", "ollama"):
				raise ValueError
			tools[pid] = tool
		result = normal(tools=tools)
		try:
			limit = data["backfill"]["daily_limit_calls"]
			if type(limit) is not int or limit < 0:
				raise ValueError
			result["limit"] = normal(daily_limit_calls=limit)
		except (ValueError, KeyError, TypeError):
			result["limit"] = unavailable("C4 하루 호출 한도 설정이 없습니다")
		return result
	except (OSError, ValueError, TypeError, KeyError, AttributeError):
		return unavailable("요금제 설정이 없거나 형식이 잘못됐습니다", tools={},
			limit=unavailable("C4 하루 호출 한도를 확인할 수 없습니다"))


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
	return {"decisions": {**group, "items": decisions}, "schedule": {**group, "items": schedule[:3]}}


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


def build_state(now: datetime | None = None) -> dict:
	now = (now or datetime.now().astimezone()).astimezone()
	root, user_home = home(), Path.home()
	plans = read_plans(root)
	ledger = read_ledger(root, now.date().isoformat())
	runs = agents.read_runs(root)
	decisions = read_decisions(root, now.date())
	jobs = read_jobs(root, user_home, now)
	people = agents.orchestration(runs, plans) + [agents.gemini(user_home, now), agents.ollama(ledger["active"]), agents.blog()]
	for person in people:
		aid, status = person["id"], person["status"]
		person["name"] = agents.NAMES[aid]
		person["room"] = "검토실" if status == "검토 중" else "집중 업무 존" if status == "집중 중" else (
			"비서실" if status in ("상태 미확인", "확인 불가") else "라운지" if status in ("쉬는 중", "꺼짐") else "개발실")
		if ledger["calls"]["status"] == "정상" and plans["status"] == "정상" and aid not in ("gemini", "blog"):
			tool = "ollama" if aid == "gemma" else aid
			person["calls"] = sum(row["count"] for row in ledger["calls"]["items"] if plans["tools"].get(row["plan"]) == tool)
		else:
			person["calls"] = None
	people[-1]["dashboard"] = dashboard(root)
	approvals = [{"id": item["id"], "status": item["status"], "reply": f'python3 -m orchestrator apply {item["id"]}'}
		for item in runs["items"] if item["status"] == "승인 필요"]
	run_errors = runs["status"] == "확인 불가" or any(item["status"] == "확인 불가" for item in runs["items"])
	run_group = unavailable("일부 실행의 반영 대기를 확인할 수 없습니다", items=approvals) if run_errors else normal(items=approvals)
	activity = runs["activity"] + [{"at": item["log"]["at"], "text": item["name"] + " · " + item["log"]["last_line"],
		"source": item["id"]} for item in jobs if item["log"]["at"]]
	activity.sort(key=lambda item: datetime.fromisoformat(item["at"]), reverse=True)
	return {"updated": now.isoformat(timespec="seconds"), "agents": people, "jobs": jobs,
		"approvals": {"c4": ledger["batches"], "runs": run_group, "decisions": decisions["decisions"]},
		"board": {"progress": ledger["progress"], "usage": {"c4": ledger["usage"], "limit": plans["limit"], "plans": ledger["calls"]},
			"schedule": {"decisions": decisions["schedule"], "jobs": [{"id": item["id"], "title": item["name"],
				**item["schedule"]} for item in jobs if item["schedule"]["next_run"] is None
				or datetime.fromisoformat(item["schedule"]["next_run"]).date() == now.date()]}}, "activity": activity[:20]}
