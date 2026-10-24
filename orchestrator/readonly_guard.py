"""읽기 전용 호출 전후를 비교하고 새 일반 파일만 정리한다."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_DEPTH = 8
MAX_ENTRIES = 20_000


@dataclass
class Snapshot:
	git: tuple[str, str] | None
	files: dict[str, tuple]
	untracked: set[str]
	states: dict[str, bytes]


def _stamp(path: Path) -> tuple[int, int, int] | None:
	try:
		info = path.lstat()
	except FileNotFoundError:
		return None
	return stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns


def _git(cwd: Path, *args: str) -> bytes:
	result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=30)
	if result.returncode:
		raise ValueError("저장소 검사 실패")
	return result.stdout


def _status(output: bytes) -> dict[bytes, bytes]:
	parts = iter(output.split(b"\0")[:-1])
	states = {}
	for entry in parts:
		if len(entry) < 4 or entry[2:3] != b" ":
			raise ValueError("저장소 상태 형식 오류")
		states[entry[3:]] = entry[:2]
		if b"R" in entry[:2] or b"C" in entry[:2]:
			states[next(parts)] = entry[:2]
	return states


def _git_snapshot(cwd: Path, root: Path) -> Snapshot:
	status = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
	diff = _git(cwd, "diff", "HEAD", "--binary")
	states = _status(status)
	# ignored를 포함한 기존 파일을 새 파일로 오인해 지우지 않도록 이름도 기록한다.
	present = set(_git(cwd, "ls-files", "-z", "--cached", "--others", "--full-name").split(b"\0")) - {b""}
	if len(present | states.keys()) > MAX_ENTRIES:
		raise ValueError("검사 항목 한도 초과")
	files, relative_states, untracked = {}, {}, set()
	for raw in present | states.keys():
		path = root / os.fsdecode(raw)
		name = os.path.relpath(path, cwd)
		if name == ".." or name.startswith("../"):
			continue
		# 중간 링크도 따라가지 않는다. Git은 링크 아래 파일을 추적하지 않는다.
		if any(parent.is_symlink() for parent in path.parents if parent != cwd and cwd in parent.parents):
			raise ValueError("검사 경로에 링크가 있습니다")
		files[name] = (_stamp(path), states.get(raw))
		if raw in states:
			relative_states[name] = states[raw]
		if states.get(raw) == b"??":
			untracked.add(name)
	return Snapshot((hashlib.sha256(status).hexdigest(), hashlib.sha256(diff).hexdigest()),
		files, untracked, relative_states)


def _tree_snapshot(cwd: Path) -> Snapshot:
	files = {}

	def walk(folder: Path, depth: int):
		with os.scandir(folder) as entries:
			for entry in entries:
				if depth >= MAX_DEPTH or len(files) >= MAX_ENTRIES:
					raise ValueError("검사 범위 한도 초과")
				path = Path(entry.path)
				info = entry.stat(follow_symlinks=False)
				files[str(path.relative_to(cwd))] = (stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns)
				if stat.S_ISDIR(info.st_mode):
					walk(path, depth + 1)

	walk(cwd, 0)
	return Snapshot(None, files, {name for name, info in files.items() if info[0] == stat.S_IFREG}, {})


def snapshot(cwd: Path) -> Snapshot | None:
	"""Git 상태·차이의 해시 또는 링크를 따르지 않는 폴더 목록. 검사 불가는 None."""
	try:
		cwd = Path(cwd).absolute()
		if not stat.S_ISDIR(cwd.lstat().st_mode):
			return None
		# 상위 폴더에 내용이 빈 .git 이 있어도 저장소로 오인하지 않도록 git 이 인정한 작업 트리만 쓴다.
		try:
			top = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel")).strip())
		except ValueError:
			return _tree_snapshot(cwd)
		return _git_snapshot(cwd.resolve(), top)
	except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
		return None


def _remove_new(cwd: Path, names: set[str]) -> None:
	try:
		root = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
	except OSError:
		return
	try:
		for name in names:
			parts = Path(name).parts
			if not parts or Path(name).is_absolute() or ".." in parts:
				continue
			fd = os.dup(root)
			try:
				for part in parts[:-1]:
					child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
					os.close(fd)
					fd = child
				if stat.S_ISREG(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
					os.unlink(parts[-1], dir_fd=fd)
			except OSError:
				pass
			finally:
				os.close(fd)
	finally:
		os.close(root)


def verify(cwd: Path, before: Snapshot | None, tool: str) -> None:
	"""검사 불가인 AGY는 실행 전에 거부하고, 변경 오류는 다른 호출 오류보다 우선한다."""
	from .providers import ProviderError

	if before is None:
		if tool == "agy":
			raise ProviderError("write", "읽기 전용을 검사할 수 없는 폴더입니다")
		return
	after = snapshot(cwd)
	if after is None:
		raise ProviderError("write", "읽기 전용을 검사할 수 없는 폴더입니다")
	if before == after:
		return
	changed = sorted(name for name in before.files.keys() | after.files.keys()
		if before.files.get(name) != after.files.get(name))
	if not changed:
		changed = sorted(before.states.keys() | after.states.keys())
	_remove_new(cwd, after.untracked - before.files.keys())
	names = ", ".join(changed[:5]) or "변경 경로 확인 불가"
	more = f" 외 {len(changed) - 5}개" if len(changed) > 5 else ""
	raise ProviderError("write", "읽기 전용 호출이 파일을 바꿨습니다: " + names + more)
