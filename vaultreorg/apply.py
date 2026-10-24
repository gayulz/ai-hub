"""승인된 폴더의 계획만 재대조해 쓰고, 해당 파일만 로컬 커밋한다."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from collector import vault as vault_io
from collector.backfill_common import locked, sha
from collector.config import ConfigError, home, load_sources
from vaultsync import sync

from . import frontmatter, report, rules


def load_plan(vault: Path) -> dict:
	try:
		plan = json.loads((rules.base() / "r1-plan.json").read_text(encoding="utf-8"))
		if plan["version"] != 1 or plan["vault"] != str(vault) or not isinstance(plan["notes"], list):
			raise ValueError
		mapping = plan["tag_map"]
		if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str)
			or not k or not v or any(c in k + v for c in "\r\n\x00") for k, v in mapping.items()):
			raise ValueError
		seen = set()
		for note in plan["notes"]:
			path = rules.relative(note["path"])
			if rules.protected(path) or rules.EXCLUDED.intersection(path.parts) or note["path"] in seen:
				raise ValueError
			seen.add(note["path"])
			if note["folder"] != rules.folder(path) or note["status"] not in ("pending", "unchanged", "failed", "skipped", "applied"):
				raise ValueError
			if "after" not in note and note["status"] == "failed":
				continue
			for field in ("before", "after", "body_hash"):
				if not re.fullmatch(r"[a-f0-9]{64}", note[field]):
					raise ValueError
			if not isinstance(note["additions"], dict):
				raise ValueError
			for name, value in note["additions"].items():
				if name not in frontmatter.KEYS or not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]+", value):
					raise ValueError
				if name == "created" and not rules.iso_date(value):
					raise ValueError
				if name == "sensitivity" and value not in ("company", "personal"):
					raise ValueError
		return plan
	except (OSError, ValueError, KeyError, TypeError, ConfigError) as exc:
		raise ConfigError("R1 계획이 없거나 형식·볼트 경로가 다릅니다. report를 다시 실행하세요") from exc


def apply_note(vault: Path, note: dict, mapping: dict) -> bool:
	path = rules.target(vault, note["path"])
	with locked("vault:" + str(path.absolute())):
		path = rules.target(vault, note["path"])
		before = path.read_bytes()
		if note["status"] == "applied":
			if sha(before) != note["after"] or sha(frontmatter.split(before).body) != note["body_hash"]:
				note["reason"] = "반영 후 바뀜: 커밋 보류"
				return False
			note["reason"] = ""
			return True
		if sha(before) != note["before"]:
			note.update(status="skipped", reason="보고서 이후 바뀜")
			return False
		after, _, _ = frontmatter.render(before, note["additions"], mapping)
		body = frontmatter.split(before).body
		if sha(after) != note["after"] or sha(body) != note["body_hash"] or frontmatter.split(after).body != body:
			raise frontmatter.NoteError("계획 해시 또는 본문 바이트 대조 실패")
		temporary = path.with_name(f".{path.name}.tmp")
		if temporary.is_symlink() or temporary.exists():
			raise frontmatter.NoteError("기존 임시 파일이 있어 쓰기를 보류함")
		try:
			vault_io.atomic_write(path, after.decode("utf-8"))
			written = path.read_bytes()
			if sha(written) != note["after"] or frontmatter.split(written).body != body:
				raise frontmatter.NoteError("반영 후 해시 또는 본문 바이트 대조 실패")
		except (OSError, ValueError) as exc:
			try:
				vault_io.atomic_write(path, before.decode("utf-8"))
				if path.read_bytes() != before:
					raise OSError
			except (OSError, ValueError):
				raise frontmatter.NoteError("반영 실패 및 원본 복원 실패: 수동 확인 필요") from exc
			raise frontmatter.NoteError("반영 후 대조 실패: 원본 복원함") from exc
		note.update(status="applied", reason="", commit=None)
		return True


def nfc(path: str) -> str:
	return unicodedata.normalize("NFC", path)


def git_paths(vault: Path, notes: list[dict]) -> list[str]:
	"""반영한 노트를 git이 보고한 경로 그대로 돌려준다.

	git 색인은 한글 경로를 NFD로 갖고 있을 수 있고, pathspec 파일의 글자 배열이 다르면 git은 오류 없이
	건너뛴다(10-24 Career 88개). 그래서 상태 목록의 경로를 NFC로 맞춰 짝을 찾고, 넘길 때는 원래 바이트를 쓴다.
	"""
	wanted = {nfc(note["path"]) for note in notes}
	found = [path for path in sync.changes(vault) if nfc(path) in wanted]
	missing = wanted - {nfc(path) for path in found}
	if missing:
		listed = "\0".join(note["path"] for note in notes if nfc(note["path"]) in missing) + "\0"
		ignored = subprocess.run(["git", "-C", str(vault), "check-ignore", "-z", "--stdin"], input=listed.encode("utf-8"),
			capture_output=True).stdout.decode("utf-8").split("\0")
		if missing - {nfc(path) for path in ignored if path}:
			raise RuntimeError(f"git 상태에서 찾지 못한 노트가 있어 커밋을 보류했습니다: {len(missing)}개")
		print(f"Git 무시 목록이라 커밋하지 않은 노트 {len(missing)}개")
	return found


def commit(vault: Path, notes: list[dict]) -> str:
	paths = git_paths(vault, notes)
	if not paths:
		return sync.git(vault, "rev-parse", "HEAD").stdout.decode().strip()
	with tempfile.NamedTemporaryFile(prefix="r1-paths-", dir=rules.base(), delete=False) as listing:
		listing.write(b"\0".join(path.encode("utf-8") for path in paths) + b"\0")
		path = Path(listing.name)
	args = (f"--pathspec-from-file={path}", "--pathspec-file-nul")
	try:
		if sync.git(vault, "--literal-pathspecs", "add", *args).returncode != 0:
			raise RuntimeError("git add 실패: 필터 설정을 확인하세요")
		staged = sync.git(vault, "diff", "--cached", "--name-only", "-z").stdout.count(b"\0")
		if staged != len(paths):
			raise RuntimeError(f"스테이징한 파일 수({staged})가 반영한 노트 수({len(paths)})와 달라 커밋하지 않았습니다")
		# 같은 노트가 NFD 삭제 + NFC 추가 두 줄로 잡힐 수 있어 노트 수는 NFC 경로로 센다
		message = f"볼트 재정리 R1: 머리말 정리 · 파일 {len({nfc(path) for path in paths})}개"
		if sync.git(vault, "commit", "-q", "-m", message).returncode != 0:
			raise RuntimeError("git commit 실패: 노트는 유지했습니다")
		left = {nfc(path) for path in sync.changes(vault)} & {nfc(path) for path in paths}
		if left:
			raise RuntimeError(f"커밋 뒤에도 바뀐 채 남은 노트가 있습니다: {len(left)}개")
		result = sync.git(vault, "rev-parse", "HEAD")
		if result.returncode != 0:
			raise RuntimeError("커밋 해시 확인 실패")
		return result.stdout.decode().strip()
	except (OSError, RuntimeError):
		reset = sync.git(vault, "--literal-pathspecs", "reset", "-q", *args)
		if reset.returncode != 0:
			raise RuntimeError("Git 실패 및 스테이징 복원 실패: 수동 확인 필요")
		raise
	finally:
		path.unlink(missing_ok=True)


def apply(folder: str) -> int:
	with locked("vaultreorg:r1"):
		vault = rules.vault_root(load_sources().vault)
		plan = load_plan(vault)
		selected = [note for note in plan["notes"] if note["folder"] == folder]
		if not selected:
			raise ConfigError("계획에 없는 폴더입니다. 보고서의 회신 문구를 확인하세요")
		with locked("vaultsync", exact=home() / "run/vaultsync.lock"):
			reason = sync.busy(vault)
			if reason:
				print("반영 거부: " + report.mask(reason, report.dictionary()))
				return 1
			pending, failed, skipped, written = [], 0, 0, 0
			for note in selected:
				if note["status"] == "unchanged" or (note["status"] == "applied" and note.get("commit")):
					continue
				if "after" not in note:
					failed += 1
					continue
				try:
					was_applied = note["status"] == "applied"
					if apply_note(vault, note, plan["tag_map"]):
						pending.append(note)
						written += not was_applied
					else:
						skipped += 1
						failed += note["status"] == "applied"
				except (OSError, ValueError, ConfigError) as exc:
					note.update(status="failed", reason=str(exc) if isinstance(exc, frontmatter.NoteError) else "노트 접근 또는 쓰기 실패")
					failed += 1
				finally:
					report.save(plan)
			if pending:
				try:
					# 쓰는 동안 사람이 새로 스테이징했으면 함께 커밋하지 않는다.
					reason = sync.busy(vault)
					if reason:
						raise RuntimeError("커밋 전 Git 상태가 바뀌어 커밋을 보류했습니다")
					digest = commit(vault, pending)
					for note in pending:
						note["commit"] = digest
					plan.pop("commit_error", None)
				except (OSError, RuntimeError) as exc:
					plan["commit_error"] = str(exc) if isinstance(exc, RuntimeError) else "Git 실행 실패"
					print(plan["commit_error"])
					failed += 1
				finally:
					report.save(plan)
			print(f"반영 {written}개 · 건너뜀 {skipped}개 · 실패 {failed}개")
			return 1 if failed else 0
