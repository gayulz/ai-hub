"""프로젝트 안의 납품 문서만 저장하고 읽는다."""

import hashlib
import os
import re
import stat
import tempfile

from collector.backfill_common import safe

from . import project


def path(identity: str, task_id: str):
	if not isinstance(task_id, str) or not re.fullmatch(r"t[1-9][0-9]*", task_id):
		raise ValueError("납품 작업 id 형식이 잘못됐습니다")
	result = project._folder(identity) / "deliverables" / (task_id + ".md")
	if not safe(result):
		raise ValueError("납품 문서에 심볼릭 링크를 사용할 수 없습니다")
	return result


def read(identity: str, task_id: str) -> bytes:
	file = path(identity, task_id)
	try:
		fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
		with os.fdopen(fd, "rb") as stream:
			info = os.fstat(stream.fileno())
			if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
				raise ValueError("납품 문서는 권한 0600의 일반 파일이어야 합니다")
			return stream.read()
	except OSError:
		raise ValueError("납품 문서를 읽을 수 없습니다") from None


def save(identity: str, task_id: str, text: str) -> str:
	if not isinstance(text, str) or not text.strip():
		raise ValueError("통과한 납품 문서가 비어 있습니다")
	content = text.encode("utf-8")
	file = path(identity, task_id)
	file.parent.mkdir(mode=0o700, exist_ok=True)
	if not safe(file) or not file.parent.is_dir():
		raise ValueError("납품 문서 폴더가 잘못됐습니다")
	file.parent.chmod(0o700)
	fd, temporary = tempfile.mkstemp(prefix=".document-", dir=file.parent)
	try:
		with os.fdopen(fd, "wb") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		try:
			os.link(temporary, file, follow_symlinks=False)
		except FileExistsError:
			if read(identity, task_id) != content:
				raise ValueError("기존 납품 문서가 실행 결과와 다릅니다") from None
		if read(identity, task_id) != content:
			raise ValueError("납품 문서를 다시 읽은 결과가 다릅니다")
	finally:
		os.unlink(temporary)
	return str(file)


def manifest(row: dict) -> list[dict]:
	items = []
	for task in row["tasks"]:
		content = read(row["id"], task["id"])
		content.decode("utf-8")
		items.append({"task": task["id"], "path": str(path(row["id"], task["id"])),
			"sha256": hashlib.sha256(content).hexdigest()})
	return items
