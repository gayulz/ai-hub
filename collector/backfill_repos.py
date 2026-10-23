"""승인 문서의 AI 없는 색인과 로컬 Git 전체 이력 읽기."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

from . import repos
from .backfill_common import child, clean, safe, sha
from .config import ConfigError, Repo, Sources, classify

SKIP = {".git", ".claude", ".codex", ".agents", ".autopus", "node_modules", "build", "target", "dist", "vendor_imports"}
UNSUPPORTED = {".pdf", ".doc", ".docx", ".rtf", ".odt", ".html", ".htm"}


def git(path: Path, *args: str, optional: bool = False) -> str:
	try:
		result = subprocess.run(["git", "--no-optional-locks", "--literal-pathspecs", "-C", str(path), *args],
			capture_output=True, text=True, timeout=60)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ConfigError(f"Git 조회 실패: {type(exc).__name__}") from exc
	if result.returncode and not optional:
		raise ConfigError("Git 조회 실패(빈 결과와 구분)")
	return result.stdout if result.returncode == 0 else ""


def walk(root: Path, recursive: bool = True):
	if not safe(root) or not root.is_dir():
		return
	for entry in sorted(root.iterdir()):
		if not safe(entry) or entry.name in SKIP:
			continue
		if entry.is_file():
			yield entry
		elif recursive and entry.is_dir():
			yield from walk(entry)


def resolve_roots(root: Path) -> list[Path]:
	"""등록 폴더 안의 실제 Git 루트만 허용한다. 상위 저장소로 조용히 넓히지 않는다."""
	if not safe(root) or not root.is_dir():
		raise ConfigError("등록 원천 경로가 없거나 안전하지 않습니다")
	if (root / ".git").exists():
		if not safe(root / ".git"):
			raise ConfigError("Git 메타데이터가 링크 또는 비밀 경로입니다")
		actual = Path(git(root, "rev-parse", "--show-toplevel").strip())
		if actual != root.absolute():
			raise ConfigError("등록 경로와 실제 Git 루트가 다릅니다")
		return [root]
	parent = git(root, "rev-parse", "--show-toplevel", optional=True).strip()
	if parent:
		raise ConfigError("등록 경로가 Git 루트의 하위입니다. 실제 루트를 등록해야 합니다")
	found = []
	for folder, dirs, _ in os.walk(root, followlinks=False):
		path = Path(folder)
		dirs[:] = sorted(d for d in dirs if d not in SKIP and safe(path / d))
		if path != root and (path / ".git").exists():
			found.extend(resolve_roots(path))
			dirs[:] = []
	return found


def document_dates(path: Path, roots: list[Path]) -> tuple[str, str, str]:
	root = next((p for p in roots if path.is_relative_to(p)), None)
	if root is None:
		return "", "", "Git 없는 원천"
	rel = str(path.relative_to(root))
	if not git(root, "ls-files", "--", rel).strip():
		return "", "", "Git 미추적"
	try:
		dates = git(root, "log", "--follow", "--format=%aI", "--", rel).splitlines()
	except ConfigError:
		return "", "", "Git 일자 조회 실패"
	return (min(dates), max(dates), "로컬 Git 이력") if dates else ("", "", "Git 일자 미확인")


def title(text: str, path: Path) -> str:
	if path.suffix.lower() in (".html", ".htm"):
		match = re.search(r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", text, re.S | re.I)
		return unescape(re.sub(r"<[^>]+>", "", match[1])).strip() if match else path.name
	match = re.search(r"^#{1,6}\s+(.+)$", text, re.M)
	return match[1].strip() if match else path.name


def inventory(repo: Repo, sources: Sources) -> dict:
	root = Path(repo.path)
	tasks, warnings, git_failed = [], [], False
	try:
		roots = resolve_roots(root)
	except ConfigError as exc:
		roots = []
		git_failed = True
		warnings.append(str(exc))
		tasks.append({"key": f"backfill:repo:{repo.id}:git-error", "hash": sha(str(exc)),
			"type": "error", "reason": str(exc)})
	seen = set()
	for relroot in repo.doc_dirs:
		folder = child(root, relroot)
		if not folder.exists():
			warnings.append("문서 읽기 범위가 없습니다: " + clean(relroot))
			tasks.append({"key": f"backfill:repo:{repo.id}:missing:{sha(relroot)}", "hash": sha(relroot),
				"type": "error", "reason": "승인 문서 읽기 범위가 없음: " + clean(relroot)})
		for path in ([folder] if folder.is_file() else walk(folder, recursive=relroot != ".")):
			rel = path.relative_to(root).as_posix()
			rule = classify(str(path), sources.rules)
			if rule and rule.cls == "exclude":
				continue
			if rel in seen or any(fnmatch.fnmatchcase(rel, p) for p in repo.doc_exclude):
				continue
			seen.add(rel)
			explicit = any(fnmatch.fnmatchcase(rel, p) for p in repo.doc_include)
			if repo.doc_include and not explicit:
				continue
			suffix = path.suffix.lower()
			if suffix not in repos.DOC_SUFFIXES and not explicit and suffix not in UNSUPPORTED:
				continue
			key = f"backfill:repo:{repo.id}:docs:{sha(rel)}"
			if rule and rule.cls != repo.cls:
				tasks.append({"key": key, "hash": sha(rel), "type": "error", "relative": clean(rel),
					"reason": "문서 경로 분류와 프로젝트 분류가 충돌"})
				continue
			if clean(rel) != rel:
				tasks.append({"key": key, "hash": sha(rel), "type": "error", "relative": clean(rel),
					"reason": "문서 경로에 보안값이 있어 자동 처리 보류"})
				continue
			html_allowed = suffix in (".html", ".htm") and any(
				p.lower().endswith((".html", ".htm")) and fnmatch.fnmatchcase(rel, p) for p in repo.doc_include)
			if suffix not in repos.DOC_SUFFIXES and not html_allowed:
				tasks.append({"key": key, "hash": sha(rel), "type": "error",
					"reason": "미지원 문서 형식", "relative": clean(rel)})
				continue
			try:
				data = path.read_bytes()
				text = data.decode("utf-8")
				first, last, evidence = document_dates(path, roots)
				if git_failed:
					evidence = "Git 루트 확인 실패"
				body = (f"- 제목: {clean(title(text, path))}\n- 원천: <{clean(path.absolute().as_uri())}>\n"
					f"- 상대 경로: `{clean(rel)}`\n- 내용 SHA-256: `{sha(data)}`\n"
					f"- 확인 가능한 Git 일자: {first or '미확인'} ~ {last or '미확인'} ({evidence})\n"
					"- 정제 방식: 문서 색인, AI 호출 없음. Git 일자는 실제 최초 작성일을 보장하지 않음")
				tasks.append({"key": key, "hash": sha(data), "type": "document", "relative": clean(rel),
					"body": clean(body)})
			except (OSError, UnicodeError, ConfigError) as exc:
				tasks.append({"key": key, "hash": sha(rel), "type": "error", "relative": clean(rel),
					"reason": f"문서 조회 실패: {type(exc).__name__}"})
	by_hash = {}
	for task in tasks:
		if task["type"] != "document":
			continue
		if task["hash"] in by_hash:
			task["body"] += "\n- 동일 내용의 별도 문서: `" + by_hash[task["hash"]] + "` (자동 병합하지 않음)"
		else:
			by_hash[task["hash"]] = task["relative"]
	days, all_shas, excluded = defaultdict(list), set(), 0
	for gitroot in roots:
		try:
			email = repos.author_email(gitroot)
			if not email:
				raise ConfigError("본인 커밋 이메일을 확인할 수 없습니다")
			if not git(gitroot, "show-ref", optional=True).strip() and not git(gitroot, "rev-parse", "--verify", "HEAD", optional=True).strip():
				continue
			total = int(git(gitroot, "rev-list", "--all", "--count").strip())
			log = git(gitroot, "log", "--all", f"--author={email}", "--format=%H%x09%aI%x09%s")
			excluded += total - len(log.splitlines())
			for line in log.splitlines():
				commit, stamp, subject = line.split("\t", 2)
				if commit in all_shas:
					continue
				all_shas.add(commit)
				when = datetime.fromisoformat(stamp).astimezone(ZoneInfo("Asia/Seoul"))
				days[when.date().isoformat()].append({"sha": commit, "stamp": stamp, "subject": clean(subject),
					"source": clean(gitroot.absolute().as_uri())})
		except ConfigError as exc:
			tasks.append({"key": f"backfill:repo:{repo.id}:git:{sha(str(gitroot))}",
				"hash": sha(str(exc)), "type": "error", "reason": str(exc)})
	for day, commits in sorted(days.items()):
		commits.sort(key=lambda c: (c["stamp"], c["sha"]))
		tasks.append({"key": f"backfill:repo:{repo.id}:commits", "day": day,
			"hash": sha("".join(c["sha"] for c in commits)), "type": "commits", "commits": commits,
			"id": f"commits:{day}"})
	# 같은 블록 키는 일지마다 독립적이므로 작업 키에만 날짜를 붙인다.
	for task in tasks:
		if task["type"] == "commits":
			task["block_key"] = task["key"]
			task["key"] += ":" + task["day"]
	return {"tasks": tasks, "warnings": warnings, "git_roots": len(roots), "commits": len(all_shas),
		"excluded_commits": excluded}


def commit_body(task: dict) -> str:
	return "\n".join(f"- {c['stamp']} 커밋 `{c['sha']}` {clean(c['subject'])} · 원천 <{c['source']}>"
		for c in task["commits"])
