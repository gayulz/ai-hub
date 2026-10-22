"""사전 초기 후보: 회사 저장소의 폴더·파일 이름과 커밋 작성자 이름만 모은다(파일 내용은 읽지 않는다)."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

# 문서 이름에 흔한 일반 단어는 회사 이름이 아니므로 뺀다
STOP = {
	"docs", "doc", "notes", "note", "readme", "index", "test", "tests", "qa", "error", "log", "logs",
	"migration", "pending", "tasks", "task", "scope", "release", "analysis", "entry", "app", "src",
	"main", "api", "spec", "specs", "plan", "review", "design", "todo", "config", "build", "file",
	"files", "data", "mock", "sample", "guide", "history", "setup", "server", "client", "common",
	"module", "modules", "feature", "fix", "update", "status", "report", "summary", "memo", "draft",
	"final", "backup", "temp", "new", "old", "copy", "image", "images", "screen", "screens", "video",
}
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def _name_words(names: list[str]) -> Counter:
	counts: Counter = Counter()
	for name in names:
		for word in TOKEN.findall(name):
			if word.lower() not in STOP:
				counts[word] += 1
	return counts


def scan_names(roots: list[Path], max_depth: int = 4) -> Counter:
	names: list[str] = []
	for root in roots:
		names.append(root.name)
		for path in root.rglob("*"):
			if len(path.relative_to(root).parts) > max_depth or ".git" in path.parts:
				continue
			names.append(path.name)
	return _name_words(names)


def commit_authors(repo: Path) -> Counter:
	if not (repo / ".git").exists():
		return Counter()
	out = subprocess.run(["git", "-C", str(repo), "log", "--format=%an"],
		capture_output=True, text=True, timeout=120).stdout
	return Counter(line.strip() for line in out.splitlines() if line.strip())
