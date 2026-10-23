"""기존 설정을 재사용하고 설치에 필요한 경로만 계산한다."""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from collector.config import home, load_sources


class SetupError(Exception):
	"""설정 값이나 명령 출력을 노출하지 않는 안내."""


def command(args: list[str]) -> subprocess.CompletedProcess:
	try:
		return subprocess.run(args, capture_output=True, text=True, timeout=20)
	except (OSError, subprocess.TimeoutExpired):
		return subprocess.CompletedProcess(args, 1, "", "")


def git(vault: Path, *args: str) -> subprocess.CompletedProcess:
	return command(["git", "-C", str(vault), *args])


def read_config(name: str) -> dict:
	with (home() / name).open("rb") as incoming:
		return tomllib.load(incoming)


@dataclass(frozen=True)
class Context:
	user_home: Path
	hub_home: Path
	repo: Path
	vault: Path
	hooks: Path
	python: Path
	label_prefix: str
	uid: int

	@property
	def domain(self) -> str:
		return f"gui/{self.uid}"


def context(label_prefix: str | None = None) -> Context:
	prefix = label_prefix or "com." + getpass.getuser()
	if not re.fullmatch(r"[a-zA-Z0-9_.-]+", prefix):
		raise SetupError("launchd 라벨 접두어에는 영문·숫자·점·밑줄·하이픈만 쓸 수 있습니다.")
	vault = load_sources().vault.expanduser().absolute()
	top = git(vault, "rev-parse", "--show-toplevel")
	if top.returncode or Path(top.stdout.strip()).resolve() != vault.resolve():
		raise SetupError("vault는 볼트 Git 저장소의 최상위 폴더여야 합니다.")
	result = git(vault, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
	if result.returncode or not result.stdout.strip():
		raise SetupError("볼트 Git 저장소를 열 수 없습니다. sources.toml의 vault와 git 설치를 확인하세요.")
	return Context(Path.home().resolve(), home().expanduser().resolve(), Path(__file__).resolve().parents[1],
		vault, Path(result.stdout.strip()), Path(sys.executable), prefix, os.getuid())


def author(name: str | None, email: str | None) -> tuple[str, str] | None:
	settings = read_config("sources.toml").get("hubsetup", {})
	name = name if name is not None else settings.get("author_name")
	email = email if email is not None else settings.get("author_email")
	if name is None or email is None:
		return None
	if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\0" in value
			or "\r" in value for value in (name, email)):
		raise SetupError("볼트 작성자 이름·이메일은 비어 있지 않은 한 줄 문자열이어야 합니다.")
	return name, email
