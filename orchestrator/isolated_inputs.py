"""로컬 대상 설정에서 지정한 입력만 격리 시험과 도구 없는 AI 호출에 제공한다."""

import copy
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from collector.config import ConfigError

from . import checks, prompts, providers, targets, test_isolation

MAX_BYTES = 2 * 1024 * 1024


def valid_path(name):
	if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
		return False
	path = PurePosixPath(name)
	return (bool(path.parts) and not path.is_absolute() and str(path) == name and not any(part in ("..", ".git", ".blogops") or part.startswith("🔐") for part in path.parts)
		and not any(c in name for c in "*?[") and not path.name.startswith(".env")
		and path.suffix.lower() not in (".db", ".sqlite", ".sqlite3", ".key", ".pem"))


def selected(repo):
	path = Path(repo).resolve(strict=True)
	matches = [item for item in targets.targets().values() if path.samefile(item.path)]
	if len(matches) != 1 or not getattr(matches[0], "isolated_inputs", ()):
		raise ValueError("대상의 격리 입력 목록이 없습니다")
	return matches[0]


def ready(repo):
	try:
		selected(repo)
		return True
	except (ConfigError, OSError, ValueError):
		return False


def snapshot(target, root=None, *, commit=None, missing=False):
	files, total = [], 0
	for name in target.isolated_inputs:
		if not valid_path(name):
			raise ValueError("격리 입력 경로가 잘못됐습니다")
		if commit is not None:
			# 해당 파일의 blob만 읽으며 Git 객체 저장소 자체는 복사하지 않는다.
			mode = checks.git(target.path, "ls-tree", commit, "--", name).split(" ", 1)[0]
			if not mode and missing:
				continue
			if mode not in ("100644", "100755"):
				raise ValueError("격리 입력은 일반 파일이어야 합니다")
			proc = subprocess.run(["git", "-C", str(target.path), "show", commit + ":" + name], capture_output=True, check=True)
			data = proc.stdout
		else:
			path = Path(root or target.path).resolve() / name
			try:
				fd = checks._isolation_open(path)
			except FileNotFoundError:
				if missing:
					continue
				raise ValueError("격리 입력 파일이 없습니다") from None
			with os.fdopen(fd, "rb") as stream:
				info = os.fstat(stream.fileno())
				if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
					raise ValueError("격리 입력은 링크 없는 일반 파일이어야 합니다")
				data = stream.read(MAX_BYTES + 1)
		total += len(data)
		if total > MAX_BYTES:
			raise ValueError("격리 입력 크기가 한도를 넘었습니다")
		files.append(test_isolation.SyntheticFile(name, data, test_isolation.digest(data), "approved-input-v1"))
	if not files:
		raise ValueError("격리 입력이 비어 있습니다")
	return tuple(files)


def check_worktree(target, worktree):
	worktree = Path(worktree)
	for entry in checks.git(worktree, "ls-files", "-t", "-z").split("\0"):
		if entry and (len(entry) < 3 or entry[1] != " " or
				entry[0] != ("H" if entry[2:] in target.isolated_inputs else "S")):
			raise ValueError("격리 작업 폴더의 입력 범위가 다릅니다")
	allowed = set(target.isolated_inputs) | {".git"}
	for folder, directories, files in os.walk(worktree, followlinks=False):
		if any((Path(folder) / name).is_symlink() for name in directories):
			raise ValueError("격리 작업 폴더에 링크가 있습니다")
		for name in files:
			path = Path(folder) / name
			if path.is_symlink() or path.relative_to(worktree).as_posix() not in allowed:
				raise ValueError("격리 작업 폴더에 허용되지 않은 파일이 있습니다")


def check_git_safety(target, commit, worktree=None, *, read_only=False):
	for root in (target.path,) if worktree is None else (target.path, Path(worktree)):
		for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
			path = Path(checks.git(root, "rev-parse", "--git-path", "hooks/" + name).strip())
			path = path if path.is_absolute() else root / path
			if not read_only and os.access(path, os.X_OK):
				raise ValueError("격리할 수 없는 Git 커밋 훅이 있어 실행하지 않습니다")
		for key in ("core.fsmonitor", "commit.gpgsign"):
			value = checks.git(root, "config", "--get", key, check=False).strip().lower()
			if value and value not in ("false", "no", "off", "0"):
				raise ValueError("격리할 수 없는 Git 실행 설정이 있어 실행하지 않습니다")
		command = ["git", "-C", str(root), "check-attr", "-z", "--stdin"]
		if root == target.path:
			command += ["--source", commit]
		command += ["filter", "merge"]
		result = subprocess.run(command, input=("\0".join(target.isolated_inputs) + "\0").encode(), capture_output=True)
		if result.returncode:
			raise ValueError("Git 파일 속성의 격리 여부를 확인할 수 없습니다")
		fields = result.stdout.split(b"\0")
		if len(fields) != len(target.isolated_inputs) * 6 + 1:
			raise ValueError("Git 파일 속성의 격리 여부를 확인할 수 없습니다")
		for index in range(0, len(fields) - 1, 3):
			attribute, value = fields[index + 1:index + 3]
			if (attribute == b"filter" and value not in (b"unspecified", b"unset") or
					attribute == b"merge" and value not in (b"unspecified", b"unset", b"set", b"text", b"binary", b"union")):
				raise ValueError("격리할 수 없는 Git 파일 속성이 있어 실행하지 않습니다")


def verify_commit(repo, worktree, commit):
	if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
		raise ValueError("검증할 커밋이 잘못됐습니다")
	target = selected(repo)
	check_git_safety(target, commit, worktree, read_only=True)
	if checks.git(worktree, "rev-parse", "HEAD").strip() != commit or checks.changed_paths(Path(worktree), include_artifacts=True):
		raise ValueError("검증할 커밋이나 작업 폴더가 변경됐습니다")
	return target


def commit_files(repo, worktree, base, commit, request):
	target = verify_commit(repo, worktree, commit)
	files = [p for p in checks.git(worktree, "diff", "--name-only", base, commit).splitlines() if p]
	if any(p not in target.isolated_inputs or not checks.owned(p, request.get("owned_paths", [])) for p in files):
		raise ValueError("커밋 변경이 허용 경로를 벗어났습니다")
	existing = [p for p in files if checks.git(repo, "ls-tree", commit, "--", p).startswith(("100644 ", "100755 "))]
	long_files = [p for p in existing if p.endswith(".py") and len(checks.git(repo, "show", commit + ":" + p).splitlines()) > checks.MAX_LINES]
	return existing, long_files


def create_worktree(repo, worktree, commit, branch=None):
	target = selected(repo)
	check_git_safety(target, commit)
	files = snapshot(target, commit=commit, missing=True)
	args = ["worktree", "add", "--no-checkout", "-q"]
	args += ["-b", branch] if branch else ["--detach"]
	checks.git(repo, *args, str(worktree), commit)
	checks.git(worktree, "read-tree", commit)
	tracked = checks.git(worktree, "ls-files", "-z").split("\0")
	excluded = [name for name in tracked if name and name not in target.isolated_inputs]
	if excluded:
		result = subprocess.run(["git", "-C", str(worktree), "update-index", "--skip-worktree", "-z", "--stdin"],
			input="\0".join(excluded) + "\0", text=True, capture_output=True)
		if result.returncode:
			raise ValueError("격리 작업 폴더의 입력 범위를 설정하지 못했습니다")
	for item in files:
		path = Path(worktree) / item.name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(item.content)
		mode = checks.git(repo, "ls-tree", commit, "--", item.name).split(" ", 1)[0]
		path.chmod(0o755 if mode == "100755" else 0o644)
	check_worktree(target, worktree)
	check_git_safety(target, commit, worktree)


def review_material(target, base, tip):
	paths = checks.git(target.path, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", base, tip).split("\0")
	paths = [path for path in paths if path]
	if any(path not in target.isolated_inputs for path in paths):
		raise ValueError("격리 검토 범위에 허용되지 않은 변경이 있습니다")
	for commit in (base, tip):
		for name in paths:
			mode = checks.git(target.path, "ls-tree", commit, "--", name).split(" ", 1)[0]
			if mode and mode not in ("100644", "100755"):
				raise ValueError("격리 검토에 일반 파일이 아닌 변경이 있습니다")
	args = ("--no-ext-diff", "--no-textconv", "--no-renames", base, tip, "--", *paths)
	return checks.git(target.path, "diff", "--stat", *args), checks.git(target.path, "diff", *args)


def policy(target, files):
	argv = shlex.split(target.test_command)
	if len(argv) < 3 or argv[1:3] != ["-m", "unittest"] or not Path(argv[0]).name.startswith("python3"):
		raise ValueError("격리 시험은 Python unittest 명령만 지원합니다")
	location = shutil.which(argv[0])
	if not location:
		raise ValueError("격리 Python 실행기를 찾지 못했습니다")
	launcher = Path(location).resolve()
	framework = launcher.parent.parent
	engine = framework / "Resources/Python.app/Contents/MacOS/Python"
	library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
	runtime = tuple(test_isolation.RuntimeFile(str(path), checks.isolation_runtime_file_digest(path), number < 2)
		for number, path in enumerate((launcher, engine, library, framework / "Python")))
	stdlib = framework / "lib" / ("python" + framework.name)
	value = test_isolation.Policy(files, runtime, str(launcher), (str(Path.home()), str(target.path)),
		stdlib_sha256=checks.isolation_runtime_tree(stdlib))
	return value, (str(launcher), *argv[1:])


def run(repo, evidence_dir, *, root=None, commit=None):
	try:
		target = selected(repo)
		if root is not None and Path(root).resolve() != target.path:
			if commit is None:
				check_worktree(target, root)
			else:
				verify_commit(repo, root, commit)
		files = snapshot(target, root, commit=commit)
		value, command = policy(target, files)
		result = test_isolation.run_isolated(value, command, Path(evidence_dir), seconds=900)
		counts = result.get("tests", {})
		detail = result["reason"]
		if counts:
			detail += " · 실행 {ran}개, 실패 {failures}개, 오류 {errors}개, 건너뜀 {skipped}개".format(**counts)
		return {"name": "테스트", "ok": result["ok"], "detail": detail, "executed": result["executed"],
			"unverified": result["unverified"], "counts": counts, "isolation": result}
	except (ConfigError, OSError, ValueError, RuntimeError):
		return test_isolation.unverified()


def worker_schema():
	value = copy.deepcopy(prompts.WORKER_SCHEMA)
	value["required"].append("edits")
	value["properties"]["edits"] = {"type": "array", "items": prompts._strict({
		"path": {"type": "string"}, "content": {"type": "string"}})}
	return value


def call(caller, plan, writer, prompt, worktree, schema, timeout, repo, *, review=False, commit=None, owned_paths=()):
	from agency import private_provider
	target = selected(repo)
	if Path(worktree).resolve() != target.path:
		if review and commit is not None:
			verify_commit(repo, worktree, commit)
		else:
			check_worktree(target, worktree)
	if not review:
		names = tuple(name for name in target.isolated_inputs if checks.owned(name, owned_paths))
		files = snapshot(replace(target, isolated_inputs=names), worktree, missing=True) if names else ()
		if files:
			from vaultfilter.public import Rules
			from .draft import safe_text
			rules = Rules()
			for item in files:
				safe_text(item.name + "\n" + item.content.decode("utf-8"), rules)
		prompt += ("\n도구를 쓰지 않는다. 아래 명시적 입력만 근거로 작업한다. 코드 수정은 edits에 파일 전체 내용을 반환한다. "
			"미제공 자료를 추측하지 않는다. 시험은 서버가 격리 실행한다.\n"
			+ json.dumps({item.name: item.content.decode("utf-8") for item in files}, ensure_ascii=False))
	with tempfile.TemporaryDirectory(prefix="isolated-provider-") as folder:
		adapter = private_provider.call if caller is providers.call else caller
		return adapter(plan, False, prompt, Path(folder), schema, timeout)


def write_edits(worktree, request, edits, repo):
	target = selected(repo)
	if Path(worktree).resolve() == target.path:
		raise ValueError("대상 저장소 원본에는 격리 수정을 쓰지 않습니다")
	check_worktree(target, worktree)
	allowed = target.isolated_inputs
	if not isinstance(edits, list) or len(edits) > 100:
		raise ValueError("격리 구현 응답의 수정 목록이 잘못됐습니다")
	seen, total = set(), 0
	for item in edits:
		if not isinstance(item, dict) or set(item) != {"path", "content"}:
			raise ValueError("격리 구현 응답의 파일 형식이 잘못됐습니다")
		name, content = item["path"], item["content"]
		if (not valid_path(name) or name not in allowed or name in seen or not checks.owned(name, request.get("owned_paths", []))
				or not isinstance(content, str)):
			raise ValueError("격리 구현 응답이 허용 경로를 벗어났습니다")
		seen.add(name)
		total += len(content.encode())
		if total > MAX_BYTES:
			raise ValueError("격리 구현 응답이 크기 한도를 넘었습니다")
		path = Path(worktree) / name
		if any(parent.is_symlink() for parent in (path, *path.parents)):
			raise ValueError("격리 수정에 심볼릭 링크를 사용할 수 없습니다")
	for item in edits:
		path = Path(worktree) / item["path"]
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(item["content"], encoding="utf-8")
