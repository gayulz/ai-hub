"""외부 작업 가지의 커밋만 별도 읽기 전용 Seatbelt에서 실행한다."""

import hashlib
import json
import os
import platform
import stat
import tempfile
from pathlib import Path, PurePosixPath

from . import checks, isolated_inputs, test_isolation

HOOK_SHA256 = "4d47e0a760d3b225d4360bc2257499d151b36ac18ec5e3fcd1d57333eb4e68aa"
AUTO_SHA256 = "40943fb29d3a1ff8f83dee95bad9f1bdbd47047c1c1dcf28fe4bb77b88dc9513"
AUTO = Path("/usr/local/bin/auto")
GIT = Path("/Library/Developer/CommandLineTools/usr/bin/git")
FAILURE = "격리 커밋 또는 커밋 전 검사를 통과하지 못했습니다"


class IndexUpdateError(Exception):
	"""커밋 뒤 인덱스 정리가 실패하면 자동 재시도하지 않는다."""


class CommitStateUnknown(Exception):
	"""커밋 종료 상태가 불확실하면 자동 재시도하지 않는다."""


def _verified(path, digest):
	try:
		info = path.lstat()
		return (stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
			and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
	except OSError:
		return False


def approved_hook(path):
	return _verified(Path(path), HOOK_SHA256) and _verified(AUTO, AUTO_SHA256)


def _gitdirs(worktree, repo):
	link = (worktree / ".git").read_text().strip()
	if not link.startswith("gitdir: "):
		raise ValueError("연결된 작업 폴더가 아닙니다")
	gitdir = (worktree / link[8:]).resolve(strict=True)
	common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve(strict=True)
	if common != (repo / ".git").resolve(strict=True) or gitdir.parent != common / "worktrees":
		raise ValueError("Git 작업 폴더의 저장소가 다릅니다")
	head = (gitdir / "HEAD").read_text().strip()
	if not head.startswith("ref: refs/heads/"):
		raise ValueError("작업 가지를 확인할 수 없습니다")
	name = head[5:]
	parts = PurePosixPath(name)
	if str(parts) != name or any(p in (".", "..") for p in parts.parts):
		raise ValueError("작업 가지를 확인할 수 없습니다")
	return gitdir, common, common / name


def _profile(worktree, gitdir, common, ref, scratch, hook):
	quote = lambda value: json.dumps(str(value), ensure_ascii=False)
	lines = ["(version 1)", "(deny default)", '(import "dyld-support.sb")',
		"(deny network*)", "(allow process-fork)", "(allow sysctl-read)", "(allow mach-lookup)",
		'(allow file-read* file-map-executable (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share") (subpath "/Library/Developer/CommandLineTools") (subpath "/usr/bin"))',
		'(allow file-read* file-write-data (literal "/dev/null"))',
		'(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))']
	for path in (worktree, common):
		lines.append("(allow file-read* (subpath " + quote(path) + "))")
	lines.append("(allow file-read* file-write* (subpath " + quote(scratch) + "))")
	lines.append("(allow file-write* (subpath " + quote(common / "objects") + "))")
	for path in (ref, Path(str(ref) + ".lock"), common / "logs" / ref.relative_to(common),
			gitdir / "index", gitdir / "index.lock", gitdir / "HEAD.lock", gitdir / "COMMIT_EDITMSG", gitdir / "logs/HEAD"):
		lines.append("(allow file-write* (literal " + quote(path) + "))")
	for path in (worktree, common, gitdir, scratch, ref, Path("/private/var/select/sh")):
		lines.append("(allow file-read-metadata (path-ancestors " + quote(path) + "))")
	lines.append('(allow file-read* (literal "/private/var/select/sh"))')
	for path in (GIT, Path("/usr/bin/git"), Path("/bin/sh"), Path("/bin/bash"), *([AUTO, hook] if hook else [])):
		lines.append("(allow file-read* file-map-executable (literal " + quote(path) + "))")
		lines.append("(allow process-exec (literal " + quote(path) + "))")
	if hook:
		lines.append("(allow file-read-metadata (path-ancestors " + quote(hook) + "))")
	return "\n".join(lines)


def commit_owned_isolated(worktree: Path, patterns: list[str], message: str, repo: Path) -> tuple[list[str], list[str]]:
	try:
		worktree, repo = Path(worktree).resolve(strict=True), Path(repo).resolve(strict=True)
		if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file() or not GIT.is_file():
			raise ValueError("격리 실행기를 사용할 수 없습니다")
		target = isolated_inputs.selected(repo)
		gitdir, common, ref = _gitdirs(worktree, repo)
		if any((gitdir / name).exists() for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")):
			raise ValueError("진행 중인 Git 작업이 있습니다")
		commit = checks.git(worktree, "rev-parse", "HEAD").strip()
		isolated_inputs.check_git_safety(target, commit, worktree)
		hook = Path(checks.git(worktree, "rev-parse", "--git-path", "hooks/pre-commit").strip())
		hook_path = (hook if hook.is_absolute() else worktree / hook).absolute()
		hook = hook_path if os.access(hook_path, os.X_OK) else None
		if hook and not approved_hook(hook):
			raise ValueError("커밋 훅 검증에 실패했습니다")
		with tempfile.TemporaryDirectory(prefix="isolated-commit-") as folder:
			scratch = Path(folder).resolve()
			profile = _profile(worktree, gitdir, common, ref, scratch, hook)
			env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/local/bin:/Library/Developer/CommandLineTools/usr/bin:/usr/bin:/bin",
				"LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_LITERAL_PATHSPECS": "1"}
			def git(*args, index=False):
				value = dict(env)
				if index:
					value["GIT_INDEX_FILE"] = str(scratch / "index")
				try:
					result, output = test_isolation._execute(["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c",
						'exec "$@" 2>/dev/null', "sh", str(GIT), *args], worktree, value, 120)
				except test_isolation.TerminationUnverified:
					raise CommitStateUnknown("격리 Git 프로세스 종료를 확인하지 못했습니다. 상태를 확인해야 합니다") from None
				if args[0] == "commit" and result["status"] in ("timeout", "output_limit"):
					raise CommitStateUnknown("커밋 결과가 불확실합니다. 작업 가지와 인덱스를 확인해야 합니다")
				if result["status"] != "passed":
					raise checks.CommitRejected(FAILURE)
				return output.decode("utf-8", "strict")
			actual = Path(git("rev-parse", "--git-path", "hooks/pre-commit").strip())
			actual = (actual if actual.is_absolute() else worktree / actual).absolute()
			if actual != hook_path or (hook is None and os.access(actual, os.X_OK)):
				raise ValueError("커밋 훅 설정이 다릅니다")
			staged = set(git("diff", "--cached", "--name-only", "--no-renames", "-z").split("\0"))
			status = git("--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
			paths = [item[3:] for item in status.split("\0") if len(item) >= 4 and not checks._artifact(item[3:])]
			inside = [p for p in paths if p not in staged and p in target.isolated_inputs and checks.owned(p, patterns)]
			outside = [p for p in paths if p not in inside]
			if inside:
				git("read-tree", "HEAD", index=True)
				git("add", "--", *inside, index=True)
				isolated_inputs.check_git_safety(target, commit, worktree)
				if hook and not approved_hook(hook):
					raise ValueError("커밋 훅 검증에 실패했습니다")
				git("commit", "-q", "-m", message, index=True)
				try:
					git("reset", "-q", "HEAD", "--", *inside)
				except (OSError, RuntimeError, UnicodeError):
					raise IndexUpdateError("커밋은 생성됐지만 인덱스 정리에 실패했습니다. 상태를 확인해야 합니다") from None
			return inside, outside
	except (OSError, ValueError, RuntimeError, UnicodeError):
		raise checks.CommitRejected(FAILURE) from None
