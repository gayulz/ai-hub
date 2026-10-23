"""키 내용·노트·AI 로그인 정보는 읽지 않는 점검표."""

from __future__ import annotations

import glob
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from collector.config import home, load_plans, load_sources
from hubbackup import backup, state, targets

from .config import Context, SetupError, command, context, git, read_config
from .templates import MODULES, matches, render


def config_paths(data, location: str = ""):
	if isinstance(data, dict):
		for key, value in data.items():
			yield from config_paths(value, location + "." + key if location else key)
	elif isinstance(data, list):
		for index, value in enumerate(data):
			yield from config_paths(value, f"{location}[{index}]")
	elif isinstance(data, str) and (data.startswith(("/", "~/")) or "/Users/" in data
			or location.rsplit(".", 1)[-1] in ("vault", "path", "glob", "attachments_backup")):
		yield location, data


def other_home(value: str) -> bool:
	return any(owner != Path.home().name and not glob.has_magic(owner)
		for owner in re.findall(r"/Users/([^/]+)", value))


def path_problems(data: dict) -> list[str]:
	paths = list(config_paths(data))
	for index, repo in enumerate(data.get("repo", [])):
		for directory in repo.get("doc_dirs", []):
			if not directory.startswith(("/", "~/")):
				paths.append((f"repo[{index}].doc_dirs", str(Path(repo["path"]).expanduser() / directory)))
	problems = []
	for field, value in paths:
		if other_home(value):
			problems.append(f"{field}: 다른 사용자 홈 경로")
			continue
		expanded = os.path.expanduser(value)
		if glob.has_magic(expanded):
			exists = next(glob.iglob(expanded), None) is not None
		else:
			exists = Path(expanded).exists()
		if not exists:
			problems.append(f"{field}: 경로 없음")
	return problems


def unlocked(ctx: Context) -> bool:
	result = git(ctx.vault, "rev-parse", "--path-format=absolute", "--git-path", "git-crypt/keys/default")
	if result.returncode or not result.stdout.strip():
		return False
	key = Path(result.stdout.strip())
	if key.is_symlink() or not key.is_file():
		return False
	for operation in ("clean", "smudge"):
		result = git(ctx.vault, "config", "--get", f"filter.git-crypt.{operation}")
		if result.returncode or "git-crypt" not in result.stdout or operation not in result.stdout:
			return False
	return git(ctx.vault, "config", "--bool", "--get", "filter.git-crypt.required").stdout.strip() == "true"


def check(label_prefix: str | None = None) -> int:
	rows = []

	def add(name: str, ok: bool, detail: str, warning: bool = False):
		rows.append((name, "통과" if ok else "경고" if warning else "실패", detail))

	for program in ("git", "git-crypt", "age", "age-keygen", "claude", "codex", "gemini", "agy"):
		found = shutil.which(program) is not None
		add(f"프로그램: {program}", found, "설치됨" if found else "명령 없음", program in ("claude", "codex", "gemini", "agy"))
	add("Python 3.14 이상", sys.version_info >= (3, 14), f"{sys.version_info.major}.{sys.version_info.minor}")
	for name, loader in (("sources.toml", load_sources), ("plans.toml", load_plans)):
		try:
			data = read_config(name)
			loader()
			add(f"설정: {name}", True, "읽기·형식 확인")
			problems = path_problems(data)
			add(f"설정 경로: {name}", not problems, "; ".join(problems) or "경로 존재·다른 사용자 홈 없음")
		except Exception:
			add(f"설정: {name}", False, "파일 읽기·TOML 형식·필수 항목 확인 필요 (값 생략)")
	for name in ("vault-git-crypt.key", "backup.agekey"):
		try:
			info = (home() / "keys" / name).lstat()
			ok = stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
			add(f"키: {name}", ok, f"권한 {oct(stat.S_IMODE(info.st_mode))}, 일반 파일·0o600 필요")
		except OSError:
			add(f"키: {name}", False, "파일 없음 또는 접근 불가")
	try:
		ctx = context(label_prefix)
	except Exception:
		ctx = None
	add("볼트 Git", ctx is not None, "최상위 경로 확인" if ctx else "설정·Git 저장소 확인 필요")
	add("볼트 git-crypt 잠금 해제", ctx is not None and unlocked(ctx), "내부 키의 존재와 필수 clean·smudge 설정 확인 (키 내용 미열람)")
	for key in ("user.name", "user.email"):
		ok = ctx is not None and bool(git(ctx.vault, "config", "--local", "--get", key).stdout.strip())
		add(f"볼트 작성자: {key}", ok, "로컬 설정 있음" if ok else "로컬 설정 없음 (값 생략)")
	if ctx is not None:
		for item in render(ctx):
			try:
				ok = matches(item)
			except (OSError, UnicodeError, SetupError):
				ok = False
			add(f"설치물: {item.path.name}", ok, "내용·권한 일치" if ok else "내용·권한·경로 확인 필요")
		for module in MODULES:
			label = f"{ctx.label_prefix}.ai-hub-{module}"
			ok = command(["launchctl", "print", f"{ctx.domain}/{label}"]).returncode == 0
			add(f"예약 등록: {module}", ok, "등록됨" if ok else "미등록 또는 launchctl 접근 실패")
		try:
			extra = sorted(path.name for path in ctx.hooks.iterdir()
				if not path.name.endswith(".sample") and path.name not in ("commit-msg", "pre-push")
				and (path.is_file() or path.is_symlink()))
			add("템플릿에 없는 훅", not extra, ", ".join(extra) if extra else "없음", warning=True)
		except OSError:
			add("템플릿에 없는 훅", False, "훅 폴더에 접근 불가")
	else:
		add("설치물·예약·추가 훅", False, "볼트 Git 경로를 고친 뒤 다시 점검하세요.")
	destination = targets.backup_dir()
	try:
		with os.scandir(destination):
			pass
		accessible = os.access(destination, os.R_OK | os.W_OK | os.X_OK)
	except OSError:
		accessible = False
	add("백업 iCloud 사본 폴더", accessible, "목록 접근·쓰기 권한 확인" if accessible else "폴더 없음 또는 접근 불가")
	try:
		value = state.load(home() / "backup-state.json")
		stamp = datetime.fromisoformat(value["last_backup"])
		if stamp.tzinfo is None:
			stamp = stamp.astimezone()
		age = datetime.now(timezone.utc) - stamp
		ok = 0 <= age.total_seconds() <= 36 * 3600 and value["last_failed"] == 0
		add("마지막 백업: 36시간 이내", ok, backup.status())
	except Exception:
		add("마지막 백업: 36시간 이내", False, "hubbackup status 기록 없음·시각 오류·읽기 실패")
	print("| 점검 항목 | 결과 | 안내 |")
	print("| --- | --- | --- |")
	for row in rows:
		print("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in row) + " |")
	return int(any(row[1] == "실패" for row in rows))
