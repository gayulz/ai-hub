"""R4 읽기 전용 후보·AI 판정 보고서. 병합과 삭제는 제안만 한다."""

from __future__ import annotations

import json
import math
from collections import Counter

from collector.backfill_common import locked, now, private_write
from collector.config import ConfigError, home, load_plans, load_sources
from collector.ledger import Ledger

from . import r4_judge, r4_vectors, rules
from .r3 import state_file
from .report import inline


def save(plan: dict) -> None:
	private_write(state_file("r4-plan.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")


def render_report(plan: dict) -> str:
	groups = plan["groups"]
	lines = ["# 볼트 재정리 R4 주제 중복", "", f"작성: {plan['made']}", "",
		f"대상 {plan['target_count']}개 · 후보 묶음 {len(groups)}개 · "
		f"판정 성공 {sum(group['status'] == 'judged' for group in groups)}개 · "
		f"판정 실패 {sum(group['status'] == 'failed' for group in groups)}개.", "",
		f"기준 유사도 {plan['threshold']:.2f} · 최대 판정 묶음 {plan['max_groups']}개.", "",
		"볼트에는 쓰지 않았습니다. 링크 반영은 r4-apply-links로 별도 실행합니다. 병합·삭제는 실행하지 않습니다.", "",
		"## 유사도 분포", "", "모든 대상 쌍을 0.05 구간으로 셉니다. 상한은 제외하되 마지막 구간은 1.00을 포함합니다.", "",
		"| 코사인 구간 | 쌍 수 |", "|---|---:|"]
	lines.extend(f"| {item['lower']:.2f} ~ {item['upper']:.2f} | {item['pairs']} |" for item in plan["distribution"])
	lines.extend(["", "## 제외 집계", "", "| 이유 | 수 |", "|---|---:|"])
	lines.extend(f"| {reason} | {count} |" for reason, count in sorted(plan["skipped"].items()))
	lines.extend(["", "## 묶음별 판정", ""])
	for index, group in enumerate(groups, 1):
		value = group.get("judgment")
		lines.extend([f"### 묶음 {index} · 최고 유사도 {group['similarity']:.4f}", ""])
		lines.extend("- " + inline(note["path"]) for note in group["notes"])
		lines.extend(["", "| 노트 A | 노트 B | 유사도 |", "|---|---|---:|"])
		lines.extend(f"| {inline(pair['left'])} | {inline(pair['right'])} | {pair['similarity']:.4f} |" for pair in group["pairs"])
		if value:
			lines.extend(["", f"AI 판정: {value['relation']} · 권장 조치: {value['action']} · 요금제: {group['plan']}",
				"", "이유: " + inline(value["reason"]), ""])
		else:
			lines.extend(["", "판정 실패: " + group["reason"] if group["status"] == "failed" else "미판정: 호출 상한", ""])
	lines.extend(["## 병합 제안", "", "아래는 제안만 담습니다. 살릴 내용 후보는 다른 노트의 원문 앞부분 발췌입니다.", ""])
	for index, group in enumerate(groups, 1):
		value = group.get("judgment", {})
		if value.get("action") != "merge" and value.get("relation") != "duplicate":
			continue
		lines.extend([f"### 묶음 {index}", "", "유지 경로: " + inline(value["keep"] or "미지정"),
			"", "이유: " + inline(value["reason"]), ""])
		for note in group["notes"]:
			if note["path"] == value["keep"]:
				continue
			lines.append("- " + inline(note["path"]) + ": " + inline(" / ".join(note.get("excerpts", [])) or "본문 없음"))
		lines.append("")
	return "\n".join(lines) + "\n"


def report(threshold: float = .80, max_groups: int = 40):
	if type(threshold) not in (float, int) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
		raise ConfigError("R4 --threshold는 -1 이상 1 이하의 유한한 수여야 합니다")
	if type(max_groups) is not int or max_groups < 1:
		raise ConfigError("R4 --max-groups는 양의 정수여야 합니다")
	with locked("vaultreorg:r4"):
		vault = rules.vault_root(load_sources().vault)
		plans, cache = r4_judge.routing(load_plans()), r4_vectors.load_cache("r4-cache.json")
		skipped = Counter()
		notes = r4_vectors.collect(vault, skipped)
		groups, distribution = r4_vectors.group(notes, r4_vectors.vectors(notes), threshold)
		ledger = Ledger(home() / "ledger.db")
		try:
			failed, day = set(), now().date().isoformat()
			for index, group in enumerate(groups):
				group.update(status="limited", reason="호출 상한")
				if index < max_groups:
					entry, reason = r4_judge.judge(group["notes"], plans, ledger, day, cache, failed)
					r4_vectors.save_cache("r4-cache.json", cache)
					group.update(status="judged" if entry else "failed", reason=reason)
					if entry:
						group.update(entry)
				for note in group["notes"]:
					if group.get("judgment", {}).get("action") == "merge" or group.get("judgment", {}).get("relation") == "duplicate":
						note["excerpts"] = [line.strip()[:160] for line in note["body"].splitlines() if line.strip()][:2]
					for key in ("data", "body"):
						note.pop(key)
					note.update(status="pending", reason="")
		finally:
			ledger.close()
		plan = {"version": 4, "vault": str(vault), "made": now().isoformat(), "threshold": threshold,
			"max_groups": max_groups, "target_count": len(notes), "distribution": distribution,
			"groups": groups, "skipped": dict(skipped)}
		path = state_file("r4-report.md")
		private_write(path, render_report(plan))
		save(plan)
		print(f"R4 대상 {len(notes)}개 · 후보 묶음 {len(groups)}개 · 판정 실패 {sum(g['status'] == 'failed' for g in groups)}개")
		return path
