"""age 입출력. 도구 오류에 들어 있는 키·절대 경로는 전달하지 않는다."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

CHUNK = 1024 * 1024


class BackupError(Exception):
	"""민감한 값이 없는 사용자 안내."""


def _call(args: list[str], **kwargs):
	try:
		return subprocess.run(args, **kwargs)
	except FileNotFoundError:
		raise BackupError("age·age-keygen 명령을 설치한 뒤 다시 실행하세요.") from None


def recipient(key: Path) -> str:
	public = key.with_suffix(".pub")
	if not public.is_file():
		public = key.with_name(key.name + ".pub")
	if public.is_file():
		value = public.read_text(encoding="ascii").strip()
	else:
		if not key.is_file():
			raise BackupError("백업 키가 없습니다. age-keygen으로 keys/backup.agekey를 만들고 비밀키를 별도 보관하세요.")
		result = _call(["age-keygen", "-y", str(key)], capture_output=True)
		if result.returncode:
			raise BackupError("백업 공개키를 얻지 못했습니다. 키 파일을 확인하세요.")
		value = result.stdout.decode("ascii").strip()
	if not value.startswith("age1") or not value.isalnum():
		raise BackupError("백업 공개키 형식이 잘못되었습니다.")
	return value


def encrypt(incoming, target: Path, public: str) -> int:
	# 사본 폴더에는 암호화가 끝난 바이트만 옮긴다. 실패한 임시 사본도 지우지 않는다.
	with tempfile.TemporaryFile() as encrypted:
		result = _call(["age", "--encrypt", "--recipient", public],
			stdin=incoming, stdout=encrypted, stderr=subprocess.DEVNULL)
		if result.returncode:
			raise BackupError("age 암호화에 실패했습니다.")
		size = encrypted.tell()
		encrypted.seek(0)
		temporary = target.parent / (secrets.token_hex(32) + ".age")
		with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as outgoing:
			shutil.copyfileobj(encrypted, outgoing, CHUNK)
			outgoing.flush()
			os.fsync(outgoing.fileno())
		os.replace(temporary, target)
		return size


def decrypt(source: Path, outgoing, key: Path) -> None:
	with source.open("rb") as incoming:
		result = _call(["age", "--decrypt", "--identity", str(key)],
			stdin=incoming, stdout=outgoing, stderr=subprocess.DEVNULL)
	if result.returncode:
		raise BackupError("age 복호화에 실패했습니다. 키와 사본을 확인하세요.")
	outgoing.seek(0)


def copy_hash(incoming, outgoing, limit: int | None = None) -> tuple[str, int]:
	digest, size = hashlib.sha256(), 0
	while block := incoming.read(CHUNK):
		size += len(block)
		if limit is not None and size > limit:
			raise BackupError("복원 크기가 목록과 다릅니다.")
		digest.update(block)
		outgoing.write(block)
	return digest.hexdigest(), size
