"""실행 기록: ~/.ai-hub/runs/<run_id>/ 의 run.json(현재 상태)과 events.jsonl(사건 순서).

재개해도 호출 수·시간·열린 지적이 이어지도록 모든 상태를 run.json 하나에 둔다.
호출 직전에 예약 기록을 먼저 남기므로, 중간에 끊긴 호출도 사용한 것으로 센다.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from collector.config import home

DEFAULT_BUDGET = {"calls": 8, "seconds": 1800, "fixes": 2}


def runs_dir() -> Path:
	return home() / "runs"


def now() -> str:
	return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id(day: date | None = None) -> str:
	"""YYYY-MM-DD-NNN. 폴더를 먼저 만들어 두 실행이 같은 번호를 받지 않게 한다."""
	day = day or datetime.now().date()
	runs_dir().mkdir(parents=True, exist_ok=True)
	for number in range(1, 1000):
		run_id = f"{day.isoformat()}-{number:03d}"
		try:
			(runs_dir() / run_id).mkdir()
			return run_id
		except FileExistsError:
			continue
	raise RuntimeError("오늘 실행 번호를 모두 썼습니다")


@dataclass
class Run:
	run_id: str
	request: dict
	repo: str
	kind: str = "code"  # code | analysis
	base_commit: str = ""
	worktree: str = ""
	branch: str = ""
	step: str = "prepare"  # prepare → implement → verify → review → (fix → verify → review) → done
	fixes: int = 0
	status: str = "작업 중"  # 작업 중·승인 필요·완료·실패·시간 초과·호출 한도 소진·사용자 입력 필요·취소
	current_action: str = ""
	agent: str = ""
	approval_pending: bool = False
	quota_observed: str = "미측정"
	handoff: dict = field(default_factory=dict)
	document: str = ""
	checks: list = field(default_factory=list)
	open_findings: list = field(default_factory=list)
	review_summary: str = ""
	calls: list = field(default_factory=list)
	budget: dict = field(default_factory=lambda: dict(DEFAULT_BUDGET))
	elapsed: float = 0.0
	started: str = field(default_factory=now)
	end_reason: str = ""

	@property
	def folder(self) -> Path:
		return runs_dir() / self.run_id

	def save(self) -> None:
		self.folder.mkdir(parents=True, exist_ok=True)
		tmp = self.folder / "run.json.tmp"
		tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
		os.replace(tmp, self.folder / "run.json")

	@classmethod
	def load(cls, run_id: str) -> "Run":
		data = json.loads((runs_dir() / run_id / "run.json").read_text(encoding="utf-8"))
		return cls(**data)

	def event(self, event_name: str, **data) -> None:
		# 사건 이름 칸은 "event" 로 둔다. data 의 kind 같은 칸과 부딪히지 않게
		line = json.dumps({"at": now(), **data, "event": event_name}, ensure_ascii=False)
		with open(self.folder / "events.jsonl", "a", encoding="utf-8") as fh:
			fh.write(line + "\n")

	def calls_left(self) -> int:
		return self.budget["calls"] - len(self.calls)

	def seconds_left(self) -> float:
		return self.budget["seconds"] - self.elapsed

	def settle_interrupted(self) -> None:
		"""끊긴 호출(결과 없음)은 사용한 것으로 남긴다. 0으로 돌려 다시 부르지 않는다."""
		for call in self.calls:
			if call.get("ok") is None:
				call.update(ok=False, error="중단됨(결과 미확인)")

	def finish(self, status: str, reason: str) -> None:
		self.status, self.end_reason, self.current_action = status, reason, ""
		self.approval_pending = status == "승인 필요"
		self.event("finish", status=status, reason=reason)
		self.save()
