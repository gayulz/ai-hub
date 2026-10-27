"""R3 미리보기: 볼트는 읽고, 기존 상태 폴더에 계획·보고서·캐시만 쓴다."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from collector.backfill_common import child, locked, now, private_write, sha
from collector.config import ConfigError, home, load_plans, load_sources
from collector.ledger import Ledger

from . import frontmatter, r3_select, r3_text, rules
from .report import inline


def state_file(name: str) -> Path:
	base = rules.base()
	if not base.is_dir():
		raise ConfigError("기존 vaultreorg 상태 폴더가 필요합니다")
	return child(base, name)


def save(plan: dict) -> None:
	private_write(state_file("r3-plan.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")


def load_cache() -> dict:
	path = state_file("r3-cache.json")
	if not path.exists():
		return {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		if data["version"] != 3 or not isinstance(data["entries"], dict) \
			or any(not isinstance(value, dict) for value in data["entries"].values()):
			raise ValueError
		return data["entries"]
	except (ValueError, KeyError, TypeError) as exc:
		raise ConfigError("R3 캐시 형식 오류") from exc


def render_report(plan: dict) -> str:
	notes = plan["notes"]
	targets = [note for note in notes if note.get("kind")]
	ready = [note for note in notes if note["status"] == "pending"]
	counts = Counter(note["type"] for note in targets)
	lines = ["# 볼트 재정리 R3 미리보기", "", f"작성: {plan['made']}", "",
		f"대상 {len(targets)}개 · 반영 예정 {len(ready)}개 · 건너뜀 {sum(plan['skipped'].values())}건.", "",
		"볼트에 쓰지 않았습니다. 보호·숨김·회사 폴더는 하위 탐색 없이 경로 수로 셉니다.", "",
		"## 종류별 대상 수", "", "| 종류 | 대상 수 | 반영 예정 |", "|---|---:|---:|"]
	for kind, count in sorted(counts.items()):
		lines.append(f"| {kind} | {count} | {sum(note['type'] == kind for note in ready)} |")
	if not counts:
		lines.append("| 없음 | 0 | 0 |")
	lines.extend(["", "## 건너뛴 이유", "", "| 이유 | 수 |", "|---|---:|"])
	lines.extend(f"| {reason} | {count} |" for reason, count in sorted(plan["skipped"].items()))
	if not plan["skipped"]:
		lines.append("| 없음 | 0 |")
	lines.extend(["", "## 노트별 제안 문구", ""])
	for note in ready:
		lines.extend(["### " + inline(note["path"]), ""])
		if note["kind"] == "study":
			lines.extend(["> [!summary] 3줄 요약", *("> " + line for line in note["proposal"]["lines"])])
		else:
			lines.extend(["> [!abstract] 정리", *(f"> - **{label}** {note['proposal'][key]}"
				for key, label in r3_text.LABELS.items() if key in note["proposal"])])
		lines.append("")
	return "\n".join(lines) + "\n"


def plan_note(vault, path, plans, ledger, day, cache, failed, company_names) -> dict:
	note = {"path": path.relative_to(vault).as_posix(), "status": "skipped", "reason": ""}
	try:
		data, reason = r3_select.read_personal(vault, note["path"])
		kind, note_type, reason = r3_select.choose(data) if data is not None else (None, "", reason)
	except (OSError, ValueError, ConfigError):
		note["reason"] = "읽기 또는 형식 오류"
		return note
	if not kind:
		note["reason"] = reason
		return note
	note.update(kind=kind, type=note_type)
	body = frontmatter.split(data).body
	entry, reason = r3_text.propose(kind, body.decode("utf-8"), plans, ledger, day, cache, failed)
	private_write(state_file("r3-cache.json"), json.dumps({"version": 3, "entries": cache}, ensure_ascii=False) + "\n")
	if entry is not None:
		try:
			value = json.loads(entry["text"])
		except ValueError:
			value = None
		proposal = r3_text.validate(kind, value, body.decode("utf-8"), company_names)
		if proposal is None:
			reason = "검증 실패"
		else:
			after = r3_text.insert(data, kind, proposal)
			note.update(status="pending", before=sha(data), after=sha(after), body_hash=sha(body),
				proposal=proposal, plan=entry["plan"])
	note["reason"] = reason
	return note


def report() -> Path:
	with locked("vaultreorg:r3"):
		vault = rules.vault_root(load_sources().vault)
		plans, cache, company_names = r3_text.routing(load_plans()), load_cache(), r3_text.names()
		counts, notes, failed = Counter(), [], set()
		ledger = Ledger(home() / "ledger.db")
		try:
			day = now().date().isoformat()
			for path in r3_select.inventory(vault, counts):
				note = plan_note(vault, path, plans, ledger, day, cache, failed, company_names)
				notes.append(note)
				if note["status"] == "skipped":
					counts[note["reason"]] += 1
		finally:
			ledger.close()
		plan = {"version": 3, "vault": str(vault), "made": now().isoformat(), "notes": notes, "skipped": dict(counts)}
		path = state_file("r3-report.md")
		private_write(path, render_report(plan))
		save(plan)
		print(f"R3 대상 {sum(bool(note.get('kind')) for note in notes)}개 · 반영 예정 {sum(note['status'] == 'pending' for note in notes)}개")
		return path
