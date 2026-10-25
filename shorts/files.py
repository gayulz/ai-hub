"""상위 링크를 거부하는 fd 접근과 원자적 JSON 저장."""

import fcntl
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import StoreError
from .schema import MAX_RECORD_BYTES, require


def private(fd, *, folder=False, exact_mode=None):
	info = os.fstat(fd)
	require((stat.S_ISDIR(info.st_mode) if folder else stat.S_ISREG(info.st_mode))
		and info.st_uid == os.getuid() and not (info.st_mode & 0o022))
	if not folder:
		require(info.st_nlink == 1)
	if exact_mode is not None:
		require(stat.S_IMODE(info.st_mode) == exact_mode)
	return info


@contextmanager
def directory(path):
	path = Path(path).absolute()
	require(".." not in path.parts)
	fd = None
	try:
		try:
			fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
			for part in path.parts[1:]:
				child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
				os.close(fd)
				fd = child
		except OSError:
			raise StoreError("링크·권한 또는 저장 경로를 확인할 수 없습니다") from None
		yield fd
	finally:
		if fd is not None:
			os.close(fd)


@contextmanager
def child_directory(parent, name):
	fd = None
	try:
		try:
			fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
			private(fd, folder=True)
		except OSError:
			raise StoreError("제작 폴더의 링크·권한을 확인할 수 없습니다") from None
		yield fd
	finally:
		if fd is not None:
			os.close(fd)


def mkdir(parent, name):
	os.mkdir(name, mode=0o700, dir_fd=parent)
	with child_directory(parent, name) as fd:
		os.fchmod(fd, 0o700)
	os.fsync(parent)


def open_file(parent, name, flags):
	flags |= os.O_NOFOLLOW | os.O_NONBLOCK
	created = False
	if flags & os.O_CREAT:
		try:
			fd = os.open(name, flags | os.O_EXCL, 0o600, dir_fd=parent)
			created = True
		except FileExistsError:
			if flags & os.O_EXCL:
				raise
			fd = os.open(name, flags & ~os.O_CREAT, dir_fd=parent)
	else:
		fd = os.open(name, flags, dir_fd=parent)
	try:
		if created:
			os.fchmod(fd, 0o600)
		private(fd, exact_mode=0o600)
	except BaseException:
		os.close(fd)
		raise
	return fd


@contextmanager
def locked(parent, name, *, shared=False, create=False):
	fd = open_file(parent, name, os.O_RDWR | (os.O_CREAT if create else 0))
	try:
		fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
		yield
	finally:
		os.close(fd)


def unique_object(pairs):
	row = {}
	for key, value in pairs:
		require(key not in row)
		row[key] = value
	return row


def read_json(parent, name, *, maximum=MAX_RECORD_BYTES):
	fd = open_file(parent, name, os.O_RDONLY)
	try:
		require(os.fstat(fd).st_size <= maximum)
		with os.fdopen(fd, "rb", closefd=False) as stream:
			raw = stream.read(maximum + 1)
		require(len(raw) <= maximum)
		return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
			parse_constant=lambda value: require(False))
	except (ValueError, UnicodeError, RecursionError):
		raise StoreError("저장된 JSON의 형식을 확인할 수 없습니다") from None
	finally:
		os.close(fd)


def encoded(data):
	try:
		raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
		require(len(raw) <= MAX_RECORD_BYTES)
		return raw
	except (ValueError, TypeError, UnicodeError, RecursionError):
		raise StoreError("제작 기록을 안전한 JSON으로 저장할 수 없습니다") from None


def write_new(parent, name, raw):
	fd = open_file(parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
	try:
		with os.fdopen(fd, "wb", closefd=False) as stream:
			stream.write(raw)
			stream.flush()
			os.fsync(fd)
	finally:
		os.close(fd)
	os.fsync(parent)


def write_atomic(parent, name, data):
	raw = encoded(data)
	try:
		fd = open_file(parent, name, os.O_RDONLY)
	except FileNotFoundError:
		pass
	else:
		os.close(fd)
	temporary = ".tmp-" + uuid.uuid4().hex
	try:
		write_new(parent, temporary, raw)
		os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
		os.fsync(parent)
	finally:
		try:
			os.unlink(temporary, dir_fd=parent)
		except FileNotFoundError:
			pass
