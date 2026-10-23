"""소급 공통 경계: 안전한 경로, 소유자 전용 결과 파일, 잠금."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError, home
from .redact import redact


def now() -> datetime:
	return datetime.now(ZoneInfo("Asia/Seoul"))


def sha(text: str | bytes) -> str:
	return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def packed(value) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean(text: str) -> str:
	return redact(text).replace("<!--", "&lt;!--")


def safe(path: Path) -> bool:
	"""링크와 비밀 경로는 stat·열기 전에 거른다."""
	return not any(p.startswith("🔐") for p in path.parts) and not any(
		p.is_symlink() for p in (path, *path.parents))


def child(root: Path, relative: str) -> Path:
	rel = Path(relative)
	if rel.is_absolute() or ".." in rel.parts:
		raise ConfigError("허용 루트 밖의 경로입니다")
	path = root / rel
	if not safe(path):
		raise ConfigError("비밀 경로 또는 심볼릭 링크는 읽거나 쓸 수 없습니다")
	return path


def files(root: Path):
	if not safe(root) or not root.is_dir():
		return
	for entry in sorted(root.iterdir()):
		if not safe(entry):
			continue
		if entry.is_dir():
			yield from files(entry)
		elif entry.is_file():
			yield entry


def private_write(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	path.parent.chmod(0o700)
	fd, name = tempfile.mkstemp(prefix=".c4-", dir=path.parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as out:
			out.write(text)
			out.flush()
			os.fsync(out.fileno())
		os.replace(name, path)
		if sha(path.read_bytes()) != sha(text):
			raise OSError("준비 결과 재읽기 해시가 다릅니다")
	finally:
		if os.path.exists(name):
			os.unlink(name)


def numbered(folder: Path, keyword: str) -> Path:
	prefix = now().strftime("%y%m%d")
	entries = list(folder.iterdir()) if folder.is_dir() else []
	used = [int(p.name[7:9]) for p in entries
		if p.name.startswith(prefix + "-") and p.name[7:9].isdigit()]
	number = max(used, default=0) + 1
	if number > 99:
		raise ConfigError("오늘 생성 순번을 모두 사용했습니다")
	return folder / f"{prefix}-{number:02d}-{keyword}.md"


@contextmanager
def locked(name: str, *, exact: Path | None = None):
	path = exact or home() / "run/locks" / (sha(name) + ".lock")
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
	with os.fdopen(fd, "r+") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			yield
		except BlockingIOError as exc:
			raise ConfigError("같은 작업 또는 볼트 자동 기록이 실행 중입니다") from exc
		finally:
			fcntl.flock(lock, fcntl.LOCK_UN)
