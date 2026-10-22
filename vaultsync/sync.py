"""볼트 자동 커밋·push 한 바퀴.

사람이 Git 작업 중이면 건너뛴다. 커밋 전에 파일마다 검사해 문제 파일은 빼고 나머지만 커밋한다(D2 A).
push 전 최종 검사는 볼트의 pre-push 훅이 맡는다. 볼트 안에는 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from collector.config import run_dir
from vaultfilter.codec import encode
from vaultfilter.store import Dictionary
from vaultfilter.verify import _find_leaks

ROOT_FILES = (".gitignore", ".gitattributes")
GIT_MARKERS = ("index.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


@dataclass
class Result:
	skipped_run: str = ""
	committed: list[str] = field(default_factory=list)
	excluded: list[tuple[str, str]] = field(default_factory=list)
	commit: str = ""
	pushed: bool | None = None
	push_error: str = ""


def git(vault: Path, *args: str) -> subprocess.CompletedProcess:
	return subprocess.run(["git", "-C", str(vault), *args], capture_output=True)


def busy(vault: Path) -> str:
	"""사람이 Git 작업 중이면 그 이유를 돌려준다. 비어 있으면 진행해도 된다."""
	for name in GIT_MARKERS:
		if (vault / ".git" / name).exists():
			return f"Git 작업 중({name})"
	branch = git(vault, "symbolic-ref", "-q", "--short", "HEAD").stdout.decode().strip()
	if branch != "main":
		return f"main 브랜치가 아님({branch or '분리된 HEAD'})"
	if git(vault, "diff", "--cached", "--quiet").returncode != 0:
		return "직접 스테이징한 변경이 있음"
	return ""


def changes(vault: Path) -> list[str]:
	"""새 파일·수정·삭제 경로. 무시 목록이 적용된다."""
	out = git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.decode("utf-8")
	fields = out.split("\0")
	paths, index = [], 0
	while index < len(fields):
		entry = fields[index]
		index += 1
		if len(entry) < 4:
			continue
		paths.append(entry[3:])
		if entry[0] in "RC":  # 이름 바꾸기는 원래 경로가 한 칸 더 온다
			index += 1
	return paths


def allowed(path: str) -> bool:
	return path.endswith(".md") or path.startswith(".obsidian/") or path in ROOT_FILES


def problem(vault: Path, path: str, dictionary: Dictionary, values: list[str]) -> str:
	"""커밋하면 안 되는 이유. 비어 있으면 넣어도 된다. 찾은 값은 돌려주지 않는다."""
	if not allowed(path):
		return "허용하지 않는 파일 종류"
	if _find_leaks(path, values):
		return "경로에 사전 이름 또는 연락처"
	file = vault / path
	if file.is_symlink():
		return "심볼릭 링크"
	if not file.exists():
		return ""  # 삭제는 그대로 반영한다
	try:
		text = file.read_text(encoding="utf-8")
	except UnicodeDecodeError:
		return "텍스트가 아님"
	if path.endswith(".md"):
		text = encode(text, dictionary)  # 커밋 때 필터가 하는 일과 같다. 사전 저장은 필터가 한다
	kinds = _find_leaks(text, values)
	return f"필터 뒤에도 누출 의심({', '.join(kinds)})" if kinds else ""


def plan(vault: Path) -> Result:
	result = Result(skipped_run=busy(vault))
	if result.skipped_run:
		return result
	dictionary = Dictionary()
	values = [e["value"] for e in dictionary.entries if len(e["value"]) >= 2]
	for path in changes(vault):
		reason = problem(vault, path, dictionary, values)
		if reason:
			result.excluded.append((path, reason))
		else:
			result.committed.append(path)
	return result


def commit(vault: Path, result: Result, now: datetime) -> None:
	listing = run_dir() / "vaultsync-paths.lst"
	listing.parent.mkdir(parents=True, exist_ok=True)
	listing.write_bytes("\0".join(result.committed).encode("utf-8"))
	added = git(vault, "add", f"--pathspec-from-file={listing}", "--pathspec-file-nul")
	if added.returncode != 0:
		raise RuntimeError("git add 실패(마스킹 필터 실패일 수 있음)")
	message = f"자동 기록: {now:%Y-%m-%d %H:%M} · 파일 {len(result.committed)}개"
	done = git(vault, "commit", "-q", "-m", message)
	if done.returncode != 0:
		git(vault, "reset", "-q")  # 스테이징만 되돌린다. 노트 파일은 그대로다
		raise RuntimeError("git commit 실패(커밋 메시지 검사 또는 훅)")
	result.commit = git(vault, "rev-parse", "--short", "HEAD").stdout.decode().strip()


def push(vault: Path, result: Result) -> None:
	"""원격보다 앞선 커밋이 있으면 올린다. 실패하면 커밋은 로컬에 두고 다음 실행에서 다시 시도한다."""
	ahead = git(vault, "rev-list", "--count", "origin/main..main")
	if ahead.returncode != 0 or ahead.stdout.decode().strip() == "0":
		return
	sent = git(vault, "push", "-q", "origin", "main")
	result.pushed = sent.returncode == 0
	if not result.pushed:
		lines = sent.stderr.decode("utf-8", errors="replace").strip().splitlines()
		result.push_error = lines[-1] if lines else "push 실패"
