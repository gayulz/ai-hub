"""템플릿 렌더링과 설치물 비교. 이 모듈은 파일을 쓰지 않는다."""

from __future__ import annotations

import plistlib
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from string import Template
from xml.sax.saxutils import escape

from .config import Context, SetupError

TEMPLATES = Path(__file__).with_name("templates")
MODULES = ("collector", "vaultsync", "backup")


@dataclass(frozen=True)
class Item:
	path: Path
	text: str
	mode: int
	label: str | None = None


def shell(value: str) -> str:
	return value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")


def shell_path(path: Path, user_home: Path) -> str:
	try:
		relative = path.relative_to(user_home)
	except ValueError:
		return shell(str(path))
	return "$HOME" + ("/" + shell(str(relative)) if str(relative) != "." else "")


def render(ctx: Context) -> list[Item]:
	custom_home = ctx.hub_home != ctx.user_home / ".ai-hub"
	handoff = str(ctx.hub_home / "handoff.md")
	if ctx.hub_home.is_relative_to(ctx.user_home):
		handoff = "~/" + str((ctx.hub_home / "handoff.md").relative_to(ctx.user_home))
	values = {name: shell_path(getattr(ctx, name), ctx.user_home) for name in ("hub_home", "repo", "python")}
	values["python"] = shlex.quote(str(ctx.python))
	values["handoff"] = shell(handoff)
	values["hub_export"] = f'export AI_HUB_HOME="{values["hub_home"]}"\n' if custom_home else ""
	items = []
	for name, folder in (("commit-msg", ctx.hooks), ("pre-push", ctx.hooks),
			("vf", ctx.hub_home / "tools"), ("continue-in-codex", ctx.hub_home / "tools")):
		text = Template((TEMPLATES / name).read_text(encoding="utf-8")).substitute(values)
		items.append(Item(folder / name, text, 0o755))
	path = (f"{ctx.user_home}/.nvm/versions/node/v22.22.1/bin:{ctx.user_home}/.local/bin:"
		"/opt/homebrew/bin:/usr/bin:/bin")
	values = {name: escape(str(getattr(ctx, name))) for name in ("user_home", "hub_home", "repo", "python", "label_prefix")}
	values["path"] = escape(path)
	values["hub_environment"] = ("\t\t<key>AI_HUB_HOME</key>\n"
		f"\t\t<string>{escape(str(ctx.hub_home))}</string>\n") if custom_home else ""
	for module in MODULES:
		text = Template((TEMPLATES / f"ai-hub-{module}.plist").read_text(encoding="utf-8")).substitute(values)
		data = plistlib.loads(text.encode("utf-8"))
		label = data["Label"]
		items.append(Item(ctx.user_home / "Library/LaunchAgents" / (label + ".plist"), text, 0o644, label))
	return items


def current(item: Item) -> tuple[str, int | None]:
	if any(path.is_symlink() for path in (item.path, *item.path.parents)):
		raise SetupError("설치 대상에 심볼릭 링크가 있습니다. 링크를 직접 확인한 뒤 다시 실행하세요.")
	try:
		info = item.path.stat()
		if not stat.S_ISREG(info.st_mode):
			raise SetupError("설치 대상은 일반 파일이어야 합니다.")
		return item.path.read_bytes().decode("utf-8"), stat.S_IMODE(info.st_mode)
	except FileNotFoundError:
		return "", None


def matches(item: Item) -> bool:
	return current(item) == (item.text, item.mode)
