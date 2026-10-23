"""비밀값을 저장하지 않는 자동 점검과 사람의 나란히 비교 보고서."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import summarize
from .backfill_common import child, clean, now, packed, private_write, sha
from .config import ConfigError, home
from .redact import redact

NUMBERS = re.compile(r"(?<![\dA-Za-z])\d+(?:[.,:/-]\d+)*(?![\dA-Za-z])")
DATES = re.compile(r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일)(?!\d)")
JUDGMENTS = ("사실 왜곡", "결정·남은 일 보존", "코드 미포함", "한국어 가독성")


def material(prompt):
	return prompt.removeprefix(summarize.PROMPT_HEAD).split("\n", 1)[-1].removesuffix(summarize.PROMPT_TAIL)


def checks(prompt, text):
	body = material(prompt)
	result = {"secret_patterns_detected": redact(text) != text}
	output = clean(re.sub(r"```.*?(?:```|\Z)", "", text, flags=re.S).strip())
	result["secret_patterns_remaining"] = redact(output) != output
	for key, pattern in (("numbers", NUMBERS), ("dates", DATES)):
		original, kept = set(pattern.findall(body)), set(pattern.findall(output))
		result[key] = {"total": len(original), "preserved": len(original & kept),
			"ratio": len(original & kept) / len(original) if original else None}
	return result


def cell(text):
	return clean(str(text)).replace("&", "&amp;").replace("<", "&lt;").replace(
		">", "&gt;").replace("|", "&#124;").replace("\n", "<br>")


def ratio(value):
	if not value or value["ratio"] is None:
		return "해당 없음"
	return f"{value['preserved']}/{value['total']} ({value['ratio']:.0%})"


def report(store, bid, *, write=True):
	batch = store.batch(bid)
	scope = store.scope(batch["scope"])
	items = store.items(bid)
	requested = scope["data"]["requested"]
	completed = sum(i["state"] == "compared" for i in items)
	calls = store.rows("c4_calls", "batch=?", (bid,))
	today = store.rows("c4_calls", "day=?", (now().date().isoformat(),))
	local = sum(bool(c.get("local", 0)) for c in calls)
	subs_today = sum(not c.get("local", 0) for c in today)
	path = Path(batch["report"])
	if path != child(home(), f"backfill/compare-{bid}/report.md"):
		raise ConfigError("비교 보고서 경로가 전용 저장 위치와 다릅니다")
	previous = path.read_text(encoding="utf-8") if path.exists() else ""
	verdicts, key = {}, None
	for line in previous.splitlines():
		if line.startswith("## 대화 "):
			key = line.removeprefix("## 대화 ")
		elif key and any(line.startswith("| " + name + " |") for name in JUDGMENTS):
			verdicts.setdefault(key, []).append(line)
	state = "비교 완료" if batch["state"] == "compared" else "비교 미완료"
	text = (f"# C4 로컬·구독 비교 {bid}\n\n원천 ID: {scope['project']} · {state}\n\n"
		f"요청 {requested}건 · 선정 {len(items)}건 · 양쪽 완료 {completed}건 · 적격 부족 {requested - len(items)}건\n\n"
		f"고정 기간: {scope['since']} ~ {scope['until']}\n\n"
		f"구독 호출 {len(calls) - local}회 · 로컬 호출 {local}회 (실패·결과 불명 포함) · "
		f"당일 C4 구독 누계 {subs_today}회\n\n"
		f"재개: `python3 -m collector backfill --resume {bid}`\n\n"
		"볼트에는 쓰지 않습니다. 아래 사람 판정 칸은 로컬·구독 각각 직접 채웁니다.\n\n"
		"자동 수치·날짜 비율은 비밀값·코드를 제외한 비교 입력의 고유 표기 중 결과에 남은 비율입니다. "
		"표기가 달라진 날짜는 누락으로 셉니다. 비율만으로 사실 정확성을 판정하지 않습니다.\n")
	for reason, count in scope["data"].get("exclusions", {}).items():
		text += f"\n- 제외·부족 이유: {clean(reason)} · {count}건\n"
	for warning in scope["data"].get("warnings", []):
		text += f"\n- 목록 주의: {clean(warning)}\n"
	for item in items:
		task = item["data"]
		pair = task.get("comparison", {})
		text += f"\n## 대화 {item['key']}\n\n상태: {item['state']}\n"
		if task.get("hold_reason"):
			text += "\n보류 이유: " + clean(task["hold_reason"]) + "\n"
		if task.get("input"):
			text += "\n비교 입력(비밀값·코드 제외):\n\n" + "\n".join(
				"> " + cell(line) for line in material(task["input"]).splitlines()) + "\n"
		text += "\n| 항목 | 로컬 | 구독 |\n|---|---|---|\n"
		for label, field in (("결과", "summary"), ("실제 요금제", "plan"), ("모델", "model"), ("처리 시간(초)", "seconds")):
			text += f"| {label} | " + " | ".join(cell(pair.get(side, {}).get(field, "대기"))
				for side in ("local", "subscription")) + " |\n"
		for label, field in (("응답의 비밀값 패턴 감지(제거 전)", "secret_patterns_detected"),
				("저장 결과 비밀값 패턴 남음", "secret_patterns_remaining"), ("숫자 보존율", "numbers"), ("날짜 보존율", "dates")):
			values = [pair.get(side, {}).get("checks", {}).get(field) for side in ("local", "subscription")]
			values = [ratio(v) if field in ("numbers", "dates") else ("대기" if v is None else "있음" if v else "없음") for v in values]
			text += f"| {label} | " + " | ".join(values) + " |\n"
		text += "\n| 사람 판정 | 로컬 | 구독 |\n|---|---|---|\n"
		text += "\n".join(verdicts.get(item["key"], [f"| {name} |  |  |" for name in JUDGMENTS])) + "\n"
		if write:
			private_write(child(path.parent, sha(item["key"]) + ".json"), clean(packed({
				"key": item["key"], "state": item["state"], "input": task.get("input", ""),
				"results": pair, "reason": task.get("hold_reason", ""),
				"calls": [{"plan": c["plan"], "local": bool(c.get("local", 0)), "state": c["state"],
					"result": json.loads(c["result"]) if c["result"] else None}
					for c in store.calls(bid, item["key"])]})) + "\n")
	text += ("\n중대 왜곡·보안값 0건이고 나머지 충족이 20건 중 18건 이상이면 로컬 채택 후보\n\n"
		"20건 미만의 비교로는 이 기준을 충족했다고 표시하지 않습니다. 자동으로 채택하지 않습니다. "
		"사용자가 결과를 판정한 뒤 plans.toml의 backfill.routing에 로컬 요금제를 넣어 채택합니다.\n")
	if write:
		private_write(path, clean(text))
	print(f"묶음 {bid} · {state} · 양쪽 완료 {completed}/{requested}건 · 적격 부족 {requested - len(items)}건")
	for reason, count in scope["data"].get("exclusions", {}).items():
		print(f"제외·부족 이유: {clean(reason)} · {count}건")
	print(f"구독 호출 {len(calls) - local}회 · 로컬 호출 {local}회 · 당일 C4 구독 누계 {subs_today}회")
	print(f"비교 보고서: {path}")
	print(f"재개 명령: python3 -m collector backfill --resume {bid}")
	return str(path)
