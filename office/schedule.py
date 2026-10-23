"""예약·결정 일정과 기록된 후속 일을 한 일정으로 모은다."""

from pathlib import Path

from collector.config import ConfigError

from . import signals
from .common import normal, unavailable


def build_schedule(root, decisions, jobs, followups):
	blog_ids = set()
	try:
		blog_ids = {r.id for r in signals.sources(root).repos
			if Path(r.path).name in ("naver-blog-agent", "devlog-vault")}
	except (OSError, ValueError, KeyError, TypeError, ConfigError):
		pass
	items = [{**row, "source": "개인", "kind": "schedule"} for row in decisions["items"]]
	for job in jobs:
		at = job["schedule"]["next_run"]
		if at:
			items.append({"title": job["name"], "due": at[:10], "at": at, "source": "개인", "kind": "job"})
	items += [{**row, "source": "블로그" if row["project"] in blog_ids and row["source"] != "회사" else row["source"]}
		for row in followups["items"]]
	items.sort(key=lambda row: (row["due"] or "9999", row.get("at", ""), row["title"]))
	status = unavailable("일부 일정 출처를 읽을 수 없습니다") if any(s["status"] == "확인 불가"
		for s in (decisions, followups, *(job["schedule"] for job in jobs))) else normal()
	return {**status, "items": items}
