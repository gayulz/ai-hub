"""R4의 링크 제안만 바이트를 삽입하고, 경로 검사·잠금 후 10노트씩 로컬 커밋한다."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from collector import vault as vault_io
from collector.backfill_common import locked, sha
from collector.config import ConfigError, home, load_sources
from vaultsync import sync

from . import frontmatter, r3_select, r4, r4_judge, r4_vectors, rules
from .apply import git_paths, nfc
from .r3 import state_file

MARKER = "<!-- ai-hub:r4 -->"
HASH = re.compile(r"[a-f0-9]{64}")


def load_plan(vault: Path) -> dict:
	try:
		plan = json.loads(state_file("r4-plan.json").read_text(encoding="utf-8"))
		if plan["version"] != 4 or plan["vault"] != str(vault) or not isinstance(plan["groups"], list):
			raise ValueError
		seen = set()
		for group in plan["groups"]:
			if group["status"] not in ("judged", "failed", "limited") or not isinstance(group["notes"], list) \
				or not 2 <= len(group["notes"]) <= 6:
				raise ValueError
			for note in group["notes"]:
				rel = rules.relative(note["path"])
				if r3_select.path_reason(rel) or nfc(note["path"]) in seen or any(c in note["path"] for c in r4_vectors.LINK_UNSAFE):
					raise ValueError
				seen.add(nfc(note["path"]))
				if note["type"] not in r4_vectors.TYPES or note["status"] not in ("pending", "prepared", "applied", "skipped", "unchanged", "failed"):
					raise ValueError
				for field in ("before", "body_hash", "input_hash"):
					if not isinstance(note[field], str) or not HASH.fullmatch(note[field]):
						raise ValueError
				if note["status"] in ("prepared", "applied"):
					if not isinstance(note["after"], str) or not HASH.fullmatch(note["after"]) \
						or type(note["offset"]) is not int or note["offset"] < 0 or not isinstance(note["addition"], str) \
						or not isinstance(note["links"], list) or not note["links"] or any(not isinstance(link, str) for link in note["links"]):
						raise ValueError
					if not isinstance(note["times"], list) or len(note["times"]) != 2 or any(type(t) is not int for t in note["times"]):
						raise ValueError
				if note.get("commit") is not None and (not isinstance(note["commit"], str)
					or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", note["commit"])):
					raise ValueError
			if group["status"] == "judged" and r4_judge.validate(group.get("judgment"), group["notes"]) is None:
				raise ValueError
		return plan
	except (OSError, ValueError, KeyError, TypeError, ConfigError) as exc:
		raise ConfigError("R4 계획이 없거나 형식·볼트 경로가 다릅니다. r4-report를 다시 실행하세요") from exc


def labels(vault: Path) -> tuple[Counter, bool]:
	skipped = Counter()
	counts = Counter(nfc(path.stem) for path in r3_select.inventory(vault, skipped))
	# 탐색 금지 폴더 안의 동명 노트는 확인할 수 없으므로 경로를 붙여 모호성을 피한다.
	return counts, any(skipped[reason] for reason in ("보호 경로", "회사 폴더", "심볼릭 링크"))


def link_name(path: str, counts: Counter, uncertain: bool) -> str:
	value = Path(nfc(path))
	return value.with_suffix("").as_posix() if uncertain or counts[value.stem] != 1 else value.stem


def insert(data: bytes, links: list[str], aliases: dict[str, set[str]] | None = None) -> tuple[int, bytes]:
	header = frontmatter.split(data)
	text = header.body.decode("utf-8")
	existing = {nfc(value.split("|", 1)[0].split("#", 1)[0].removesuffix(".md"))
		for value in re.findall(r"\[\[([^\]\r\n]+)\]\]", text)}
	missing = [link for link in dict.fromkeys(links) if not existing.intersection((aliases or {}).get(link, {nfc(link)}))]
	if not missing:
		return len(data), b""
	nl, offset, end, section, fence = header.newline.encode(), len(data) - len(header.body), len(data), False, b""
	section_start = offset
	for line in header.body.splitlines(keepends=True):
		plain = line.rstrip(b"\r\n")
		mark = re.match(rb" {0,3}(`{3,}|~{3,})", plain)
		if mark:
			if not fence:
				fence = mark[1]
			elif mark[1][:1] == fence[:1] and len(mark[1]) >= len(fence) and not plain[mark.end():].strip():
				fence = b""
		elif not fence:
			if section and re.match(rb" {0,3}#{1,2}[ \t]+", plain):
				end = offset
				break
			if re.fullmatch(rb" {0,3}##[ \t]+" + "관련 노트".encode() + rb"[ \t]*(?:#+[ \t]*)?", plain):
				section, section_start = True, offset
				offset += len(line)
				continue
		offset += len(line)
	prefix = b"" if data[:end].endswith(nl * 2) else nl if data[:end].endswith(nl) else nl * 2
	rows = [] if section and MARKER.encode() in data[section_start:end] else [MARKER]
	if not section:
		rows.append("## 관련 노트")
	rows.extend(f"- [[{link}]]" for link in missing)
	addition = prefix + nl.join(row.encode("utf-8") for row in rows) + nl + (nl if end < len(data) else b"")
	return end, addition


def check_original(data: bytes, note: dict) -> None:
	fields = r3_select.fields(frontmatter.split(data))
	if fields.get("sensitivity") != "personal" or fields.get("type") != note["type"]:
		raise frontmatter.NoteError("R4 대상 조건이 달라짐")
	if sha(data) != note["before"] or sha(frontmatter.split(data).body) != note["body_hash"]:
		raise frontmatter.NoteError("보고서 이후 바뀜")


def check_applied(data: bytes, note: dict, peers: list[dict]) -> None:
	addition, offset = note["addition"].encode("utf-8"), note["offset"]
	if not addition or sha(data) != note["after"] or data[offset:offset + len(addition)] != addition:
		raise frontmatter.NoteError("반영 후 바뀜: 커밋 보류")
	before = data[:offset] + data[offset + len(addition):]
	check_original(before, note)
	allowed = {nfc(Path(peer["path"]).with_suffix("").as_posix()) for peer in peers if peer is not note}
	short = Counter(Path(value).name for value in allowed)
	allowed.update(value for value, count in short.items() if count == 1)
	if not set(note["links"]) <= allowed or insert(before, note["links"]) != (offset, addition):
		raise frontmatter.NoteError("R4 링크 삽입 계획 대조 실패")


def apply_note(vault, note, peers, counts, uncertain, plan) -> bool:
	path = r3_select.target(vault, note["path"])
	with locked("vault:" + str(path.absolute())):
		path = r3_select.target(vault, note["path"])
		stat = path.stat()
		times = (stat.st_atime_ns, stat.st_mtime_ns)
		try:
			current, reason = r4_vectors.read_note(vault, note["path"])
			if current is None:
				note.update(status="skipped", reason=reason)
				return False
			before = current["data"]
			if note["status"] in ("prepared", "applied") and sha(before) == note["after"]:
				check_applied(before, note, peers)
				note.update(status="applied", reason="")
				times = tuple(note["times"])
				return True
			try:
				check_original(before, note)
			except frontmatter.NoteError as exc:
				note.update(status="skipped", reason=str(exc))
				return False
			links = [link_name(peer["path"], counts, uncertain) for peer in peers if peer is not note]
			aliases = {link_name(peer["path"], counts, uncertain): {nfc(Path(peer["path"]).with_suffix("").as_posix()),
				link_name(peer["path"], counts, uncertain)} for peer in peers if peer is not note}
			offset, addition = insert(before, links, aliases)
			if not addition:
				note.update(status="unchanged", reason="기존 링크 있음")
				return False
			# 이미 있는 링크는 검증 때도 똑같이 제외하도록 실제 추가할 링크만 저장한다.
			links = re.findall(r"^- \[\[([^\]]+)\]\]", addition.decode("utf-8"), re.M)
			after = before[:offset] + addition + before[offset:]
			note.update(status="prepared", after=sha(after), offset=offset, addition=addition.decode("utf-8"),
				links=links, times=list(times), commit=None)
			check_applied(after, note, peers)
			temporary = path.with_name(f".{path.name}.tmp")
			if temporary.is_symlink() or temporary.exists():
				raise frontmatter.NoteError("기존 임시 파일이 있어 쓰기를 보류함")
			r4.save(plan)
			try:
				vault_io.atomic_write(path, after.decode("utf-8"))
				check_applied(path.read_bytes(), note, peers)
			except (OSError, ValueError) as exc:
				try:
					vault_io.atomic_write(path, before.decode("utf-8"))
					if path.read_bytes() != before:
						raise OSError
				except (OSError, ValueError):
					raise frontmatter.NoteError("반영 실패 및 원본 복원 실패: 수동 확인 필요") from exc
				raise frontmatter.NoteError("반영 후 대조 실패: 원본 복원함") from exc
			note.update(status="applied", reason="")
			return True
		finally:
			os.utime(r3_select.target(vault, note["path"]), ns=times, follow_symlinks=False)


def commit(vault: Path, notes: list[dict]) -> tuple[str, set[str]]:
	paths = git_paths(vault, notes)
	if not paths:
		return "", set()
	with tempfile.NamedTemporaryFile(prefix="r4-paths-", dir=rules.base(), delete=False) as listing:
		listing.write(b"\0".join(path.encode("utf-8") for path in paths) + b"\0")
		path = Path(listing.name)
	args = (f"--pathspec-from-file={path}", "--pathspec-file-nul")
	try:
		if sync.git(vault, "--literal-pathspecs", "add", *args).returncode != 0:
			raise RuntimeError("git add 실패: 노트는 유지했습니다")
		if sync.git(vault, "diff", "--cached", "--name-only", "-z").stdout.count(b"\0") != len(paths):
			raise RuntimeError("스테이징한 파일 수가 달라 커밋하지 않았습니다")
		selected = {nfc(value) for value in paths}
		if sync.git(vault, "commit", "-q", "-m", f"볼트 R4: 관련 노트 링크 {len(selected)}개 덧붙임").returncode != 0:
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


def finish_batch(vault, pending, plan) -> bool:
	ok, held = True, []
	with ExitStack() as locks:
		try:
			for note, peers in pending:
				path = r3_select.target(vault, note["path"])
				locks.enter_context(locked("vault:" + str(path.absolute())))
				held.append(note)
				current, _ = r4_vectors.read_note(vault, note["path"])
				if current is None:
					raise frontmatter.NoteError("커밋 전 대상 조건이 달라짐")
				check_applied(current["data"], note, peers)
			if sync.busy(vault):
				raise RuntimeError("커밋 전 Git 상태가 바뀌어 커밋을 보류했습니다")
			digest, paths = commit(vault, [note for note, _ in pending])
			for note, _ in pending:
				if nfc(note["path"]) in paths:
					note.update(commit=digest, reason="")
				else:
					note.update(local_only=True, reason="Git 경로 규칙으로 커밋 없이 로컬에만 반영")
			plan.pop("commit_error", None)
		except (OSError, ValueError, RuntimeError, ConfigError) as exc:
			plan["commit_error"] = str(exc) if isinstance(exc, (RuntimeError, frontmatter.NoteError)) else "커밋 전 접근 또는 잠금 실패"
			ok = False
		finally:
			for note in held:
				try:
					current, _ = r4_vectors.read_note(vault, note["path"])
					if current is not None and sha(current["data"]) == note["after"]:
						os.utime(r3_select.target(vault, note["path"]), ns=tuple(note["times"]), follow_symlinks=False)
					else:
						plan["commit_error"], ok = "반영 후 바뀜: 시각 복원 보류", False
				except (OSError, ValueError, ConfigError):
					plan["commit_error"], ok = "반영 뒤 시각 복원 실패: 수동 확인 필요", False
			r4.save(plan)
	if not ok:
		print(plan["commit_error"])
	return ok


def apply(limit: int | None = None) -> int:
	if limit is not None and (type(limit) is not int or limit < 1):
		raise ConfigError("R4 --limit은 양의 정수여야 합니다")
	with locked("vaultreorg:r4"):
		vault = rules.vault_root(load_sources().vault)
		plan = load_plan(vault)
		with locked("vaultsync", exact=home() / "run/vaultsync.lock"):
			if sync.busy(vault):
				print("R4 반영 거부: Git 작업·브랜치·직접 스테이징을 확인하세요")
				return 1
			counts, uncertain = labels(vault)
			pending, selected, written, failed, skipped = [], 0, 0, 0, 0
			for group in plan["groups"]:
				value = group.get("judgment", {})
				if group["status"] != "judged" or value["action"] != "link" or value["relation"] == "duplicate":
					continue
				peers, blocked = [], False
				for note in group["notes"]:
					try:
						path = r3_select.target(vault, note["path"])
						with locked("vault:" + str(path.absolute())):
							stat = path.stat()
							try:
								current, _ = r4_vectors.read_note(vault, note["path"])
							finally:
								if path.stat().st_mtime_ns == stat.st_mtime_ns:
									os.utime(r3_select.target(vault, note["path"]), ns=(stat.st_atime_ns, stat.st_mtime_ns), follow_symlinks=False)
						if current is not None:
							peers.append(note)
					except (OSError, ValueError, ConfigError):
						note["reason"] = "노트 접근·잠금 또는 형식 오류: 묶음 반영 보류"
						failed, blocked = failed + 1, True
				if blocked:
					r4.save(plan)
					continue
				for note in group["notes"]:
					if note["status"] in ("skipped", "unchanged") or note.get("commit") or note.get("local_only"):
						continue
					if limit is not None and selected >= limit:
						break
					if note not in peers or len(peers) < 2:
						note.update(status="skipped", reason="현재 개인 링크 대상 부족")
						skipped += 1
						r4.save(plan)
						continue
					try:
						was_applied = note["status"] in ("prepared", "applied")
						if apply_note(vault, note, peers, counts, uncertain, plan):
							pending.append((note, peers))
							selected += 1
							written += not was_applied
						else:
							skipped += 1
					except (OSError, ValueError, ConfigError) as exc:
						note.update(reason=str(exc) if isinstance(exc, frontmatter.NoteError) else "노트 접근·잠금 또는 쓰기 실패")
						failed += 1
					finally:
						r4.save(plan)
					if len(pending) == 10:
						if not finish_batch(vault, pending, plan):
							return 1
						pending = []
			if pending and not finish_batch(vault, pending, plan):
				return 1
			print(f"R4 링크 반영 {written}개 · 건너뜀 {skipped}개 · 실패 {failed}개")
			return 1 if failed else 0
