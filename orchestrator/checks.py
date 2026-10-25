"""기계 검증과 작업 폴더(git worktree) 다루기. AI의 "완료" 선언과 별개로 실행기가 직접 확인한다."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

from vaultfilter.store import Dictionary

MAX_LINES = 500
NAME_KINDS = ("회사명", "서비스명", "사람이름")
DEFAULT_TEST = "python3 -m unittest discover -s tests"
TEST_SECONDS, KILL_GRACE_SECONDS = 900, 5


class CommitRejected(RuntimeError):
	"""대상 저장소의 커밋 전 검사 등이 작업 가지 커밋을 거부했다. 메시지는 출력의 끝부분이다."""


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
	result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
	if check and result.returncode != 0:
		raise RuntimeError(f"git {args[0]} 실패: {result.stderr.strip()[-200:]}")
	return result.stdout


def _artifact(path: str) -> bool:
	# 테스트를 돌리면 생기는 파이썬 캐시는 변경으로 치지 않는다
	return "__pycache__/" in path or path.endswith(".pyc")


def changed_paths(worktree: Path, include_artifacts: bool = False) -> list[str]:
	"""미커밋 경로. 반영 충돌 검사에서는 캐시와 이름 변경의 원래 경로도 포함한다."""
	out = git(worktree, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all")
	fields, paths, index = out.split("\0"), [], 0
	while index < len(fields):
		entry = fields[index]
		index += 1
		if len(entry) < 4:
			continue
		if include_artifacts or not _artifact(entry[3:]):
			paths.append(entry[3:])
		if "R" in entry[:2] or "C" in entry[:2]:
			if include_artifacts and index < len(fields):
				paths.append(fields[index])
			index += 1
	return paths


def owned(path: str, patterns: list[str]) -> bool:
	return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def commit_owned(worktree: Path, patterns: list[str], message: str) -> tuple[list[str], list[str]]:
	"""허용 경로의 변경만 커밋한다. (커밋한 경로, 허용 밖 경로). git add -A 는 쓰지 않는다."""
	paths = changed_paths(worktree)
	inside = [p for p in paths if owned(p, patterns)]
	outside = [p for p in paths if not owned(p, patterns)]
	if inside:
		listing = worktree / ".git-orch-paths"
		listing.write_bytes("\0".join(inside).encode("utf-8"))
		try:
			git(worktree, "add", f"--pathspec-from-file={listing}", "--pathspec-file-nul")
		finally:
			listing.unlink(missing_ok=True)
		# 훅 출력은 표준 출력으로도 나오므로 둘 다 모아 끝부분을 보완 지적으로 넘긴다
		result = subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", message], capture_output=True, text=True)
		if result.returncode != 0:
			tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
			raise CommitRejected(tail[-2000:] or "git commit 실패")
	return inside, outside


def _names() -> list[re.Pattern]:
	"""사전의 회사·서비스·사람 이름. 3글자 이하는 단어 경계로만 맞춘다(Desktop 속 글자 같은 오탐 방지)."""
	try:
		entries = Dictionary().entries
	except (OSError, ValueError):
		return []
	patterns = []
	for entry in entries:
		value = entry["value"]
		if entry.get("kind") not in NAME_KINDS or len(value) < 2:
			continue
		escaped = re.escape(value)
		if len(value) <= 3:
			escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
		patterns.append(re.compile(escaped, re.IGNORECASE))
	return patterns


def _added_lines(worktree: Path, base: str, path: str) -> str:
	"""base 에서 HEAD 까지 이 파일에 새로 들어간 줄(새 파일이면 전부)."""
	patch = git(worktree, "-c", "core.quotepath=false", "diff", "-U0", "--no-color", "--no-ext-diff", base, "HEAD", "--", path)
	return "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))


def verify(worktree: Path, base: str, request: dict, outside: list[str]) -> list[dict]:
	"""검사 결과 목록. 값·원문은 담지 않고 파일 이름과 종류만 담는다."""
	results = [{"name": "허용 경로", "ok": not outside, "detail": ", ".join(outside[:10])}]
	files = [p for p in git(worktree, "diff", "--name-only", base, "HEAD").splitlines() if p]
	existing = [p for p in files if (worktree / p).is_file()]
	long_files = [p for p in existing if p.endswith(".py")
		and len((worktree / p).read_text(encoding="utf-8", errors="replace").splitlines()) > MAX_LINES]
	results.append({"name": f"파일 {MAX_LINES}줄", "ok": not long_files, "detail": ", ".join(long_files)})
	names = _names()
	# 파일에 원래 있던 이름은 이번 변경의 잘못이 아니다(공개 저장소 push 검사도 새 줄만 본다)
	leaked = [p for p in existing if any(n.search(p) or n.search(_added_lines(worktree, base, p)) for n in names)]
	results.append({"name": "회사·서비스 이름", "ok": not leaked, "detail": ", ".join(leaked)})
	results.append(run_tests(request.get("test_command", DEFAULT_TEST), worktree))
	return results


def _end_group(process: subprocess.Popen, sig: int) -> None:
	try:
		os.killpg(process.pid, sig)
	except (ProcessLookupError, PermissionError):
		pass


def run_tests(command: str, cwd: Path) -> dict:
	"""시험을 새 세션에서 돌리고, 끝났거나 시간 초과가 되면 그 세션에 남은 프로세스(브라우저 등)까지 끝낸다.

	시험이 띄운 하위 프로세스가 시험 프로세스보다 오래 살아 고아가 되면 램을 계속 차지한다. 출력은 파이프가 아니라
	임시 파일로 받는다(남은 하위 프로세스가 파이프를 쥐고 있어도 시험 프로세스가 끝나면 기다리지 않는다).
	"""
	with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as output:
		process = subprocess.Popen(shlex.split(command), cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
			start_new_session=True)
		try:
			process.wait(timeout=TEST_SECONDS)
		except subprocess.TimeoutExpired:
			_end_group(process, signal.SIGTERM)
			try:
				process.wait(timeout=KILL_GRACE_SECONDS)
			except subprocess.TimeoutExpired:
				pass
			_end_group(process, signal.SIGKILL)
			process.wait()
			return {"name": "테스트", "ok": False, "detail": f"시간 초과({TEST_SECONDS}초)"}
		_end_group(process, signal.SIGKILL)  # 시험이 끝난 뒤에도 남은 하위 프로세스를 정리한다
		output.seek(0)
		tail = output.read().strip().splitlines()[-15:]
	return {"name": "테스트", "ok": process.returncode == 0, "detail": "\n".join(tail)}


def checks_passed(checks: list[dict]) -> bool:
	return bool(checks) and all(c["ok"] for c in checks)
