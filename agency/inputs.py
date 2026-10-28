"""준비 명령과 허용 자료를 프로젝트의 비공개 입력으로 고정한다."""

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time

from collector.backfill_common import locked, safe
from collector.config import ConfigError
from jobs import materials, storage

from . import project

MAX_OUTPUT = 1024 * 1024


class InputError(ValueError):
	pass


def _hash(value):
	return hashlib.sha256(value).hexdigest()


def _folder(identity):
	path = project._folder(identity) / "inputs"
	if not safe(path):
		raise InputError("입력 폴더에 심볼릭 링크를 사용할 수 없습니다")
	path.mkdir(mode=0o700, exist_ok=True)
	if not path.is_dir():
		raise InputError("입력 폴더를 확인하세요")
	path.chmod(0o700)
	return path


def _read(path, maximum):
	if not safe(path):
		raise InputError("입력 파일에 심볼릭 링크를 사용할 수 없습니다")
	try:
		fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
		with os.fdopen(fd, "rb") as stream:
			info = os.fstat(stream.fileno())
			if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
				raise InputError("입력 파일의 종류 또는 권한이 잘못됐습니다")
			data = stream.read(maximum + 1)
			if len(data) > maximum:
				raise InputError("입력 파일 크기가 한도를 넘었습니다")
			return data
	except OSError:
		raise InputError("입력 파일을 읽을 수 없습니다") from None


def _save(path, data, maximum):
	if not safe(path):
		raise InputError("입력 파일에 심볼릭 링크를 사용할 수 없습니다")
	fd, temporary = tempfile.mkstemp(prefix=".input-", dir=path.parent)
	try:
		with os.fdopen(fd, "wb") as stream:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(data)
			stream.flush()
			os.fsync(stream.fileno())
		try:
			os.link(temporary, path, follow_symlinks=False)
		except FileExistsError:
			pass
	finally:
		os.unlink(temporary)
	if _read(path, maximum) != data:
		raise InputError("기존 입력 파일이 준비 결과와 다릅니다")


def _stop(process):
	try:
		os.killpg(process.pid, signal.SIGKILL)
	except ProcessLookupError:
		pass
	process.wait()


def _run(command, timeout, cwd):
	argv = [os.path.expanduser(arg) if arg.startswith("~/") else arg for arg in command]
	try:
		process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL, start_new_session=True)
	except OSError:
		raise InputError("실행 파일을 시작할 수 없습니다") from None
	deadline, data = time.monotonic() + timeout, bytearray()
	try:
		with selectors.DefaultSelector() as selector:
			selector.register(process.stdout, selectors.EVENT_READ)
			while selector.get_map():
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					raise InputError("시간 초과")
				for key, _ in selector.select(remaining):
					chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(data)))
					if not chunk:
						selector.unregister(key.fileobj)
					data.extend(chunk)
					if len(data) > MAX_OUTPUT:
						raise InputError("표준출력 크기 초과")
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise InputError("시간 초과")
			try:
				status = process.wait(timeout=remaining)
			except subprocess.TimeoutExpired:
				raise InputError("시간 초과") from None
		if status:
			raise InputError("종료 코드 실패")
		return bytes(data)
	except BaseException:
		_stop(process)
		raise
	finally:
		process.stdout.close()


def _set_prepare(identity, number, value):
	with project._edit(identity, "prepare") as row:
		row.setdefault("input_prepares", {})[str(number)] = value


def _commands(identity, department, folder):
	paths = []
	prior = project.load(identity).get("input_prepares", {})
	if not isinstance(prior, dict) or any(key not in {str(n) for n in range(1, len(department.prepare) + 1)} for key in prior):
		raise InputError("기존 준비 명령의 설정이 바뀌었습니다")
	for number, command in enumerate(department.prepare, 1):
		path = folder / f"prepare-{number}.txt"
		fingerprint = _hash(json.dumps(command, ensure_ascii=False).encode("utf-8"))
		entry = project.load(identity).get("input_prepares", {}).get(str(number))
		if entry:
			if not isinstance(entry, dict) or entry.get("command") != fingerprint:
				raise InputError(f"준비 명령 {number}번 설정이 바뀌었습니다")
			if entry.get("state") == "done":
				if _hash(_read(path, MAX_OUTPUT)) != entry.get("sha256"):
					raise InputError(f"준비 명령 {number}번 결과가 바뀌었습니다")
				paths.append(str(path))
				continue
			if entry.get("state") != "failed":
				raise InputError(f"준비 명령 {number}번의 중단 상태를 확인해야 합니다")
		_set_prepare(identity, number, {"state": "running", "command": fingerprint})
		try:
			data = _run(command, department.prepare_timeout, project._folder(identity))
		except InputError as exc:
			_set_prepare(identity, number, {"state": "failed", "command": fingerprint})
			raise InputError(f"준비 명령 {number}번 실패: {exc}") from None
		_save(path, data, MAX_OUTPUT)
		_set_prepare(identity, number, {"state": "done", "command": fingerprint, "sha256": _hash(data)})
		paths.append(str(path))
	return paths


def _materials(identity, folder, root, *, create):
	allowed = materials.allowed(root)
	snapshot = [{"id": item["id"], "sha256": item["sha256"]} for item in allowed]
	row = project.load(identity)
	previous = row.get("input_materials")
	if previous is not None and previous != snapshot:
		raise InputError("허용 자료 또는 최신 버전이 바뀌었습니다")
	if create and previous is None:
		with project._edit(identity, "input_materials") as current:
			current["input_materials"] = snapshot
	elif not create and previous != snapshot:
		raise InputError("허용 자료의 입력 기록이 없습니다")
	paths = []
	for item in allowed:
		path = folder / ("materials-" + item["id"] + ".md")
		content = item["content"].encode("utf-8")
		if create:
			_save(path, content, materials.MAX_BYTES)
		if _read(path, materials.MAX_BYTES) != content:
			raise InputError("허용 자료의 입력 파일이 바뀌었습니다")
		paths.append(str(path))
	return paths


def collect(identity, department, *, include_materials=True):
	if not department.prepare and (not department.inputs or not include_materials):
		return []
	try:
		with locked("agency-inputs:" + identity):
			folder = _folder(identity)
			paths = _commands(identity, department, folder)
			if department.inputs and include_materials:
				if not department.external_ai:
					raise InputError("외부 AI 금지 부서는 허용 자료를 전달할 수 없습니다")
				with storage.transaction() as root:
					paths += _materials(identity, folder, root, create=True)
			return paths
	except InputError:
		raise
	except (OSError, ConfigError, ValueError, UnicodeError):
		raise InputError("입력 준비 또는 권한 검사에 실패했습니다") from None


def verify(identity, department, request, root=None):
	if not department.prepare and not department.inputs and not request.get("inputs"):
		return
	if department.inputs and (not department.external_ai or root is None):
		raise InputError("허용 자료 잠금 또는 외부 AI 설정이 잘못됐습니다")
	folder = _folder(identity)
	row, paths = project.load(identity), []
	entries = row.get("input_prepares", {})
	if not isinstance(entries, dict) or len(entries) != len(department.prepare):
		raise InputError("준비 명령의 입력 기록이 다릅니다")
	for number, command in enumerate(department.prepare, 1):
		entry = entries.get(str(number), {})
		path = folder / f"prepare-{number}.txt"
		if (not isinstance(entry, dict) or entry.get("state") != "done"
				or entry.get("command") != _hash(json.dumps(command, ensure_ascii=False).encode())
				or entry.get("sha256") != _hash(_read(path, MAX_OUTPUT))):
			raise InputError(f"준비 명령 {number}번의 입력 기록이 다릅니다")
		paths.append(str(path))
	if department.inputs:
		paths += _materials(identity, folder, root, create=False)
	if request.get("inputs", []) != paths:
		raise InputError("분석 요청서의 입력 자료가 다릅니다")
