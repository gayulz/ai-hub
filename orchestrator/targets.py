"""로컬 허용 목록의 개발 대상. 경로·명령·정책은 AI나 화면에서 받지 않는다."""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from collector.config import ConfigError, home

from . import checks

REPO = Path(__file__).resolve().parent.parent
ID = re.compile(r"[a-z][a-z0-9-]{0,30}")
KEYS = {"id", "path", "test_command", "commit_pattern", "forbidden", "restart_paths", "restart_notice", "claude_trailer"}


@dataclass(frozen=True)
class Target:
	id: str
	path: Path
	test_command: str = checks.DEFAULT_TEST
	commit_pattern: str | None = None
	forbidden: tuple[str, ...] = ()
	restart_paths: tuple[str, ...] = ()
	restart_notice: str = ""
	claude_trailer: str = ""


def _invalid(source: str, index: int | None = None, key: str | None = None) -> ConfigError:
	# 값을 예외에 넣지 않고, TOML로 읽히는 첫 줄에서 해당 칸의 위치만 찾는다.
	lines = source.splitlines(keepends=True)
	fallback = None
	for number in range(1, len(lines) + 1):
		try:
			data = tomllib.loads("".join(lines[:number]))
		except tomllib.TOMLDecodeError:
			continue
		if index is not None:
			rows = data.get("repo", [])
			if not isinstance(rows, list) or index >= len(rows):
				continue
			data = rows[index]
			if fallback is None:
				fallback = number
		if key is None or isinstance(data, dict) and key in data:
			return ConfigError(f"repos.toml:{number}: 대상 저장소 목록 형식 오류")
	return ConfigError(f"repos.toml:{fallback or 1}: 대상 저장소 목록 형식 오류")


def _string(value, minimum: int, maximum: int) -> bool:
	return (isinstance(value, str) and minimum <= len(value) <= maximum
		and (not minimum or bool(value.strip())) and "\x00" not in value)


def targets() -> dict[str, Target]:
	found = {"ai-hub": Target("ai-hub", REPO)}
	try:
		source = (home() / "repos.toml").read_text(encoding="utf-8")
	except FileNotFoundError:
		return found
	except (OSError, UnicodeError):
		raise ConfigError("repos.toml:1: 대상 저장소 목록을 읽을 수 없습니다") from None
	try:
		data = tomllib.loads(source)
	except tomllib.TOMLDecodeError as exc:
		line = re.search(r"\(at line (\d+),", str(exc))
		number = int(line[1]) if line else source.count("\n") + 1
		raise ConfigError(f"repos.toml:{number}: TOML 형식 오류") from None
	except RecursionError:
		raise ConfigError("repos.toml:1: TOML 중첩이 너무 깊습니다") from None
	if data.keys() - {"repo"}:
		raise _invalid(source, key=next(iter(data.keys() - {"repo"})))
	rows = data.get("repo", [])
	if not isinstance(rows, list):
		raise _invalid(source, key="repo")
	for index, row in enumerate(rows):
		if not isinstance(row, dict):
			raise _invalid(source, index)
		if row.keys() - KEYS:
			raise _invalid(source, index, next(iter(row.keys() - KEYS)))
		identity = row.get("id")
		if not isinstance(identity, str) or not ID.fullmatch(identity) or identity in found:
			raise _invalid(source, index, "id")
		value = row.get("path")
		if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
			raise _invalid(source, index, "path")
		path = Path(value)
		try:
			if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_dir():
				raise ValueError
			path = path.resolve()
			if path == REPO.resolve() or Path(checks.git(path, "rev-parse", "--show-toplevel").strip()) != path:
				raise ValueError
		except (OSError, RuntimeError, ValueError):
			raise _invalid(source, index, "path") from None
		command = row.get("test_command")
		if not _string(command, 1, 300):
			raise _invalid(source, index, "test_command")
		try:
			if not shlex.split(command):
				raise ValueError
		except ValueError:
			raise _invalid(source, index, "test_command") from None
		pattern = row.get("commit_pattern")
		if "commit_pattern" in row:
			try:
				if not isinstance(pattern, str):
					raise ValueError
				re.compile(pattern)
			except (ValueError, re.error, OverflowError, RecursionError):
				raise _invalid(source, index, "commit_pattern") from None
		for key in ("forbidden", "restart_paths"):
			values = row.get(key, [])
			if (not isinstance(values, list) or len(values) > 10
					or any(not isinstance(v, str) or "\x00" in v for v in values)
					or key == "forbidden" and any(len(v) > 300 for v in values)):
				raise _invalid(source, index, key)
		for prefix in row.get("restart_paths", []):
			if (not prefix.strip() or Path(prefix).is_absolute() or PureWindowsPath(prefix).drive
					or "\\" in prefix or ".." in prefix or prefix == "."):
				raise _invalid(source, index, "restart_paths")
		for key, minimum in (("restart_notice", 1), ("claude_trailer", 0)):
			if key in row and not _string(row[key], minimum, 200):
				raise _invalid(source, index, key)
		if row.get("restart_paths") and "restart_notice" not in row:
			raise _invalid(source, index, "restart_paths")
		found[identity] = Target(identity, path, command, pattern, tuple(row.get("forbidden", [])),
			tuple(row.get("restart_paths", [])), row.get("restart_notice", ""), row.get("claude_trailer", ""))
	return found


def target(identity: str) -> Target:
	if not isinstance(identity, str) or not ID.fullmatch(identity):
		raise ValueError("목록에 없는 대상 저장소입니다")
	try:
		return targets()[identity]
	except KeyError:
		raise ValueError("목록에 없는 대상 저장소입니다") from None


def for_repo(repo: str) -> Target:
	for item in targets().values():
		if str(item.path) == repo:
			return item
	raise ValueError("목록에 없는 대상 저장소입니다")
