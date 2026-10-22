"""등록 저장소의 하루치 커밋·문서 변경과 미등록 저장소 찾기.

커밋은 저장소 사용자 이메일(git config user.email)로 거른다. 다른 사람의 커밋을 내 일지에 넣지 않기 위해서다.
문서 변경은 파일 수정 시각이 그날인 md·txt 파일이다(내용 해시는 쓰지 않는다).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

DOC_SUFFIXES = (".md", ".txt")
SKIP_PARTS = ("node_modules", "/build/", "/target/", "/dist/", "/.git/")


@dataclass(frozen=True)
class Commit:
	sha: str
	when: datetime
	subject: str


def _git(path: Path, *args: str) -> str:
	result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=60)
	return result.stdout if result.returncode == 0 else ""


def author_email(path: Path) -> str:
	return _git(path, "config", "user.email").strip()


def commits_on(path: Path, day: date) -> list[Commit]:
	email = author_email(path)
	if not email or not path.is_dir():
		return []
	start = datetime.combine(day, datetime.min.time()).astimezone()
	end = start + timedelta(days=1)
	out = _git(path, "log", f"--since={start.isoformat()}", f"--until={end.isoformat()}",
		f"--author={email}", "--pretty=format:%h%x09%aI%x09%s")
	commits = []
	for line in out.splitlines():
		sha, when, subject = line.split("\t", 2)
		commits.append(Commit(sha, datetime.fromisoformat(when), subject))
	return sorted(commits, key=lambda c: c.when)


def docs_on(path: Path, doc_dirs: tuple[str, ...], day: date) -> list[str]:
	found: list[str] = []
	for rel in doc_dirs:
		root = path / rel
		if not root.is_dir():
			continue
		# "." 은 최상위 문서만, 그 밖의 폴더는 하위까지 읽는다
		files = root.glob("*") if rel == "." else root.rglob("*")
		for file in files:
			if not file.is_file() or not file.name.endswith(DOC_SUFFIXES):
				continue
			if any(part in str(file) for part in SKIP_PARTS):
				continue
			if datetime.fromtimestamp(file.stat().st_mtime).date() == day:
				found.append(str(file.relative_to(path)))
	return sorted(found)


def find_unregistered(home: Path, registered: set[str], ignore: set[str], max_depth: int = 3) -> list[str]:
	"""홈 폴더 3단계 안에서 .git이 있는데 등록·무시되지 않은 저장소 경로."""
	found: list[str] = []

	def walk(folder: Path, depth: int) -> None:
		if depth > max_depth:
			return
		try:
			entries = list(os.scandir(folder))
		except OSError:
			return
		names = {e.name for e in entries}
		if ".git" in names and str(folder) not in registered | ignore and not any(
				str(folder).startswith(r + "/") for r in registered | ignore):
			found.append(str(folder))
			return  # 저장소 안쪽은 더 보지 않는다
		for entry in entries:
			if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") \
					and entry.name not in ("Library", "node_modules"):
				walk(Path(entry.path), depth + 1)

	walk(home, 0)
	return found
