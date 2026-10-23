"""로컬 크기·수정 시각으로 증분 백업한다. 사본 폴더는 읽지 않는다."""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from collector.archive import _snapshot
from collector.config import home

from . import crypto, state, targets


@dataclass
class BackupStats:
	new: int = 0
	updated: int = 0
	unchanged: int = 0
	failed: int = 0
	written_bytes: int = 0

	def summary(self) -> str:
		return (f"백업: 새로 {self.new}건 · 갱신 {self.updated}건 · 그대로 {self.unchanged}건 · "
			f"실패 {self.failed}건 · 쓴 바이트 {self.written_bytes}")


def version(source: Path) -> dict:
	info = source.lstat()
	if not stat.S_ISREG(info.st_mode):
		raise OSError("일반 파일이 아닙니다.")
	value = {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
	if source.suffix == ".db":
		try:
			wal = source.with_name(source.name + "-wal").lstat()
			if not stat.S_ISREG(wal.st_mode):
				raise OSError("WAL이 일반 파일이 아닙니다.")
			value["wal"] = {"size": wal.st_size, "mtime_ns": wal.st_mtime_ns}
		except FileNotFoundError:
			value["wal"] = None
	return value


def write_file(source: Path, relative: str, before: dict, destination: Path, public: str, stamp: str) -> tuple[dict, int]:
	compressed = source.suffix.lower() not in (".gz", ".zst")
	with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile() as payload:
		original = source
		if source.suffix == ".db":
			original = Path(folder) / "snapshot.db"
			_snapshot(source, original)
		with os.fdopen(os.open(original, os.O_RDONLY | os.O_NOFOLLOW), "rb") as incoming:
			if compressed:
				with gzip.GzipFile(filename="", mode="wb", fileobj=payload, compresslevel=6, mtime=0) as zipped:
					digest, size = crypto.copy_hash(incoming, zipped)
			else:
				digest, size = crypto.copy_hash(incoming, payload)
		if version(source) != before:
			raise OSError("읽는 중 원본이 바뀌었습니다.")
		payload.seek(0)
		name = state.object_name(relative)
		written = crypto.encrypt(payload, destination / name, public)
	return {"name": name, "size": size, "mtime_ns": before["mtime_ns"], "sha256": digest,
		"compressed": compressed, "backed_up_at": stamp}, written


def write_index(entries: dict, destination: Path, public: str) -> int:
	with tempfile.TemporaryFile() as incoming:
		incoming.write(json.dumps({"version": 1, "files": entries}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
		incoming.seek(0)
		return crypto.encrypt(incoming, destination / "index.json.age", public)


def _run(source_roots: dict[str, Path], destination: Path, record: Path, key: Path) -> BackupStats:
	public = crypto.recipient(key)
	previous = state.load(record)
	entries, versions = previous["files"].copy(), previous["sources"].copy()
	if destination.is_symlink():
		raise crypto.BackupError("사본 폴더로 심볼릭 링크를 쓸 수 없습니다.")
	destination.mkdir(mode=0o700, parents=True, exist_ok=True)
	destination.chmod(0o700)
	stats = BackupStats()
	stamp = datetime.now().astimezone().isoformat(timespec="seconds")

	def failed(relative: str) -> None:
		stats.failed += 1
		print(f"백업 실패: {relative}")

	for relative, source in targets.files(source_roots, failed):
		try:
			before = version(source)
			if relative in entries and versions.get(relative) == before:
				stats.unchanged += 1
				continue
			entry, written = write_file(source, relative, before, destination, public, stamp)
			stats.written_bytes += written
			if relative in entries:
				stats.updated += 1
			else:
				stats.new += 1
			entries[relative], versions[relative] = entry, before
		except FileNotFoundError:
			if source.exists():
				failed(relative)
		except Exception:
			failed(relative)
	if stats.new or stats.updated or previous["last_backup"] is None:
		try:
			stats.written_bytes += write_index(entries, destination, public)
		except Exception:
			failed("index.json")
		else:
			previous["files"], previous["sources"] = entries, versions
			previous["last_backup"] = stamp
	else:
		previous["last_backup"] = stamp
	previous["last_failed"] = stats.failed
	state.save(record, previous)
	print(stats.summary())
	return stats


def run(source_roots: dict[str, Path] | None = None, destination: Path | None = None,
		record: Path | None = None, key: Path | None = None) -> BackupStats:
	record = home() / "backup-state.json" if record is None else record
	record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	with os.fdopen(os.open(record.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			raise crypto.BackupError("백업이 이미 실행 중입니다.") from None
		try:
			return _run(targets.roots() if source_roots is None else source_roots,
				targets.backup_dir() if destination is None else destination, record,
				home() / "keys/backup.agekey" if key is None else key)
		except Exception:
			try:
				previous = state.load(record)
				previous["last_failed"] = 1
				state.save(record, previous)
			except Exception:
				pass  # 로컬 기록도 쓸 수 없으면 원래 실패를 명령에 전달한다.
			raise


def status(record: Path | None = None) -> str:
	value = state.load(home() / "backup-state.json" if record is None else record)
	entries = value["files"]
	return (f"마지막 백업: {value['last_backup'] or '없음'} · 파일 {len(entries)}개 · "
		f"원본 크기 합 {sum(entry['size'] for entry in entries.values())}바이트 · 마지막 실패 {value['last_failed']}건")
