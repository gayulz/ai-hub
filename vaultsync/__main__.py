"""볼트 자동 기록.

사용:
	python3 -m vaultsync run              변경을 검사해 커밋·push하고 첨부를 iCloud에 복사한다
	python3 -m vaultsync run --dry-run    아무것도 바꾸지 않고 할 일만 출력한다

결과는 ~/.ai-hub/logs/vaultsync.log 에 남긴다(볼트 안에는 쓰지 않는다).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from collector.config import home, load_sources, run_dir

from . import mirror, sync


def backup_dir() -> Path | None:
	"""sources.toml 의 [vaultsync] attachments_backup. 없으면 첨부 복사를 하지 않는다."""
	with open(home() / "sources.toml", "rb") as fh:
		value = tomllib.load(fh).get("vaultsync", {}).get("attachments_backup")
	return Path(value).expanduser() if value else None


def log(line: str) -> None:
	path = home() / "logs" / "vaultsync.log"
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "a", encoding="utf-8") as fh:
		fh.write(f"{datetime.now().astimezone():%Y-%m-%dT%H:%M} {line}\n")


def notify(key: str, text: str) -> None:
	"""macOS 알림. 같은 문제는 하루 한 번만 띄운다. 알림 문구에 경로·값은 넣지 않는다."""
	state_path = run_dir() / "vaultsync-notified.json"
	today = datetime.now().date().isoformat()
	try:
		state = json.loads(state_path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		state = {}
	if state.get(key) == today:
		return
	state[key] = today
	state_path.parent.mkdir(parents=True, exist_ok=True)
	state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
	script = f'display notification "{text}" with title "ai-hub 볼트 자동 기록"'
	subprocess.run(["osascript", "-e", script], capture_output=True)


def report(result: sync.Result, copied: int | None) -> str:
	parts = [f"커밋 {result.commit or '없음'}({len(result.committed)}개)", f"뺀 파일 {len(result.excluded)}개"]
	parts.append({None: "push 할 것 없음", True: "push 성공", False: f"push 실패: {result.push_error}"}[result.pushed])
	parts.append("첨부 복사 안 함" if copied is None else f"첨부 복사 {copied}개")
	return " · ".join(parts)


def run(dry_run: bool) -> int:
	vault = load_sources().vault
	result = sync.plan(vault)
	if result.skipped_run:
		print(f"건너뜀: {result.skipped_run}")
		if not dry_run:
			log(f"건너뜀: {result.skipped_run}")
		return 0
	backup = backup_dir()
	if dry_run:
		print(f"커밋할 파일 {len(result.committed)}개")
		for path in result.committed:
			print(f"  + {path}")
		for path, reason in result.excluded:
			print(f"  뺌: {path} ({reason})")
		if backup is not None:
			todo = [p for p in mirror.ignored_files(vault) if mirror.wanted(vault, p)
				and not mirror.same(vault / p, backup / p)]
			print(f"iCloud에 복사할 첨부 {len(todo)}개")
		return 0
	if result.committed:
		sync.commit(vault, result, datetime.now().astimezone())
	sync.push(vault, result)
	copied = mirror.mirror(vault, backup) if backup is not None else None
	for path, reason in result.excluded:
		log(f"뺌: {path} ({reason})")
	log(report(result, copied))
	if result.excluded:
		notify("excluded:" + "|".join(sorted(p for p, _ in result.excluded)),
			f"파일 {len(result.excluded)}개를 커밋에서 뺐습니다. vaultsync.log 확인")
	if result.pushed is False:
		notify("push", "GitHub push에 실패했습니다. vaultsync.log 확인")
	return 0


def main(argv: list[str]) -> int:
	if argv[:1] != ["run"] or argv[1:] not in ([], ["--dry-run"]):
		print(__doc__, file=sys.stderr)
		return 2
	lock_path = run_dir() / "vaultsync.lock"
	lock_path.parent.mkdir(parents=True, exist_ok=True)
	with open(lock_path, "w") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			print("이미 실행 중입니다")
			return 0
		try:
			return run(dry_run=argv[1:] == ["--dry-run"])
		except Exception as exc:  # 예약 실행의 실패를 숨기지 않는다
			log(f"실패: {type(exc).__name__}: {exc}")
			notify("error", "볼트 자동 기록이 실패했습니다. vaultsync.log 확인")
			print(f"vaultsync 실패: {exc}", file=sys.stderr)
			return 1


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
