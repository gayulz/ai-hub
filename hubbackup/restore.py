"""목록과 해시를 확인하고 복원한다. 폴더 밖 링크를 따라 쓰지 않는다."""

from __future__ import annotations

import gzip
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import crypto, state, targets


@dataclass
class RestoreStats:
	restored: int = 0
	skipped: int = 0
	failed: int = 0

	def summary(self) -> str:
		return f"복원: 복원 {self.restored}건 · 건너뜀 {self.skipped}건 · 실패 {self.failed}건"


@contextmanager
def parent_dir(root_fd: int, parts: tuple[str, ...]):
	current = os.dup(root_fd)
	try:
		for part in parts[:-1]:
			try:
				os.mkdir(part, mode=0o700, dir_fd=current)
			except FileExistsError:
				pass
			child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
			os.close(current)
			current = child
		yield current
	finally:
		os.close(current)


def restore_file(source: Path, entry: dict, key: Path, parent: int, name: str, overwrite: bool) -> bool:
	if not overwrite:
		try:
			os.stat(name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			pass
		else:
			return False
	temporary = ".hubrestore-" + secrets.token_hex(16)
	try:
		with tempfile.TemporaryFile() as payload:
			crypto.decrypt(source, payload, key)
			with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent), "wb") as outgoing:
				if entry["compressed"]:
					with gzip.GzipFile(mode="rb", fileobj=payload) as incoming:
						digest, size = crypto.copy_hash(incoming, outgoing, entry["size"])
				else:
					digest, size = crypto.copy_hash(payload, outgoing, entry["size"])
				if digest != entry["sha256"] or size != entry["size"]:
					raise crypto.BackupError("복원 해시 또는 크기가 목록과 다릅니다.")
				outgoing.flush()
				os.fsync(outgoing.fileno())
		os.utime(temporary, ns=(entry["mtime_ns"], entry["mtime_ns"]), dir_fd=parent, follow_symlinks=False)
		if overwrite:
			os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
		else:
			try:
				os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
			except FileExistsError:
				return False
		return True
	finally:
		try:
			os.unlink(temporary, dir_fd=parent)
		except FileNotFoundError:
			pass


def restore(key: Path, to: Path, overwrite: bool = False, destination: Path | None = None) -> RestoreStats:
	destination = targets.backup_dir() if destination is None else destination
	if not key.is_file():
		raise crypto.BackupError("복원 비밀키가 없습니다. 별도 보관한 비밀키 파일을 --key로 지정하세요.")
	if to.resolve().is_relative_to(destination.resolve()):
		raise crypto.BackupError("복원 폴더는 사본 폴더 밖으로 지정하세요.")
	with tempfile.TemporaryFile() as incoming:
		crypto.decrypt(destination / "index.json.age", incoming, key)
		index = json.load(incoming)
	if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("files"), dict):
		raise crypto.BackupError("백업 목록 형식이 잘못되었습니다.")
	to.mkdir(mode=0o700, parents=True, exist_ok=True)
	root_fd = os.open(to, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
	stats = RestoreStats()
	try:
		for relative, entry in index["files"].items():
			try:
				parts = state.relative_parts(relative)
			except crypto.BackupError:
				stats.failed += 1
				print("복원 실패: 잘못된 상대 경로")
				continue
			try:
				state.validate_entry(relative, entry)
				with parent_dir(root_fd, parts) as parent:
					restored = restore_file(destination / entry["name"], entry, key, parent, parts[-1], overwrite)
				if restored:
					stats.restored += 1
				else:
					stats.skipped += 1
			except Exception:
				stats.failed += 1
				print(f"복원 실패: {relative}")
	finally:
		os.close(root_fd)
	print(stats.summary())
	return stats
