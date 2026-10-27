"""회사 노트와 커밋을 로컬에서만 찾는다.

이 명령의 출력은 회사 자료를 담으므로 AI 에이전트(Claude·Codex·AGY 등)가 대신 실행하거나 출력을 읽지 않는다. 사용자 터미널과 사무실 화면 전용
"""

import math
import re
import subprocess
import unicodedata
from pathlib import Path

from collector.config import ConfigError, home, load_sources


def _notes(query, limit):
	jar = home() / "bin" / "ai-hub-search.jar"
	if not jar.is_file():
		return [], "검색 엔진 파일이 없습니다"
	try:
		completed = subprocess.run(["/usr/libexec/java_home", "-v", "21"], stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=True, timeout=10)
		location = completed.stdout.strip()
		java = Path(location) / "bin" / "java"
		if not location or not java.is_file():
			return [], "Java 21을 찾을 수 없습니다"
	except (OSError, subprocess.SubprocessError):
		return [], "Java 21을 찾을 수 없습니다"
	try:
		completed = subprocess.run([str(java), "--enable-native-access=ALL-UNNAMED", "-jar", str(jar),
			"query", query, "--k", str(limit), "--mode", "hybrid", "--include-company"],
			stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
			check=True, timeout=120)
	except subprocess.TimeoutExpired:
		return [], "검색 엔진 응답 시간이 초과됐습니다"
	except (OSError, subprocess.SubprocessError):
		return [], "검색 엔진을 실행할 수 없습니다"
	notes = []
	for line in completed.stdout.splitlines():
		fields = line.split(" · ", 4)
		if len(fields) != 5:
			continue
		try:
			rank, score = int(fields[0]), float(fields[1])
		except ValueError:
			continue
		if rank < 1 or not math.isfinite(score) or not fields[2].strip() or not fields[3].strip():
			continue
		notes.append({"rank": rank, "score": score, "path": fields[2], "heading": fields[3], "excerpt": fields[4][:120]})
	return notes[:limit], ""


def _commits(query, limit):
	try:
		repos = load_sources().repos
	except (ConfigError, OSError):
		return [], "원천 설정을 읽을 수 없습니다"
	# 짧은 질의에도 조건 없는 전체 로그를 돌려주지 않는다.
	words = [word for word in query.split() if len(word) >= 2][:3] or [query]
	commits = {}
	failed = 0
	for repo in repos:
		if repo.cls != "company":
			continue
		command = ["git", "-C", repo.path, "log", "--all", "-i", "--fixed-strings", "--since=3 years ago",
			f"--max-count={limit}", "--date=short", "--format=%h%x1f%ad%x1f%s"]
		rows = []
		try:
			for arguments in ([f"--grep={query}"], [*(f"--grep={word}" for word in words), "--all-match"]):
				completed = subprocess.run(command + arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
					text=True, encoding="utf-8", errors="replace", check=True, timeout=30)
				rows.extend(completed.stdout.splitlines())
		except (OSError, subprocess.SubprocessError):
			failed += 1
			continue
		for line in rows:
			fields = line.split("\x1f", 2)
			if len(fields) != 3:
				continue
			digest, date, subject = fields
			if not re.fullmatch(r"[0-9a-f]{4,64}", digest) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
				continue
			commits.setdefault(digest, {"repo": repo.name or repo.id, "date": date, "hash": digest, "subject": subject[:120]})
	error = f"저장소 {failed}곳 읽기 실패" if failed else ""
	return sorted(commits.values(), key=lambda row: row["date"], reverse=True)[:limit], error


def search(query: str, limit: int = 10) -> dict:
	"""로컬 검색 결과와 경로를 드러내지 않는 오류 안내를 돌려준다."""
	if (not isinstance(query, str) or not 1 <= len(query) <= 200 or not query.strip()
		or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in query)):
		raise ValueError("질의는 제어문자 없는 1~200자여야 합니다")
	if type(limit) is not int or not 1 <= limit <= 30:
		raise ValueError("limit은 1~30 사이의 정수여야 합니다")
	notes, notes_error = _notes(query, limit)
	commits, commits_error = _commits(query, limit)
	return {"notes": notes, "notes_error": notes_error, "commits": commits, "commits_error": commits_error}
