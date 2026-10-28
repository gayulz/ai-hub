"""승인한 납품 묶음만 임시 병합·시험 뒤 본 가지에 반영한다."""

import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

from collector.backfill_common import locked, now, private_write, safe
from collector.config import ConfigError
from orchestrator import targets
from orchestrator.checks import changed_paths, git

from . import document_delivery, project
from .departments import valid_text

KEYS = {"version", "project", "target", "repo", "branch", "branch_tip", "base", "actions",
	"restart_notice", "changed", "criteria", "tests", "reviewer", "made", "hash"}


class _Failure(RuntimeError):
	"""외부 명령의 원문을 담지 않는 납품 실패."""


def _read(path: Path, *, missing: str | None = None) -> str:
	if not safe(path):
		raise ValueError("납품 파일에 심볼릭 링크를 사용할 수 없습니다")
	try:
		fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
		with os.fdopen(fd, encoding="utf-8") as stream:
			if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
				raise ValueError("납품 파일은 일반 파일이어야 합니다")
			return stream.read()
	except FileNotFoundError:
		if missing is not None:
			return missing
		raise ValueError("납품 파일이 없습니다") from None
	except (OSError, UnicodeError):
		raise ValueError("납품 파일을 읽을 수 없습니다") from None


def load_bundle(identity: str) -> dict:
	try:
		bundle = json.loads(_read(project._folder(identity) / "delivery.json"))
	except (ValueError, TypeError, RecursionError):
		raise ValueError("납품 묶음의 형식 또는 hash가 잘못됐습니다") from None
	if isinstance(bundle, dict) and type(bundle.get("version")) is int and bundle["version"] == 2:
		return document_delivery.validate(identity, bundle)
	try:
		if (not isinstance(bundle, dict) or bundle.keys() != KEYS
				or type(bundle["version"]) is not int or bundle["version"] != 1 or bundle["project"] != identity):
			raise ValueError
		for key in ("target", "repo", "branch", "branch_tip", "base", "hash", "made", "reviewer"):
			if not isinstance(bundle[key], str) or not bundle[key].strip() or "\x00" in bundle[key]:
				raise ValueError
		for key in ("branch_tip", "base"):
			if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", bundle[key]):
				raise ValueError
		actions = bundle["actions"]
		if (not isinstance(actions, list) or not actions or actions[0] != "merge"
				or any(action not in ("merge", "push", "restart") for action in actions)
				or len(set(actions)) != len(actions)):
			raise ValueError
		if (not isinstance(bundle["restart_notice"], str) or "\x00" in bundle["restart_notice"]
				or not isinstance(bundle["changed"], list) or any(not isinstance(p, str) for p in bundle["changed"])
				or not isinstance(bundle["criteria"], list) or not isinstance(bundle["tests"], dict)):
			raise ValueError
		if any(not isinstance(item, dict) or not isinstance(item.get("criterion"), str)
				or item.get("verdict") not in ("pass", "fail", "unverified") for item in bundle["criteria"]):
			raise ValueError
		payload = {key: value for key, value in bundle.items() if key != "hash"}
		digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
		if bundle["hash"] != digest:
			raise ValueError
	except (ValueError, TypeError, RecursionError):
		raise ValueError("납품 묶음의 형식 또는 hash가 잘못됐습니다") from None
	selected = targets.target(bundle["target"])
	if bundle["repo"] != str(selected.path):
		raise ValueError("납품 묶음의 저장소가 대상 설정과 다릅니다")
	try:
		git(selected.path, "check-ref-format", "--branch", bundle["branch"])
	except (OSError, RuntimeError):
		raise ValueError("납품 묶음의 가지 이름이 잘못됐습니다") from None
	return bundle


def _git(repo: Path, *args: str, reason: str = "Git 처리 실패") -> str:
	try:
		return git(repo, *args)
	except (OSError, RuntimeError):
		raise _Failure(reason) from None


def _test(command: str, folder: Path) -> None:
	try:
		with subprocess.Popen(shlex.split(command), cwd=folder, stdout=subprocess.DEVNULL,
				stderr=subprocess.STDOUT, start_new_session=True) as process:
			try:
				code = process.wait(timeout=1200)
			except subprocess.TimeoutExpired:
				raise _Failure("병합 결과 시험 시간 초과") from None
			finally:
				try:
					os.killpg(process.pid, signal.SIGKILL)
				except ProcessLookupError:
					pass
				process.wait()
	except (OSError, ValueError):
		raise _Failure("병합 결과 시험 실행 실패") from None
	if code != 0:
		raise _Failure("병합 결과 시험 실패")


def _merge_test(repo: Path, main: str, bundle: dict, title: str, command: str) -> str:
	with tempfile.TemporaryDirectory(prefix="agency-delivery-") as temporary:
		folder = Path(temporary) / "worktree"
		try:
			_git(repo, "worktree", "add", "--detach", str(folder), main, reason="임시 작업 폴더 준비 실패")
			try:
				_git(folder, "merge", "--no-ff", "--no-edit", "-m",
					f"사무실 프로젝트 {bundle['project']}: {title}", bundle["branch_tip"], reason="병합 충돌 또는 병합 실패")
			except _Failure:
				git(folder, "merge", "--abort", check=False)
				raise
			_test(command, folder)
			return _git(folder, "rev-parse", "HEAD", reason="병합 커밋 확인 실패").strip()
		finally:
			if folder.exists():
				_git(repo, "worktree", "remove", "--force", str(folder), reason="임시 작업 폴더 제거 실패")


def _waiting(identity: str) -> dict:
	row = project.load(identity)
	if row["status"] != "납품 대기":
		raise ValueError("납품 대기 프로젝트만 승인하거나 반려할 수 있습니다")
	return row


def _append(identity: str, text: str) -> None:
	path = project._folder(identity) / "report.md"
	private_write(path, _read(path, missing="") + "\n\n" + text + "\n")


def _facts(forwarded: bool, result: dict) -> str:
	return ("빨리 감기: " + ("완료" if forwarded else "안 함") + "; push: "
		+ ("완료" if result["pushed"] else "안 됨") + "; 원격 해시 일치: "
		+ ("일치" if result["remote_matches"] else "미확인 또는 불일치"))


def approve(identity: str, bundle_hash: str) -> dict:
	project._folder(identity)
	with locked("agency-delivery:" + identity):
		_waiting(identity)
		bundle = load_bundle(identity)
		if bundle["version"] == 2:
			return document_delivery.approve(identity, bundle_hash, bundle)
		repo = Path(bundle["repo"])
		with locked("agency-repo:" + str(repo)):
			row = _waiting(identity)
			if load_bundle(identity)["hash"] != bundle_hash or bundle["hash"] != bundle_hash:
				raise ValueError("승인 hash가 납품 묶음과 다릅니다")
			ref = "refs/heads/" + bundle["branch"]
			if git(repo, "rev-parse", "--verify", ref, check=False).strip() != bundle["branch_tip"]:
				raise ValueError("프로젝트 가지 끝이 바뀌었습니다. 다시 검수하세요")
			_read(project._folder(identity) / "report.md", missing="")
			stamp = now().isoformat(timespec="seconds")
			project.transition(identity, "납품 중")
			result = {"status": "납품 중", "merge_commit": "", "pushed": False, "remote_matches": False,
				"restart_notice": bundle["restart_notice"] if "restart" in bundle["actions"] else ""}
			forwarded = False
			try:
				if _git(repo, "diff", "--cached", "--name-only"):
					raise _Failure("본 저장소에 스테이징된 변경이 있습니다")
				main = _git(repo, "rev-parse", "HEAD", reason="본 가지 확인 실패").strip()
				main_ref = _git(repo, "symbolic-ref", "-q", "HEAD", reason="본 저장소 HEAD가 가지를 가리키지 않습니다").strip()
				selected = targets.target(bundle["target"])
				result["merge_commit"] = _merge_test(repo, main, bundle, row["title"], selected.test_command)
				paths = _git(repo, "diff", "--name-only", "-z", "--no-renames", main, result["merge_commit"]).split("\0")
				dirty = changed_paths(repo, include_artifacts=True)
				if any(p == d or p.startswith(d + "/") or d.startswith(p + "/") for p in paths if p for d in dirty):
					raise _Failure("작업 중인 파일과 병합 변경이 겹칩니다")
				if (_git(repo, "rev-parse", "HEAD").strip() != main or _git(repo, "symbolic-ref", "-q", "HEAD").strip() != main_ref):
					raise _Failure("본 가지가 승인 뒤에 바뀌었습니다. 다시 검수하세요")
				if load_bundle(identity)["hash"] != bundle_hash or _git(repo, "rev-parse", ref).strip() != bundle["branch_tip"]:
					raise _Failure("납품 묶음 또는 프로젝트 가지가 승인 뒤에 바뀌었습니다. 다시 검수하세요")
				if _git(repo, "diff", "--cached", "--name-only"):
					raise _Failure("본 저장소에 스테이징된 변경이 있습니다")
				try:
					_git(repo, "merge", "--ff-only", "--no-overwrite-ignore", result["merge_commit"], reason="본 가지 빨리 감기 실패")
				except _Failure:
					forwarded = _git(repo, "rev-parse", "HEAD").strip() == result["merge_commit"]
					raise
				forwarded = True
				if (_git(repo, "rev-parse", "HEAD").strip() != result["merge_commit"]
						or _git(repo, "symbolic-ref", "-q", "HEAD").strip() != main_ref):
					raise _Failure("본 가지가 승인 뒤에 바뀌었습니다. 다시 검수하세요")
				if "push" in bundle["actions"]:
					_git(repo, "push", "origin", main_ref.removeprefix("refs/heads/"), reason="푸시 실패")
					result["pushed"] = True
					remote = _git(repo, "ls-remote", "origin", main_ref, reason="원격 해시 불일치").split()
					result["remote_matches"] = bool(remote) and remote[0] == _git(repo, "rev-parse", "HEAD").strip() == result["merge_commit"]
					if not result["remote_matches"]:
						raise _Failure("원격 해시 불일치")
				_append(identity, _report(bundle, stamp, result, forwarded))
				project.transition(identity, "완료")
				result["status"] = "완료"
			except (OSError, ValueError, RuntimeError, ConfigError, subprocess.SubprocessError) as exc:
				reason = str(exc) if isinstance(exc, _Failure) else "로컬 납품 처리 실패"
				reason = "납품 실패: " + reason + "; " + _facts(forwarded, result)
				project.transition(identity, "막힘", reason=reason)
				result["status"] = "막힘"
				try:
					_append(identity, _report(bundle, stamp, result, forwarded) + "\n- 이유: " + reason)
				except (OSError, ValueError):
					project.transition(identity, "막힘", reason=reason + "; 보고서 기록 실패")
			return result


def _report(bundle: dict, stamp: str, result: dict, forwarded: bool) -> str:
	notice = "재기동 필요: " + result["restart_notice"] if result["restart_notice"] else "재기동 안내: 없음"
	return (f"## 납품\n\n- 승인 시각: {stamp}\n- 묶음 hash: {bundle['hash'][:12]}\n"
		f"- 상태: {result['status'] if result['status'] == '막힘' else '완료'}\n"
		f"- 병합 커밋: {result['merge_commit'] or '없음'}\n- {_facts(forwarded, result)}\n- {notice}")


def reject(identity: str, reason: str) -> dict:
	if not valid_text(reason, 1000):
		raise ValueError("반려 이유는 1~1000자여야 합니다")
	project._folder(identity)
	with locked("agency-delivery:" + identity):
		_waiting(identity)
		_read(project._folder(identity) / "report.md", missing="")
		row = project.transition(identity, "막힘", reason="납품 반려: " + reason)
		try:
			_append(identity, f"## 반려\n\n- 반려 시각: {now().isoformat(timespec='seconds')}\n- 이유: {reason}")
		except (OSError, ValueError):
			row = project.transition(identity, "막힘", reason=row["blocked_reason"] + "; 보고서 기록 실패")
		return row
