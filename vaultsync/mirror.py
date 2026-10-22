"""Git에서 빠지는 첨부(이미지·PDF·문서 등)를 iCloud Drive에 같은 폴더 구조로 복사한다(D3 A).

추가·갱신만 하고 지우지 않는다. 🔐 노트·폴더, Obsidian 작업 상태·플러그인 파일, 심볼릭 링크는 복사하지 않는다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .sync import git

SKIP_PREFIXES = (".obsidian/", ".trash/", ".git/")
SKIP_NAMES = (".DS_Store",)


def ignored_files(vault: Path) -> list[str]:
	out = git(vault, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.decode("utf-8")
	return [path for path in out.split("\0") if path and not path.endswith("/")]


def wanted(vault: Path, path: str) -> bool:
	if path.startswith(SKIP_PREFIXES) or Path(path).name in SKIP_NAMES:
		return False
	if any(part.startswith("🔐") for part in path.split("/")):
		return False
	file = vault / path
	return file.is_file() and not file.is_symlink()


def same(source: Path, target: Path) -> bool:
	# iCloud가 내려받지 않은 파일도 크기·수정 시각은 그대로 보인다
	if not target.exists():
		return False
	a, b = source.stat(), target.stat()
	return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def mirror(vault: Path, backup: Path) -> int:
	"""복사(새로 또는 갱신)한 파일 수."""
	copied = 0
	for path in ignored_files(vault):
		if not wanted(vault, path):
			continue
		source, target = vault / path, backup / path
		if same(source, target):
			continue
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(source, target)
		copied += 1
	return copied
