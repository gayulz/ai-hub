"""launchd 조회·예약 시각·로그 마지막 줄의 결과와 건수."""

from __future__ import annotations

import getpass
import os
import plistlib
import re
import subprocess
from datetime import datetime, time, timedelta
from pathlib import Path
from xml.parsers.expat import ExpatError

from .common import normal, regular, stamp, unavailable

JOBS = (("collector", "수집기", "collector.out.log"), ("vaultsync", "자동 기록", "vaultsync.log"),
	("backup", "백업", "hubbackup.log"))
FIELDS = {"Month": (1, 12), "Day": (1, 31), "Weekday": (0, 7), "Hour": (0, 23), "Minute": (0, 59)}


def next_run(interval, now: datetime) -> str:
	intervals = interval if isinstance(interval, list) else [interval]
	if not intervals:
		raise ValueError("예약 없음")
	candidates = []
	for rule in intervals:
		if not isinstance(rule, dict) or not rule or rule.keys() - FIELDS.keys():
			raise ValueError("예약 형식 오류")
		for field, value in rule.items():
			low, high = FIELDS[field]
			if type(value) is not int or not low <= value <= high:
				raise ValueError("예약 값 오류")
		for offset in range(366 * 8):
			day = now.date() + timedelta(days=offset)
			if any(field in rule and rule[field] != value for field, value in (("Month", day.month), ("Day", day.day))):
				continue
			if "Weekday" in rule and rule["Weekday"] % 7 != (day.weekday() + 1) % 7:
				continue
			for hour in [rule["Hour"]] if "Hour" in rule else range(24):
				for minute in [rule["Minute"]] if "Minute" in rule else range(60):
					candidate = datetime.combine(day, time(hour, minute)).astimezone()
					if candidate > now:
						candidates.append(candidate)
			if candidates and candidates[-1].date() == day:
				break
	if not candidates:
		raise ValueError("다음 예약 없음")
	return min(candidates).isoformat(timespec="seconds")


def schedule(path: Path, user_home: Path, now: datetime) -> dict:
	try:
		with regular(path, user_home).open("rb") as source:
			data = plistlib.load(source)
		return normal(next_run=next_run(data["StartCalendarInterval"], now))
	except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException, ExpatError):
		return unavailable("plist 예약이 없거나 형식이 잘못됐습니다", next_run=None)


def last_log(path: Path, root: Path) -> dict:
	try:
		with regular(path, root).open("rb") as source:
			source.seek(0, 2)
			source.seek(max(0, source.tell() - 8192))
			lines = source.read().decode("utf-8").splitlines()
		line = next(line for line in reversed(lines) if line.strip())
		match = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)", line)
		at = stamp(match[1]) if match else datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
		# 로그의 자유 문장·경로·이름은 내보내지 않고 결과와 건수만 옮긴다.
		counts = re.findall(r"(기록|준비|보류|반영|성공|실패|수집|복사|변경|건너뜀|제외|백업)[ :=]+(\d+)(?:건|개)?", line)
		last = " · ".join(f"{name} {count}건" for name, count in counts)
		if not last:
			last = "실패 기록" if re.search(r"실패|오류|Error|Exception", line) else "마지막 줄 확인 · 상세 내용 생략"
		return normal(at=at, last_line=last, time_source="line" if match else "mtime")
	except (OSError, ValueError, StopIteration):
		return unavailable("로그가 없거나 마지막 줄을 읽을 수 없습니다", at=None, last_line="확인 불가")


def read_jobs(root: Path, user_home: Path, now: datetime) -> list[dict]:
	items = []
	for jid, name, logfile in JOBS:
		label = f"com.{getpass.getuser()}.ai-hub-{jid}"
		item = {"id": jid, "name": name, "state": None, "last_exit_code": None}
		try:
			result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
				capture_output=True, text=True, timeout=2)
			state = re.search(r"^\s*state = (running|not running|waiting|spawn scheduled)\s*$", result.stdout, re.M)
			if result.returncode or not state:
				raise ValueError
			code = re.search(r"^\s*last exit code = (-?\d+)\s*$", result.stdout, re.M)
			exit_code = int(code[1]) if code else None
			item.update(status="확인 필요" if exit_code not in (None, 0) else "일하는 중" if state[1] == "running" else "대기 중",
				state=state[1], last_exit_code=exit_code)
		except (OSError, ValueError, subprocess.TimeoutExpired):
			item.update(unavailable("예약 작업이 없거나 launchctl 조회에 실패했습니다"))
		item["schedule"] = schedule(user_home / "Library/LaunchAgents" / (label + ".plist"), user_home, now)
		item["log"] = last_log(root / "logs" / logfile, root)
		items.append(item)
	return items
