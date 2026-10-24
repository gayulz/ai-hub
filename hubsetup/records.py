"""기존 설정을 보존하는 훅·전역 작업 기록 규칙 설치물."""

from __future__ import annotations

import json
import shlex

from collector.hooks import EVENTS

from .config import Context, SetupError
from .templates import Item, current

TARGETS = tuple((tool, kind) for tool in EVENTS for kind in ("hooks", "rules"))
PATHS = {
	"claude": (".claude/settings.local.json", ".claude/rules/user/ai-hub-record.md"),
	"codex": (".codex/hooks.json", ".codex/AGENTS.md"),
	"gemini": (".gemini/settings.json", ".gemini/GEMINI.md"),
}
CLAUDE_HOOK_PATHS = (".claude/settings.json", PATHS["claude"][0])
START, END = "<!-- ai-hub:record -->", "<!-- /ai-hub:record -->"
RULE = """## ai-hub 작업 기록
파일을 바꾸거나 결정을 내린 작업을 마칠 때, 마지막 답 끝에 아래 블록을 남긴다. 간단한 질문·답에는 남기지 않는다.
## 작업 기록
- 결정: (무엇을 정했나)
- 이유: (왜)
- 실패·막힌 것: (있으면)
- 남은 일: (있으면)
비밀값·토큰·이메일·내부 주소는 쓰지 않는다.
"""


def hook_command(ctx: Context, tool: str) -> str:
	values = {"PYTHONPATH": ctx.repo, "AI_HUB_HOME": ctx.hub_home,
		"AI_HUB_COLLECTOR_LABEL": f"{ctx.label_prefix}.ai-hub-collector"}
	return " ".join(f"{name}={shlex.quote(str(value))}" for name, value in values.items()) + " " + shlex.quote(str(ctx.python)) + f" -m collector hook {tool}"


def owned(hook) -> bool:
	return isinstance(hook, dict) and isinstance(hook.get("command"), str) and "-m collector hook" in hook["command"]


def unique_fields(pairs) -> dict:
	data = {}
	for key, value in pairs:
		if key in data:
			raise ValueError
		data[key] = value
	return data


def reject_constant(_value):
	raise ValueError


def claude_config(text: str) -> dict:
	try:
		data = json.loads(text, object_pairs_hook=unique_fields, parse_constant=reject_constant)
		if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
			raise ValueError
		for event, groups in data.get("hooks", {}).items():
			if not isinstance(groups, list):
				raise ValueError
			for group in groups:
				if (not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
						or not isinstance(group.get("matcher", ""), str)):
					raise ValueError
				for hook in group["hooks"]:
					if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
						raise ValueError
					if ("command" in hook or hook["type"] == "command") and not isinstance(hook.get("command"), str):
						raise ValueError
					if "timeout" in hook and type(hook["timeout"]) not in (int, float):
						raise ValueError
			if event in EVENTS["claude"] and sum(owned(hook) for group in groups for hook in group["hooks"]) > 1:
				raise SetupError("Claude의 같은 필수 사건에 ai-hub 훅이 중복되어 있습니다. 기존 설정을 직접 확인하세요.")
		return data
	except (ValueError, TypeError):
		raise SetupError("Claude 설정의 JSON·훅·사건·그룹 형식을 확인하세요. 기존 설정은 변경하지 않았습니다.") from None


def claude_hooks(ctx: Context) -> Item:
	items, managed = [], []
	for name in CLAUDE_HOOK_PATHS:
		path = ctx.user_home / name
		before, mode = current(Item(path, "", 0o600))
		data = claude_config(before if mode is not None else "{}")
		items.append(Item(path, before, mode if mode is not None else 0o600))
		managed.append(any(owned(hook) for groups in data.get("hooks", {}).values()
			for group in groups for hook in group["hooks"]))
	if all(managed):
		raise SetupError("Claude 전역·로컬 설정 모두에 ai-hub 훅이 있습니다. 기존 설정을 직접 확인하세요.")
	return items[0] if managed[0] else items[1]


def merge_hooks(before: str, tool: str, command: str) -> tuple[str, tuple[str, str]]:
	try:
		if tool == "claude":
			data = claude_config(before or "{}")
		else:
			data = json.loads(before) if before else {}
		if not isinstance(data, dict):
			raise ValueError
		hooks = data.setdefault("hooks", {})
		if not isinstance(hooks, dict):
			raise ValueError
		old, new, changed = {}, {}, False
		for event in EVENTS[tool]:
			groups = hooks.setdefault(event, [])
			if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list)
					or any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
				raise ValueError
			managed = [hook for group in groups for hook in group["hooks"] if owned(hook)]
			old[event] = len(managed)
			if not managed:
				group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
				if any("matcher" in g for g in groups) or event in ("PreToolUse", "BeforeTool"):
					group = {"matcher": "", **group}
				groups.append(group)
				changed = True
			new[event] = sum(owned(hook) for group in groups for hook in group["hooks"])
		encode = lambda value: json.dumps(value, ensure_ascii=False, indent="\t") + "\n"
		return (encode(data) if changed else before), (encode(old), encode(new))
	except (ValueError, TypeError, AttributeError):
		raise SetupError("훅 설정의 JSON·사건 목록 형식을 확인하세요. 기존 설정은 변경하지 않았습니다.") from None


def merge_rule(before: str, tool: str) -> tuple[str, tuple[str, str]]:
	block = RULE if tool == "claude" else START + "\n" + RULE + END + "\n"
	if tool == "claude":
		if before and before != RULE:
			raise SetupError("ai-hub 작업 기록 전용 파일에 다른 내용이 있습니다. 직접 확인하세요.")
		return block, (before, block)
	if START in before or END in before:
		if before.count(START) != 1 or before.count(END) != 1 or before.index(START) > before.index(END):
			raise SetupError("ai-hub 작업 기록 표시 블록이 깨졌습니다. 기존 규칙은 변경하지 않았습니다.")
		current_block = before[before.index(START):before.index(END) + len(END)]
		if current_block != block.rstrip("\n"):
			raise SetupError("기존 ai-hub 작업 기록 규칙이 다릅니다. 직접 확인하세요.")
		return before, (block, block)
	return before + ("\n\n" if before and not before.endswith("\n\n") else "") + block, ("", block)


def render_record(ctx: Context, tool: str, kind: str) -> Item:
	if tool == "claude" and kind == "hooks":
		item = claude_hooks(ctx)
		path, before, mode = item.path, item.text, item.mode
	else:
		path = ctx.user_home / PATHS[tool][0 if kind == "hooks" else 1]
		before, mode = current(Item(path, "", 0o600))
	if kind == "hooks":
		text, preview = merge_hooks(before, tool, hook_command(ctx, tool))
	else:
		text, preview = merge_rule(before, tool)
	return Item(path, text, mode if mode is not None else 0o600, preview=preview)


def valid_hooks(ctx: Context, tool: str, text: str) -> bool:
	if tool == "claude":
		try:
			data = claude_config(text)
		except SetupError:
			return False
		for event in EVENTS[tool]:
			managed = [(group, hook) for group in data.get("hooks", {}).get(event, [])
				for hook in group["hooks"] if owned(hook)]
			if len(managed) != 1:
				return False
			group, hook = managed[0]
			if (group.get("matcher", "") not in ("", "*", ".*") or hook.get("type") != "command"
					or hook["command"] != hook_command(ctx, tool) or hook.get("timeout") != 5):
				return False
		return True
	data = json.loads(text)
	return all(any(group.get("matcher", "") in ("", "*", ".*") and any(
		hook.get("type") == "command" and hook.get("command") == hook_command(ctx, tool)
		and hook.get("timeout") == 5 for hook in group["hooks"])
		for group in data["hooks"][event]) for event in EVENTS[tool])
