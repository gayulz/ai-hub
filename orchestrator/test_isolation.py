"""명시적으로 제공된 합성 바이트만 노출하는, 기본 거부 macOS 시험 백엔드.

호출자는 신뢰 경계 바깥에서 출처를 확인한 바이트와 런타임 해시를 제공한다.
저장소 복사, Git 객체 접근, PATH 검색, 네트워크 예외는 제공하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_OUTPUT = 1024 * 1024
class IsolationError(ValueError):
	"""검증되지 않은 실행은 시작하지 않는다."""
class TerminationUnverified(IsolationError):
	"""시험 프로세스가 종료됐는지 확인하지 못했다."""
def requires(repo: str) -> bool:
	from . import targets
	try:
		return not Path(repo).resolve(strict=True).samefile(targets.REPO.resolve(strict=True))
	except (OSError, TypeError, ValueError):
		return True
def unverified() -> dict:
	return {"name": "테스트", "ok": False, "unverified": 1, "executed": False,
		"detail": "합성 소스와 격리 실행 경로가 준비되지 않아 시험하지 않았습니다"}

def digest(value: bytes) -> str:
	return hashlib.sha256(value).hexdigest()

def _quote(value: str) -> str:
	return json.dumps(value, ensure_ascii=False)
def _real_file(value: str) -> Path:
	from .checks import _isolation_open
	path = Path(value)
	try:
		fd = _isolation_open(path)
		try:
			info = os.fstat(fd)
		finally:
			os.close(fd)
	except (OSError, ValueError):
		raise IsolationError("런타임은 링크 없는 절대 경로여야 합니다") from None
	if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
		raise IsolationError("런타임은 연결되지 않은 일반 파일이어야 합니다")
	return path
@dataclass(frozen=True)
class SyntheticFile:
	name: str
	content: bytes
	sha256: str
	provenance: str
@dataclass(frozen=True)
class RuntimeFile:
	path: str
	sha256: str
	executable: bool = False
@dataclass(frozen=True)
class Policy:
	files: tuple[SyntheticFile, ...]
	runtime: tuple[RuntimeFile, ...]
	python: str
	protected: tuple[str, ...]
	# 현재 백엔드는 연결 예외의 소유권을 보장하지 않으므로 모두 거부한다.
	connections: tuple[str, ...] = ()
	browser: bool = False
	version: int = 1
	stdlib_sha256: str = ""
def validate(policy: Policy) -> dict:
	if policy.version != 1 or policy.connections or policy.browser:
		raise IsolationError("요청한 브라우저·통신 경계는 미검증입니다")
	if not policy.files or not policy.runtime or not policy.protected:
		raise IsolationError("합성 출처·런타임·보호 경계가 필요합니다")
	names = set()
	for item in policy.files:
		name = PurePosixPath(item.name)
		if (not item.name or name.is_absolute() or str(name) != item.name
				or any(part in ("..", ".git") for part in name.parts)
				or "\\" in item.name or any(ord(c) < 32 for c in item.name)
				or item.provenance not in ("synthetic-inline-v1", "approved-input-v1") or not isinstance(item.content, bytes)
				or digest(item.content) != item.sha256 or item.name in names):
			raise IsolationError("합성 파일의 출처·이름·해시가 잘못됐습니다")
		names.add(item.name)
	if any(any("/".join(name.split("/")[:i]) in names for i in range(1, len(name.split("/"))))
			for name in names):
		raise IsolationError("합성 파일과 상위 폴더 이름이 겹칩니다")
	if not isinstance(policy.stdlib_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", policy.stdlib_sha256):
		raise IsolationError("표준 라이브러리의 확인 해시가 필요합니다")
	protected = []
	for value in policy.protected:
		path = Path(value)
		if not path.is_absolute():
			raise IsolationError("보호 경계는 절대 경로여야 합니다")
		protected.append(path.resolve())
	paths, executables, hashes = [], [], []
	for item in policy.runtime:
		path = _real_file(item.path)
		# 임의 저장소나 사용자 설치물을 런타임 이름으로 노출하지 않는다.
		if not any(path.is_relative_to(root) for root in
				(Path("/usr"), Path("/System"), Path("/Library/Frameworks"), Path("/opt/homebrew/Cellar"))):
			raise IsolationError("시스템 런타임 밖의 파일입니다")
		if any(path.is_relative_to(root) for root in protected):
			raise IsolationError("런타임과 보호 경계가 겹칩니다")
		from .checks import isolation_runtime_file_digest
		try:
			file_hash = isolation_runtime_file_digest(path)
		except (OSError, ValueError):
			raise IsolationError("런타임 파일을 안전하게 확인할 수 없습니다") from None
		if file_hash != item.sha256 or str(path) in paths:
			raise IsolationError("런타임 해시 또는 중복 경로가 잘못됐습니다")
		paths.append(str(path))
		hashes.append((str(path), item.sha256, item.executable))
		if item.executable:
			if not os.access(path, os.X_OK):
				raise IsolationError("실행 권한이 없는 런타임입니다")
			executables.append(str(path))
	python = Path(policy.python)
	framework = python.parent.parent
	engine = framework / "Resources/Python.app/Contents/MacOS/Python"
	if (python.parent.name != "bin" or framework.parent.name != "Versions"
			or framework.parent.parent.name != "Python.framework" or not python.name.startswith("python3")
			or not framework.is_relative_to(Path("/opt/homebrew/Cellar"))
			or set(executables) != {str(python), str(engine)}):
		raise IsolationError("Python 실행기와 내부 프로세스의 허용 목록이 일치하지 않습니다")
	if (str(framework / "Python") not in paths or any(path not in executables
			and path != str(framework / "Python") and Path(path).suffix != ".dylib" for path in paths)):
		raise IsolationError("검증된 실행기와 동적 라이브러리만 허용합니다")
	dyld = Path("/System/Library/Sandbox/Profiles/dyld-support.sb")
	return {"reads": sorted(paths), "exec": sorted(executables), "runtime": sorted(hashes),
		"dyld_sha256": digest(dyld.read_bytes()),
		"python_root": str(framework), "site": str(framework / "lib" / ("python" + framework.name) / "site-packages"),
		"protected": sorted(str(p) for p in protected),
		"files": sorted((f.name, f.sha256, f.provenance) for f in policy.files),
		"stdlib_sha256": policy.stdlib_sha256, "network": "deny-all", "version": policy.version}
def profile(boundary: dict, work: Path, scratch: Path) -> str:
	lines = ["(version 1)", "(deny default)", '(import "dyld-support.sb")',
		"(deny network*)", "(deny mach-lookup)", "(allow process-fork)",
		"(allow signal (target self))", "(allow sysctl-read)",
		'(allow file-read* file-map-executable (subpath "/System/Library") (subpath "/usr/lib"))',
		"(allow file-read* file-map-executable (subpath " + _quote(boundary["python_root"]) + "))",
		"(deny file-read* (subpath " + _quote(boundary["site"]) + "))"]
	for path in boundary["reads"]:
		lines.append("(allow file-read* file-map-executable (literal " + _quote(path) + "))")
	for path in boundary["exec"]:
		lines.append("(allow process-exec (literal " + _quote(path) + "))")
	lines += ["(allow file-read* (subpath " + _quote(str(work)) + "))",
		"(allow file-read* file-write* (subpath " + _quote(str(scratch)) + "))",
		'(allow file-read* file-write-data (literal "/dev/null"))',
		'(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))']
	metadata = [work, scratch, Path(boundary["python_root"]), Path("/usr/lib"), Path("/System/Library"),
		Path("/dev"), Path("/opt/homebrew/opt"), *(Path(path).parent for path in boundary["reads"])]
	for path in metadata:
		lines.append("(allow file-read-metadata (subpath " + _quote(str(path))
			+ ") (path-ancestors " + _quote(str(path)) + "))")
	# 보호 경계와 허용 경로의 중복은 호출 측에서도 거부한다.
	for path in boundary["protected"]:
		lines.append("(deny file-read* file-write* (subpath " + _quote(path) + "))")
	return "\n".join(lines) + "\n"
def _execute(argv, cwd, environment, seconds):
	process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
	deadline, data, status, exited = time.monotonic() + seconds, bytearray(), "failed", False
	try:
		with selectors.DefaultSelector() as selector:
			selector.register(process.stdout, selectors.EVENT_READ)
			while selector.get_map():
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					status = "timeout"
					break
				for key, _ in selector.select(remaining):
					chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(data)))
					if not chunk:
						selector.unregister(key.fileobj)
					data.extend(chunk)
					if len(data) >= MAX_OUTPUT:
						status = "output_limit"
						break
				if status == "output_limit":
					break
			else:
				while time.monotonic() < deadline:
					if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
						exited = True
						break
					time.sleep(min(0.02, max(0, deadline - time.monotonic())))
				if not exited:
					status = "timeout"
	finally:
		# 실제 시험은 자식 프로세스 차단을 확인한 뒤에만 시작한다.
		if not exited:
			try:
				os.killpg(process.pid, signal.SIGKILL)
			except OSError:
				pass
		try:
			process.stdout.close()
		except OSError:
			pass
		try:
			process.wait(timeout=5)
		except (subprocess.TimeoutExpired, OSError, ValueError):
			raise TerminationUnverified("종료 신호 뒤 시험 프로세스의 종료를 확인하지 못했습니다") from None
	if exited:
		status = "passed" if process.returncode == 0 else "failed"
	data = bytes(data)
	return {"status": status, "returncode": process.returncode,
		"output_sha256": digest(data), "output_bytes": len(data),
		"execution": {"root_pid": process.pid, "session": process.pid,
			"descendants": "inherited-sandbox; membership-not-observed"}}, data
GUARD = r'''import errno,json,os,socket,subprocess,sys
def operation(call):
 try: call(); return "allowed"
 except OSError as e: return "denied" if e.errno in (errno.EPERM,errno.EACCES) else "unverified"
 except subprocess.SubprocessError: return "unverified"
def read(path):
 with open(path,"rb") as stream: stream.read()
def write(path):
 with open(path,"wb") as stream: stream.write(b"changed")
def execute(path): subprocess.run([path],check=True,capture_output=True,timeout=3)
def network(address,family=socket.AF_INET):
 with socket.socket(family) as sock:
  sock.settimeout(2); sock.connect(address)
secret,link,source,binary,binary_link,script,script_link,port,unix,volume_alias,unlisted,scratch_file,original_runtime,hardlink=sys.argv[1:]
checks={"read":operation(lambda:read(secret)),"read_link":operation(lambda:read(link)),
 "read_volume_alias":operation(lambda:read(volume_alias)),"read_unlisted":operation(lambda:read(unlisted)),
 "original_runtime":operation(lambda:read(original_runtime)),
 "write":operation(lambda:write(secret)),"source_write":operation(lambda:write(source)),
 "binary":operation(lambda:execute(binary)),"binary_link":operation(lambda:execute(binary_link)),
 "script":operation(lambda:execute(script)),"script_link":operation(lambda:execute(script_link)),
 "tcp":operation(lambda:network(("127.0.0.1",int(port)))),
 "unix":operation(lambda:network(unix,socket.AF_UNIX))}
checks["scratch_write"]=operation(lambda:write(scratch_file))
checks["source_read"]=operation(lambda:read(source))
checks["hardlink"]=operation(lambda:os.link(secret,hardlink))
child=r"""import errno,json,socket,subprocess,sys
def op(call):
 try: call(); return "allowed"
 except OSError as e: return "denied" if e.errno in (errno.EPERM,errno.EACCES) else "unverified"
 except subprocess.SubprocessError: return "unverified"
def read(path):
 with open(path,"rb") as stream: stream.read()
def connect(address,family=socket.AF_INET):
 with socket.socket(family) as sock: sock.settimeout(2); sock.connect(address)
secret,binary,port,unix=sys.argv[1:]
print(json.dumps({"child_read":op(lambda:read(secret)),
 "child_binary":op(lambda:subprocess.run([binary],check=True,capture_output=True,timeout=2)),
 "child_tcp":op(lambda:connect(("127.0.0.1",int(port)))),
 "child_unix":op(lambda:connect(unix,socket.AF_UNIX))}))"""
run=subprocess.run([sys.executable,"-P","-S","-c",child,secret,binary,port,unix],capture_output=True,text=True,timeout=6)
if run.returncode: checks["child_process"]="unverified"
else:
 try: checks.update(json.loads(run.stdout))
 except ValueError: checks["child_process"]="unverified"
print(json.dumps(checks,sort_keys=True))'''
DRIVER = r'''import json,os,sys,unittest
import resource
resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576))
sys.path.insert(0,os.getcwd())
result=unittest.main(module=None,argv=["unittest",*sys.argv[1:]],exit=False).result
counts={"ran":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped)}
print("AIHUB_TEST_RESULT="+json.dumps(counts,sort_keys=True),flush=True)
sys.exit(0 if result.wasSuccessful() else 1)'''
NO_FORK = r'''import errno,json,os,subprocess,sys
checks={}
try:
 pid=os.fork()
 if pid==0: os._exit(0)
 os.waitpid(pid,0); checks["fork"]="allowed"
except OSError as e: checks["fork"]="denied" if e.errno in (errno.EPERM,errno.EACCES) else "unverified"
try: subprocess.run([sys.executable,"-c","pass"],check=True,timeout=3); checks["child"]="allowed"
except OSError as e: checks["child"]="denied" if e.errno in (errno.EPERM,errno.EACCES) else "unverified"
except subprocess.SubprocessError: checks["child"]="unverified"
for name in ("posix_spawn","posix_spawnp"):
 try:
  pid=getattr(os,name)(sys.executable,[sys.executable,"-c","pass"],dict(os.environ))
  os.waitpid(pid,0); checks[name]="allowed"
 except OSError as e: checks[name]="denied" if e.errno in (errno.EPERM,errno.EACCES) else "unverified"
print(json.dumps(checks,sort_keys=True))'''
class Backend:
	"""출처가 확인된 합성 입력만 받으며 시작 전·종료 뒤 근거를 남긴다."""
	def __init__(self, evidence_dir: Path | None = None):
		self.evidence_dir = evidence_dir
		self._attempts = {}
	def _reserve(self, identity: str, reviewed=None) -> Path:
		from .checks import reserve_isolation_attempt
		path, reservation = reserve_isolation_attempt(self.evidence_dir, identity, reviewed)
		self._attempts[path.name] = reservation
		return path
	def _save(self, path: Path, result: dict) -> None:
		from .checks import save_isolation_attempt
		save_isolation_attempt(path, result, self._attempts[path.name])
	def _guard(self, prefix, python, work, scratch, environment, seconds, original_runtime):
		secret = scratch.parent / "protected" / "sentinel"
		secret.parent.mkdir(mode=0o700)
		secret.write_bytes(b"synthetic")
		unlisted = scratch.parent / "unlisted" / "sentinel"
		unlisted.parent.mkdir(mode=0o700)
		unlisted.write_bytes(b"synthetic")
		source = work / ".isolation-guard-source"
		with source.open("xb") as stream:
			stream.write(b"synthetic")
		source.chmod(0o600)
		volume_alias = Path("/System/Volumes/Data") / secret.relative_to("/")
		if not volume_alias.samefile(secret):
			raise IsolationError("합성 볼륨 별칭을 확인할 수 없습니다")
		link = scratch / "protected-link"
		link.symlink_to(secret)
		binary, binary_link = scratch / "fake-binary", scratch / "fake-binary-link"
		shutil.copyfile("/usr/bin/true", binary)
		binary.chmod(0o700)
		binary_link.symlink_to(binary)
		script, script_link = scratch / "fake-script", scratch / "fake-script-link"
		script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
		script.chmod(0o700)
		script_link.symlink_to(script)
		if any(path.read_bytes() != b"synthetic" for path in (secret, unlisted, source)):
			raise IsolationError("합성 읽기 대조군을 확인할 수 없습니다")
		from .checks import isolation_runtime_file_digest
		isolation_runtime_file_digest(Path(original_runtime))
		with source.open("r+b") as stream:
			stream.write(b"synthetic")
		os.link(secret, scratch.parent / "control-hardlink")
		for executable in (binary, script):
			subprocess.run([str(executable)], check=True, capture_output=True, timeout=3)
		with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
			tcp.bind(("127.0.0.1", 0))
			tcp.listen()
			unix_path = scratch / "test.sock"
			unix.bind(str(unix_path))
			unix.listen()
			with socket.create_connection(("127.0.0.1", tcp.getsockname()[1]), timeout=2):
				accepted, _ = tcp.accept()
				accepted.close()
			with socket.socket(socket.AF_UNIX) as connection:
				connection.connect(str(unix_path))
				accepted, _ = unix.accept()
				accepted.close()
			arguments = [str(secret), str(link), str(source), str(binary),
				str(binary_link), str(script), str(script_link), str(tcp.getsockname()[1]), str(unix_path), str(volume_alias),
				str(unlisted), str(scratch / "allowed-write"), original_runtime, str(scratch / "protected-hardlink")]
			checked, output = _execute(prefix + [python, "-P", "-S", "-c", GUARD, *arguments],
				work, environment, min(seconds, 20))
		checked["controls"] = "synthetic-operations-allowed"
		if (checked["status"] != "passed" or secret.read_bytes() != b"synthetic"
				or source.read_bytes() != b"synthetic" or unlisted.read_bytes() != b"synthetic"):
			raise IsolationError("합성 차단 시험을 완료하지 못했습니다")
		try:
			observed = json.loads(output)
		except (UnicodeError, ValueError):
			raise IsolationError("합성 차단 시험 결과를 확인하지 못했습니다") from None
		names = {"read", "read_link", "read_volume_alias", "read_unlisted", "original_runtime", "write", "source_write", "binary", "binary_link", "script",
			"script_link", "tcp", "unix", "child_read", "child_binary", "child_tcp", "child_unix", "scratch_write", "source_read", "hardlink"}
		allowed = {"scratch_write", "source_read"}
		if (not isinstance(observed, dict) or set(observed) != names
				or any(value != ("allowed" if key in allowed else "denied") for key, value in observed.items())):
			raise IsolationError("합성 차단 시험이 기대한 경계를 보장하지 못했습니다")
		return observed, checked
	def _no_fork_guard(self, prefix, engine, work, environment, seconds):
		checked, output = _execute(prefix + [engine, "-P", "-S", "-c", NO_FORK], work, environment, min(seconds, 15))
		try:
			observed = json.loads(output)
		except (UnicodeError, ValueError):
			raise IsolationError("자식 프로세스 차단 결과를 확인할 수 없습니다") from None
		if checked["status"] != "passed" or observed != {"fork": "denied", "child": "denied", "posix_spawn": "denied", "posix_spawnp": "denied"}:
			raise IsolationError("자식 프로세스 차단이 검증되지 않았습니다")
		return observed, checked
	def run(self, policy: Policy, command: tuple[str, ...], *, seconds: int = 60, reviewed_attempt=None) -> dict:
		result = {"ok": False, "status": "unverified", "unverified": 1,
			"reason": "격리 경계를 확인할 수 없습니다", "executed": False, "test_start_attempted": False}
		evidence, lock_fd, guardian, guard_fd, hold_lock = None, None, None, None, False
		try:
			if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
				raise IsolationError("macOS sandbox-exec을 사용할 수 없습니다")
			if (type(seconds) is not int or not 1 <= seconds <= 1200 or not command
					or any(not isinstance(arg, str) or "\x00" in arg for arg in command)):
				raise IsolationError("시험 명령 또는 한도가 잘못됐습니다")
			boundary = validate(policy)
			if command[0] != policy.python or command[1:3] != ("-m", "unittest"):
				raise IsolationError("명시한 Python의 unittest 명령만 실행할 수 있습니다")
			identity = digest(json.dumps([boundary, command, seconds], sort_keys=True).encode())
			result.update(identity=identity, policy_sha256=digest(json.dumps(boundary, sort_keys=True).encode()),
				command_sha256=digest(json.dumps(command).encode()))
			from .checks import global_isolation_lock, isolation_lock_guard
			lock_fd = global_isolation_lock()
			guardian, guard_fd = isolation_lock_guard(lock_fd)
			evidence = self._reserve(identity, reviewed_attempt)
			self._save(evidence, {"identity": identity, "status": "reserved", "pid": os.getpid(),
				"reviewed": reviewed_attempt, "guardian_pid": guardian.pid})
			if reviewed_attempt is not None:
				result["reviewed_attempt"] = dict(reviewed_attempt)
			with tempfile.TemporaryDirectory(prefix="isolated-synthetic-") as temporary:
				from .checks import copied_isolation_runtime, isolation_runtime_tree
				root = Path(temporary).resolve()
				work, scratch = root / "source", root / "scratch"
				if any(root.is_relative_to(Path(p)) or Path(p).is_relative_to(root)
						for p in boundary["protected"]):
					raise IsolationError("합성 영역과 보호 경계가 겹칩니다")
				work.mkdir(mode=0o700)
				scratch.mkdir(mode=0o700)
				for item in policy.files:
					path = work / item.name
					path.parent.mkdir(parents=True, exist_ok=True)
					path.write_bytes(item.content)
					path.chmod(0o400)
				copied, original_runtime = copied_isolation_runtime(boundary, root)
				copy_root = Path(copied["python_root"])
				copy_stdlib = copy_root / "lib" / ("python" + Path(boundary["python_root"]).name)
				result["runtime_copy"] = {"source_stdlib_sha256": boundary["stdlib_sha256"],
					"modified_copy_stdlib_sha256": isolation_runtime_tree(copy_stdlib),
					"engine_sha256": digest(Path(copied["exec"][0]).read_bytes()),
					"libraries_sha256": [digest(Path(value).read_bytes()) for value in copied["reads"]]}
				guard_boundary = {**copied, "protected": [*boundary["protected"], str(root / "protected")]}
				text = profile(guard_boundary, work, scratch)
				result["profile_sha256"] = digest(text.encode())
				environment = {"HOME": str(scratch), "TMPDIR": str(scratch), "USER": "synthetic",
					"LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
					"PYTHONHOME": copied["python_root"]}
				prefix = ["/usr/bin/sandbox-exec", "-p", text]
				engine = copied["exec"][0]
				result["guard"], result["guard_process"] = self._guard(prefix, engine, work, scratch, environment, seconds, original_runtime)
				test_profile = text + "\n(deny process-fork)\n"
				result["test_profile_sha256"] = digest(test_profile.encode())
				test_prefix = ["/usr/bin/sandbox-exec", "-p", test_profile]
				result["no_fork_guard"], result["no_fork_process"] = self._no_fork_guard(test_prefix, engine, work, environment, seconds)
				actual = [engine, "-P", "-S", "-c", DRIVER, *command[3:]]
				result["actual_command_sha256"] = digest(json.dumps(actual).encode())
				result["test_start_attempted"] = True
				checked, output = _execute(test_prefix + actual,
					work, environment, seconds)
				checked["execution"]["descendants"] = "deny-process-fork; synthetic-probe-passed"
				result.update(executed=True, process=checked)
				if checked["status"] == "timeout":
					result.update(status="timeout", reason="격리 시험이 시간 초과로 중단됐습니다")
					raise IsolationError(result["reason"])
				if checked["status"] == "output_limit":
					result.update(status="failed", reason="격리 시험 출력 한도를 넘었습니다")
					raise IsolationError(result["reason"])
				marker = b"AIHUB_TEST_RESULT="
				line = next((line[len(marker):] for line in reversed(output.splitlines()) if line.startswith(marker)), b"")
				try:
					tests = json.loads(line)
				except (UnicodeError, ValueError):
					raise IsolationError("시험 결과를 확인할 수 없습니다") from None
				if (not isinstance(tests, dict) or set(tests) != {"ran", "failures", "errors", "skipped"}
						or any(type(value) is not int or value < 0 for value in tests.values())):
					raise IsolationError("시험 결과 형식이 잘못됐습니다")
				result["tests"] = tests
				failed = checked["status"] != "passed" or not tests["ran"] or tests["failures"] or tests["errors"]
				result.update(ok=not failed, status="failed" if failed else "passed",
					unverified=tests["skipped"],
					reason="시험 실패가 있습니다" if failed else "격리 시험이 통과했습니다")
		except IsolationError as exc:
			hold_lock = isinstance(exc, TerminationUnverified)
			result["reason"] = str(exc)
		except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
			# 예외 문자열에 실제 경로가 들어갈 수 있어 기록하지 않는다.
			result["reason"] = ("시험 시작 또는 종료 상태가 미확인입니다" if result["test_start_attempted"]
				else "격리 정책 또는 실행 경계 미검증으로 시험을 시작하지 않았습니다")
		finally:
			try:
				if sys.exc_info()[0] is not None:
					result.update(ok=False, status="unverified", reason="중단된 시험의 결과를 확인해야 합니다")
				if guardian is not None:
					from .checks import isolation_guard_outcome
					result.update(isolation_guard_outcome(guardian, guard_fd, hold_lock))
				if evidence is not None:
					try:
						self._save(evidence, result)
					except (OSError, ValueError):
						result.update(ok=False, status="unverified", unverified=1, reason="시험 근거를 보존하지 못했습니다")
			finally:
				if lock_fd is not None:
					os.close(lock_fd)
		return result
def run_isolated(policy: Policy, command: tuple[str, ...], evidence_dir: Path, *, seconds: int = 60, reviewed_attempt=None) -> dict:
	return Backend(evidence_dir).run(policy, command, seconds=seconds, reviewed_attempt=reviewed_attempt)
