"""대화 원문의 로컬 압축 사본. 원천이 사라져도 완성된 사본은 지우지 않는다."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import home

ROOTS = {
	"claude": Path.home() / ".claude" / "projects",
	"codex": Path.home() / ".codex",
	"gemini": Path.home() / ".gemini" / "tmp",
	"agy": Path.home() / ".gemini",
}
PATTERNS = {
	"claude": ("**/*.jsonl",),
	"codex": ("sessions/**/*.jsonl", "archived_sessions/*.jsonl", "archived_sessions/*.jsonl.zst"),
	"gemini": ("*/chats/**/*",),
	"agy": ("antigravity-cli/conversations/*.db", "antigravity-cli/conversation_summaries.db",
		"antigravity/conversation_summaries.db", "antigravity/conversations/*.pb",
		"antigravity-ide/conversations/*.db", "antigravity-ide/conversations/*.pb",
		"antigravity-backup/conversations/*.pb"),
}


@dataclass
class ArchiveStats:
	new: int = 0
	updated: int = 0
	unchanged: int = 0
	failed: int = 0
	written_bytes: int = 0

	def summary(self) -> str:
		return (f"보관: 새로 {self.new}건 · 갱신 {self.updated}건 · 그대로 {self.unchanged}건 · "
			f"실패 {self.failed}건 · 쓴 바이트 {self.written_bytes}")


def _wal(source: Path) -> Path:
	return source.with_name(source.name + "-wal")


def _mtime(source: Path, database: bool) -> int:
	stamp = source.stat().st_mtime_ns
	if database:
		try:
			stamp = max(stamp, _wal(source).stat().st_mtime_ns)
		except FileNotFoundError:
			pass
	return stamp


def _private_dir(path: Path, root: Path) -> None:
	root.mkdir(mode=0o700, parents=True, exist_ok=True)
	root.chmod(0o700)
	for part in path.relative_to(root).parts:
		root /= part
		root.mkdir(mode=0o700, exist_ok=True)
		root.chmod(0o700)


def _copy(source: Path, target: Path) -> None:
	with source.open("rb") as incoming, target.open("wb") as outgoing:
		shutil.copyfileobj(incoming, outgoing)


def _version(source: Path) -> tuple:
	versions = []
	for path in (source, _wal(source)):
		try:
			info = path.stat()
			versions.append((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
		except FileNotFoundError:
			if path == source:
				raise
			versions.append(None)
	return tuple(versions)


def _backup_database(source: Path, target: Path) -> None:
	def progress(status: int, remaining: int, total: int) -> None:
		if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
			raise sqlite3.OperationalError("DB 잠금으로 보관을 미룹니다")

	incoming = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
	try:
		outgoing = sqlite3.connect(target)
		try:
			incoming.backup(outgoing, pages=256, progress=progress)
		finally:
			outgoing.close()
	finally:
		incoming.close()


def _snapshot(source: Path, target: Path) -> None:
	with source.open("rb") as incoming:
		wal_mode = incoming.read(20)[18:20] == b"\x02\x02"
	if not wal_mode:
		_backup_database(source, target)
		return
	# WAL은 읽기 전용 연결도 원천에 보조 파일을 만들 수 있어 안정된 사본에서 연다.
	with tempfile.TemporaryDirectory(dir=target.parent) as folder:
		staged = Path(folder) / "source.db"
		before = _version(source)
		_copy(source, staged)
		if before[1] is not None:
			_copy(_wal(source), _wal(staged))
		if _version(source) != before:
			raise OSError("복사 중 DB가 바뀌어 보관을 미룹니다")
		_backup_database(staged, target)


def _compress(source: Path, outgoing) -> None:
	with source.open("rb") as incoming, gzip.GzipFile(
		filename="", mode="wb", fileobj=outgoing, compresslevel=6, mtime=0,
	) as compressed:
		shutil.copyfileobj(incoming, compressed)


def _write(source: Path, target: Path, stamp: int, database: bool) -> int:
	with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".archive-", delete=False) as outgoing:
		temporary = Path(outgoing.name)
		try:
			if source.suffix == ".zst":
				with source.open("rb") as incoming:
					shutil.copyfileobj(incoming, outgoing)
			elif database:
				with tempfile.TemporaryDirectory(dir=target.parent) as folder:
					payload = Path(folder) / "snapshot.db"
					_snapshot(source, payload)
					_compress(payload, outgoing)
			else:
				_compress(source, outgoing)
			outgoing.flush()
			source.stat()
			size = outgoing.tell()
			os.utime(temporary, ns=(stamp, stamp))
			os.replace(temporary, target)
			return size
		finally:
			# 이번 임시 파일만 정리하며 완성된 보관 사본에는 손대지 않는다.
			temporary.unlink(missing_ok=True)


def archive(roots: dict[str, Path] | None = None) -> ArchiveStats:
	stats = ArchiveStats()
	root = home() / "archive"
	_private_dir(root, root)
	for tool, source_root in (ROOTS if roots is None else roots).items():
		for pattern in PATTERNS[tool]:
			try:
				paths = sorted(source_root.glob(pattern))
			except OSError as exc:
				stats.failed += 1
				print(f"보관 실패: {tool}/{pattern} ({type(exc).__name__})")
				continue
			for source in paths:
				relative = source.relative_to(source_root)
				try:
					if not source.is_file():
						continue
					database = tool == "agy" and source.suffix == ".db"
					stamp = _mtime(source, database)
					target = root / tool / relative
					if source.suffix != ".zst":
						target = target.with_name(target.name + ".gz")
					existed = target.exists()
					if existed and target.stat().st_mtime_ns == stamp:
						stats.unchanged += 1
						continue
					_private_dir(target.parent, root)
					size = _write(source, target, stamp, database)
					stats.written_bytes += size
					if existed:
						stats.updated += 1
					else:
						stats.new += 1
				except FileNotFoundError:
					continue
				except Exception as exc:
					stats.failed += 1
					print(f"보관 실패: {tool}/{relative.as_posix()} ({type(exc).__name__})")
	print(stats.summary())
	return stats
