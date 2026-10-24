"""합성·실제 볼트의 읽기 전용 계획과 본문 없는 승인 보고서."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from collector.backfill_common import locked, now, numbered, private_write, sha
from collector.config import ConfigError, load_sources
from vaultfilter.store import Dictionary

from . import frontmatter, rules


def save(plan: dict) -> None:
	private_write(rules.base() / "r1-plan.json", json.dumps(plan, ensure_ascii=False, indent=2) + "\n")


COMPANY_KINDS = ("회사명", "서비스명", "호스트")


def dictionary(kinds: tuple[str, ...] | None = None) -> list[tuple[int, str]]:
	return [(index, entry["value"]) for index, entry in enumerate(Dictionary().entries, 1)
		if isinstance(entry.get("value"), str) and len(entry["value"]) >= 2 and (kinds is None or entry.get("kind") in kinds)]


def mask(text: str, values: list[tuple[int, str]]) -> str:
	for index, value in sorted(values, key=lambda pair: len(pair[1]), reverse=True):
		text = re.sub(re.escape(value), lambda _: f"[사전 항목 {index}]", text, flags=re.I)
	return text


def inline(value: str) -> str:
	return "`" + str(value).replace("\r", "\\r").replace("\n", "\\n").replace("`", "&#96;").replace("|", "\\|") + "`"


def plan_note(path: Path, vault: Path, dates, overrides, mapping, names) -> dict:
	rel = path.relative_to(vault)
	item = {"path": rel.as_posix(), "folder": rules.folder(rel), "status": "failed",
		"reason": "", "created_source": "없음", "possible_move_day": False}
	try:
		path = rules.target(vault, item["path"])
		data = path.read_bytes()
		header = frontmatter.split(data)
		existing = {frontmatter.key(line) for line in header.lines}
		digest = sha(data)
		day, source = rules.created(rel, digest, dates)
		chosen = {"type": rules.note_type(rel),
			"sensitivity": "company" if item["folder"] == "Company" else "personal", "created": day}
		override = overrides.get(item["path"], {})
		chosen.update(override)
		additions = {name: chosen[name] for name in frontmatter.KEYS if name not in existing and chosen[name] is not None}
		after, changes, unsupported = frontmatter.render(data, additions, mapping)
		# 결정 2 A의 확인 목록: 이번에 sensitivity를 새로 넣는 비회사 노트만, 회사·서비스·호스트 이름으로 찾는다
		text = unicodedata.normalize("NFC", item["path"] + "\n" + header.body.decode("utf-8")).casefold()
		matches = [index for index, value in names if unicodedata.normalize("NFC", value).casefold() in text] \
			if item["folder"] != "Company" and "sensitivity" in additions else []
		item.update(before=digest, after=sha(after), body_hash=sha(header.body), additions=additions,
			created_source=source, created_rule_date=day, created_overridden="created" in override,
			needs_type="type" not in existing and not chosen["type"],
			missing_created="created" not in existing and not chosen["created"],
			company_entries=matches, tag_changes=changes, unsupported_tags=unsupported,
			status="pending" if after != data else "unchanged")
	except (OSError, ValueError, ConfigError) as exc:
		# 예외의 입력 조각에는 본문이 들어갈 수 있어 그대로 저장하지 않는다.
		item["reason"] = str(exc) if isinstance(exc, frontmatter.NoteError) else "노트 읽기 또는 UTF-8 형식 오류"
	return item


def move_days(notes: list[dict]) -> None:
	old = [note for note in notes if note.get("created_source") in ("옛 볼트 경로", "옛 볼트 내용")
		and note.get("additions", {}).get("created") and not note.get("created_overridden")]
	months = Counter(note["additions"]["created"][:7] for note in old)
	for note in old:
		note["possible_move_day"] = months[note["additions"]["created"][:7]] >= 50


def render_report(plan: dict, values) -> str:
	notes = plan["notes"]
	counts = Counter(note["status"] for note in notes)
	choices = sum(bool(note.get("needs_type")) for note in notes)
	lines = ["# 볼트 재정리 R1 미리보기", "", f"작성: {plan['made']}", "",
		f"노트 {len(notes)}개 · 변경 예정 {counts['pending']}개 · 무변경 {counts['unchanged']}개 · 실패 {counts['failed']}개.", "",
		f"종류 선택 {choices}건은 아래 ‘종류 선택 필요’, 회사·서비스명 확인은 ‘회사·서비스명이 든 비회사 노트’에서 확인합니다.", "",
		"볼트는 바꾸지 않았습니다. 덮어쓰기를 수정하면 report를 다시 실행한 뒤 폴더 단위로 승인합니다.", "",
		f"보호 경로 건너뜀 {plan['skipped']['protected']}개(하위 탐색 안 함) · 제외 경로 {plan['skipped']['excluded']}개 · 링크 {plan['skipped']['symlink']}개.",
		"", "## 폴더별 승인", "", "| 폴더 | 변경 예정 | 회신 문구 |", "|---|---:|---|"]
	folders = Counter(note["folder"] for note in notes if note["status"] == "pending")
	for name, count in sorted(folders.items()):
		lines.append(f"| {inline(name)} | {count} | {inline('R1 ' + name + ' 반영해')} |")
	if not folders:
		lines.append("| 없음 | 0 | 승인 대상 없음 |")
	sections = [("종류 선택 필요", "needs_type"), ("created를 못 찾음", "missing_created"),
		("태그 형식 미지원", "unsupported_tags"), ("옮긴 날일 수 있음", "possible_move_day")]
	for title, field in sections:
		lines.extend(["", f"## {title}", ""])
		found = [note for note in notes if note.get(field)]
		lines.extend(["- " + inline(note["path"]) for note in found] or ["없음."])
	lines.extend(["", "## 회사·서비스명이 든 비회사 노트", "", "기본 sensitivity는 personal입니다. 덮어쓰기에서 선택한 노트만 company로 추가합니다.", ""])
	flagged = [note for note in notes if note.get("company_entries")]
	lines.extend([f"- {inline(note['path'])}: 사전 항목 " + ", ".join(map(str, note["company_entries"]))
		for note in flagged] or ["없음."])
	lines.extend(["", "## 태그 대응표", "", "| 원래 | 새 태그 | 노트 수 |", "|---|---|---:|"])
	pairs = Counter(pair for note in notes for pair in set(map(tuple, note.get("tag_changes", []))))
	for (old, new), count in sorted(pairs.items()):
		lines.append(f"| {inline(mask(old, values))} | {inline(mask(new, values))} | {count} |")
	if not pairs:
		lines.append("| 변경 없음 | 변경 없음 | 0 |")
	lines.extend(["", "## 노트별 계획", "", "| 경로 | 추가 키 | created 근거 | 상태·주의 |", "|---|---|---|---|"])
	for note in notes:
		added = ", ".join(f"{key}={value}" for key, value in note.get("additions", {}).items()) or "없음"
		origin = note["created_source"] + (" · 덮어쓰기 우선" if note.get("created_overridden") else "")
		state = {"pending": "반영 대기", "unchanged": "무변경", "failed": "실패"}[note["status"]]
		warning = note.get("reason") or ("옮긴 날일 수 있음" if note["possible_move_day"] else "")
		lines.append(f"| {inline(note['path'])} | {inline(mask(added, values))} | {origin} | {state} {warning} |")
	return "\n".join(lines) + "\n"


def report() -> Path:
	with locked("vaultreorg:r1"):
		vault = rules.vault_root(load_sources().vault)
		dates, overrides, mapping, values = rules.old_dates(), rules.overrides(), rules.tags_map(), dictionary()
		counts = {"protected": 0, "excluded": 0, "symlink": 0}
		names = dictionary(COMPANY_KINDS)
		notes = [plan_note(path, vault, dates, overrides, mapping, names) for path in rules.inventory(vault, counts)]
		move_days(notes)
		path = numbered(rules.base(), "r1-report")
		plan = {"version": 1, "vault": str(vault), "made": now().isoformat(), "report": str(path),
			"tag_map": mapping, "notes": notes, "skipped": counts}
		private_write(path, render_report(plan, values))
		save(plan)
		folders = sorted({note["folder"] for note in notes if note["status"] == "pending"})
		replies = " / ".join("'R1 " + name + " 반영해'" for name in folders) or "없음(반영 대상 없음)"
		print(f"노트 {len(notes)}개 · 변경 예정 {sum(note['status'] == 'pending' for note in notes)}개")
		print(f"보고서: {path} · 폴더별 회신 문구: {replies}")
		return path
