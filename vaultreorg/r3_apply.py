"""R3 계획을 재대조해 덧붙이고, 기존 Git 경로 검사로 10개씩 커밋한다."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path

from collector import vault as vault_io
from collector.backfill_common import locked, sha
from collector.config import ConfigError, home, load_sources
from vaultsync import sync

from . import frontmatter, r3, r3_select, r3_text, rules
from .apply import git_paths, nfc


def load_plan(vault: Path) -> dict:
	try:
		plan = json.loads(r3.state_file("r3-plan.json").read_text(encoding="utf-8"))
		if plan["version"] != 3 or plan["vault"] != str(vault) or not isinstance(plan["notes"], list):
			raise ValueError
		seen = set()
		for note in plan["notes"]:
			rel = rules.relative(note["path"])
			key = nfc(rel.as_posix())
			if r3_select.path_reason(rel) or key in seen:
				raise ValueError
			seen.add(key)
			if note["status"] not in ("pending", "skipped", "applied", "failed"):
				raise ValueError
			if note["status"] == "skipped" and "proposal" not in note:
				continue
			if note["kind"] not in ("study", "outline") or note["type"] not in r3_select.TYPES \
				or (note["kind"] == "study") != (note["type"] == "study") or not isinstance(note["proposal"], dict):
				raise ValueError
			parts = r3_text.items(note["kind"], note["proposal"])
			if parts is None or any(not isinstance(line, str) for _, line in parts):
				raise ValueError
			for field in ("before", "after", "body_hash"):
				if not isinstance(note[field], str) or not re.fullmatch(r"[a-f0-9]{64}", note[field]):
					raise ValueError
			if "times" in note and (not isinstance(note["times"], list) or len(note["times"]) != 2
				or any(type(value) is not int for value in note["times"])):
				raise ValueError
			if note.get("commit") is not None and (not isinstance(note["commit"], str)
				or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", note["commit"])):
				raise ValueError
		return plan
	except (OSError, ValueError, KeyError, TypeError, ConfigError) as exc:
		raise ConfigError("R3 계획이 없거나 형식·볼트 경로가 다릅니다. r3-report를 다시 실행하세요") from exc


def check_original(data: bytes, note: dict, company_names: list[str]) -> None:
	body = frontmatter.split(data).body
	kind, note_type, reason = r3_select.choose(data)
	if sha(data) != note["before"] or sha(body) != note["body_hash"]:
		raise frontmatter.NoteError("보고서 이후 바뀜")
	if reason or kind != note["kind"] or note_type != note["type"]:
		raise frontmatter.NoteError("R3 대상 조건이 달라짐")
	proposal = r3_text.validate(kind, note["proposal"], body.decode("utf-8"), company_names)
	if proposal != note["proposal"]:
		raise frontmatter.NoteError("검증 실패")


def check_applied(data: bytes, note: dict, company_names: list[str]) -> None:
	header = frontmatter.split(data)
	addition = r3_text.block(data, note["kind"], note["proposal"])
	if sha(data) != note["after"] or not header.body.startswith(addition):
		raise frontmatter.NoteError("반영 후 바뀜: 커밋 보류")
	offset = len(data) - len(header.body)
	before = data[:offset] + data[offset + len(addition):]
	check_original(before, note, company_names)


def apply_note(vault: Path, note: dict, company_names: list[str]) -> bool:
	path = r3_select.target(vault, note["path"])
	with locked("vault:" + str(path.absolute())):
		path = r3_select.target(vault, note["path"])
		original = path.stat()
		before, reason = r3_select.read_personal(vault, note["path"])
		if before is None:
			note.update(status="skipped", reason=reason)
			return False
		times = (original.st_atime_ns, original.st_mtime_ns)
		try:
			if note["status"] == "applied":
				check_applied(before, note, company_names)
				return True
			if r3_select.MARKER.encode() in before:
				note.update(status="skipped", reason="이미 R3 표식")
				return False
			if sha(before) != note["before"] or sha(frontmatter.split(before).body) != note["body_hash"]:
				note.update(status="skipped", reason="보고서 이후 바뀜")
				return False
			check_original(before, note, company_names)
			after = r3_text.insert(before, note["kind"], note["proposal"])
			check_applied(after, note, company_names)
			temporary = path.with_name(f".{path.name}.tmp")
			if temporary.is_symlink() or temporary.exists():
				raise frontmatter.NoteError("기존 임시 파일이 있어 쓰기를 보류함")
			try:
				vault_io.atomic_write(path, after.decode("utf-8"))
				check_applied(path.read_bytes(), note, company_names)
			except (OSError, ValueError) as exc:
				try:
					vault_io.atomic_write(path, before.decode("utf-8"))
					if path.read_bytes() != before:
						raise OSError
				except (OSError, ValueError):
					raise frontmatter.NoteError("반영 실패 및 원본 복원 실패: 수동 확인 필요") from exc
				raise frontmatter.NoteError("반영 후 대조 실패: 원본 복원함") from exc
			note.update(status="applied", reason="", commit=None, times=list(times))
			return True
		finally:
			os.utime(r3_select.target(vault, note["path"]), ns=times, follow_symlinks=False)


def commit(vault: Path, notes: list[dict]) -> tuple[str, set[str]]:
	paths = git_paths(vault, notes)
	if not paths:
		return "", set()
	with tempfile.NamedTemporaryFile(prefix="r3-paths-", dir=rules.base(), delete=False) as listing:
		listing.write(b"\0".join(path.encode("utf-8") for path in paths) + b"\0")
		path = Path(listing.name)
	args = (f"--pathspec-from-file={path}", "--pathspec-file-nul")
	try:
		if sync.git(vault, "--literal-pathspecs", "add", *args).returncode != 0:
			raise RuntimeError("git add 실패: 노트는 유지했습니다")
		staged = sync.git(vault, "diff", "--cached", "--name-only", "-z").stdout.count(b"\0")
		if staged != len(paths):
			raise RuntimeError("스테이징한 파일 수가 달라 커밋하지 않았습니다")
		selected = {nfc(value) for value in paths}
		message = f"볼트 R3: 본문 요약·정리 {len(selected)}개 덧붙임"
		if sync.git(vault, "commit", "-q", "-m", message).returncode != 0:
			raise RuntimeError("git commit 실패: 노트는 유지했습니다")
		if {nfc(value) for value in sync.changes(vault)} & selected:
			raise RuntimeError("커밋 뒤에도 바뀐 노트가 있어 확인이 필요합니다")
		result = sync.git(vault, "rev-parse", "HEAD")
		if result.returncode != 0:
			raise RuntimeError("커밋 해시 확인 실패")
		return result.stdout.decode().strip(), selected
	except (OSError, RuntimeError):
		if sync.git(vault, "--literal-pathspecs", "reset", "-q", *args).returncode != 0:
			raise RuntimeError("Git 실패 및 스테이징 복원 실패: 수동 확인 필요")
		raise
	finally:
		path.unlink(missing_ok=True)


def finish_batch(vault: Path, pending: list[dict], plan: dict, company_names: list[str]) -> bool:
	ok, held = True, []
	with ExitStack() as locks:
		try:
			for note in pending:
				path = r3_select.target(vault, note["path"])
				locks.enter_context(locked("vault:" + str(path.absolute())))
				held.append(note)
				data, _ = r3_select.read_personal(vault, note["path"])
				if data is None:
					raise frontmatter.NoteError("커밋 전 대상 조건이 달라짐")
				check_applied(data, note, company_names)
			if sync.busy(vault):
				raise RuntimeError("커밋 전 Git 상태가 바뀌어 커밋을 보류했습니다")
			digest, paths = commit(vault, pending)
			for note in pending:
				if nfc(note["path"]) in paths:
					note.update(commit=digest, reason="")
				else:
					# 사전 이름·무시 목록 경로는 R1 과 같이 볼트 Git 에 올리지 않고 로컬에만 둔다
					note.update(local_only=True, reason="Git 경로 규칙으로 커밋 없이 로컬에만 반영")
			plan.pop("commit_error", None)
		except (OSError, ValueError, RuntimeError, ConfigError) as exc:
			plan["commit_error"] = str(exc) if isinstance(exc, (RuntimeError, frontmatter.NoteError)) else "커밋 전 접근 또는 잠금 실패"
			ok = False
		finally:
			for note in held:
				if "times" not in note:
					continue
				try:
					data, _ = r3_select.read_personal(vault, note["path"])
					if data is not None and sha(data) == note["after"]:
						os.utime(r3_select.target(vault, note["path"]), ns=tuple(note["times"]), follow_symlinks=False)
					else:
						plan["commit_error"], ok = "반영 후 바뀜: 시각 복원 보류", False
				except (OSError, ValueError, ConfigError):
					plan["commit_error"], ok = "반영 뒤 시각 복원 실패: 수동 확인 필요", False
			r3.save(plan)
	if not ok:
		print(plan["commit_error"])
	return ok


def apply(limit: int | None = None) -> int:
	if limit is not None and (type(limit) is not int or limit < 1):
		raise ConfigError("R3 --limit은 양의 정수여야 합니다")
	with locked("vaultreorg:r3"):
		vault = rules.vault_root(load_sources().vault)
		plan, company_names = load_plan(vault), r3_text.names()
		with locked("vaultsync", exact=home() / "run/vaultsync.lock"):
			if sync.busy(vault):
				print("R3 반영 거부: Git 작업·브랜치·직접 스테이징을 확인하세요")
				return 1
			pending, written, skipped, failed, selected = [], 0, 0, 0, 0
			for note in plan["notes"]:
				if note["status"] == "skipped" or note.get("commit") or note.get("local_only"):
					continue
				if limit is not None and selected >= limit:
					break
				try:
					was_applied = note["status"] == "applied"
					if apply_note(vault, note, company_names):
						pending.append(note)
						selected += 1
						written += not was_applied
					else:
						skipped += 1
				except (OSError, ValueError, ConfigError) as exc:
					note.update(status="applied" if was_applied else "failed",
						reason=str(exc) if isinstance(exc, frontmatter.NoteError) else "노트 접근·잠금 또는 쓰기 실패")
					failed += 1
				finally:
					r3.save(plan)
				if len(pending) == 10:
					if not finish_batch(vault, pending, plan, company_names):
						return 1
					pending = []
			if pending and not finish_batch(vault, pending, plan, company_names):
				return 1
			print(f"R3 반영 {written}개 · 건너뜀 {skipped}개 · 실패 {failed}개")
			return 1 if failed else 0
