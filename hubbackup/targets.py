"""허용한 설정과 보관함만 고른다. 링크와 인증 파일은 따라가지 않는다."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from collector.config import home

FILES = {
	"ai-hub": ("sources.toml", "plans.toml", "redact_hosts.toml", "ledger.db", "handoff.md"),
	"claude": ("CLAUDE.md", "settings*.json", "statusline-*.sh"),
	"codex": ("AGENTS.md", "config.toml", "hooks.json"),
	"gemini": ("GEMINI.md", "settings.json", "projects.json", "trustedFolders.json"),
}
TREES = {
	"ai-hub": ("redaction", "runs", "tools", "archive", "office-tasks"),
	"claude": ("rules", "hooks", "agents", "skills"),
	"codex": ("hooks",),
	"gemini": ("rules", "skills", "hooks"),
}
EXCLUDED = {"keys", "__pycache__", ".DS_Store", "auth.json", "oauth_creds.json", "google_accounts.json"}


def roots(user_home: Path | None = None, hub_home: Path | None = None) -> dict[str, Path]:
	user_home = Path.home() if user_home is None else user_home
	return {"ai-hub": home() if hub_home is None else hub_home,
		**{name: user_home / ("." + name) for name in ("claude", "codex", "gemini")}}


def backup_dir() -> Path:
	return Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/ai-hub-backup/state"


def excluded(name: str) -> bool:
	return name in EXCLUDED or "oauth-token" in name.lower() or name.startswith("🔐")


def _wanted(tool: str, parts: tuple[str, ...], directory: bool) -> bool:
	if len(parts) == 1:
		if directory:
			return parts[0] in TREES[tool] or (tool == "claude" and parts[0] == "projects")
		return any(fnmatch.fnmatchcase(parts[0], pattern) for pattern in FILES[tool])
	if parts[0] in TREES[tool]:
		return True
	if tool == "claude" and parts[0] == "projects":
		return directory if len(parts) == 2 else parts[2] == "memory" and (directory or len(parts) > 3)
	return False


def files(source_roots: dict[str, Path], failed):
	def walk(tool: str, path: Path, parts: tuple[str, ...]):
		relative = "/".join((tool, *parts))
		try:
			if path.is_symlink():
				return
			with os.scandir(path) as listing:
				entries = sorted(listing, key=lambda item: item.name)
		except FileNotFoundError:
			return
		except OSError:
			failed(relative)
			return
		for entry in entries:
			if excluded(entry.name):
				continue
			child_parts = (*parts, entry.name)
			child_relative = "/".join((tool, *child_parts))
			try:
				if entry.is_symlink():
					continue
				directory = entry.is_dir(follow_symlinks=False)
				if not _wanted(tool, child_parts, directory):
					continue
				if directory:
					yield from walk(tool, Path(entry.path), child_parts)
				elif entry.is_file(follow_symlinks=False):
					yield child_relative, Path(entry.path)
			except FileNotFoundError:
				continue
			except OSError:
				failed(child_relative)
	for tool, root in source_roots.items():
		yield from walk(tool, root, ())
