"""공개 저장소(ai-hub) push 전 검사: 보낼 커밋의 새 줄·경로·작성자·메시지에서 회사 이름과 비밀값을 찾는다.

볼트와 달리 평문으로 공개되므로 커밋마다 새로 추가된 줄만 본다. 이미 확인한 합성 값이 있는 파일을
고쳐도 그 줄이 그대로면 다시 걸리지 않는다. 결과에는 커밋·파일 순번과 종류만 적고 찾은 값은 출력하지 않는다.
"""

from __future__ import annotations

import getpass
import re
import unicodedata

from collector import redact as secrets

from . import verify
from .store import Dictionary

NAME_KINDS = ("회사명", "서비스명", "호스트", "사람이름")
SECRETS = (secrets.PRIVATE_KEY, secrets.JWT, secrets.TOKEN_PREFIX, verify.SECRET_LITERAL)


def _pattern(values: list[str]) -> re.Pattern | None:
	parts = []
	for value in sorted({unicodedata.normalize("NFC", v) for v in values if len(v) >= 2}, key=len, reverse=True):
		escaped = re.escape(value)
		# 영문·숫자 이름은 단어 단위로만 찾는다("Desktop" 안의 세 글자 같은 오탐을 막는다)
		parts.append(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])" if value.isascii() else escaped)
	return re.compile("|".join(parts), re.IGNORECASE) if parts else None


class Rules:
	def __init__(self):
		dictionary = Dictionary()
		if not dictionary.path.is_file() or not dictionary.entries:
			raise ValueError("마스킹 사전이 없거나 비어 있습니다")
		self.names = _pattern([e["value"] for e in dictionary.entries if e["kind"] in NAME_KINDS])
		user = getpass.getuser()
		self.people = _pattern([e["value"] for e in dictionary.entries] + [user])
		self.home = re.compile(rf"/(?:Users|home)/{re.escape(user)}(?![A-Za-z0-9])")

	def kinds(self, text: str) -> list[str]:
		text = unicodedata.normalize("NFC", text)
		found = []
		if self.names and self.names.search(text):
			found.append("사전 이름")
		if any(pattern.search(text) for pattern in SECRETS):
			found.append("비밀값")
		if self.home.search(text):
			found.append("홈 경로")
		return found


def _added(commit: str) -> tuple[dict[str, list[str]], int]:
	"""파일별로 이 커밋이 새로 넣은 줄과 줄 단위로 볼 수 없는 바이너리 수. 이름 바꾸기는 바뀐 줄만 나온다."""
	patch = verify._git("-c", "core.quotepath=false", "show", "--format=", "-U0", "--no-color", "--no-ext-diff",
		"-M", commit).decode("utf-8", errors="replace")
	result, current, binaries = {}, None, 0
	for line in patch.split("\n"):
		if line.startswith("Binary files ") and not line.endswith(" and /dev/null differ"):
			binaries += 1
		elif line.startswith("+++ "):
			# 특수 문자가 든 경로는 git이 따옴표로 감싼다. 그래도 줄은 검사한다(파일 순번만 '?')
			current = None if line == "+++ /dev/null" else line[6:] if line.startswith("+++ b/") else line[4:]
		elif line.startswith("+") and current is not None:
			result.setdefault(current, []).append(line[1:])
	return result, binaries


def check_public(revs: list[str]) -> int:
	rules = Rules()
	problems = 0
	for commit in verify._git("rev-list", *revs).decode().split():
		short = commit[:12]
		metadata = verify._git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", commit).decode("utf-8", errors="replace")
		if rules.people and rules.people.search(unicodedata.normalize("NFC", metadata)):
			problems += 1
			print(f"작성자 정보에 사전 원문 또는 사용자 이름: {short}")
		message = verify._git("log", "-1", "--format=%B", commit).decode("utf-8", errors="replace")
		if verify.AI_SIGNATURE.search(message):
			problems += 1
			print(f"커밋 메시지에 AI 서명: {short}")
		if rules.kinds(message):
			problems += 1
			print(f"커밋 메시지 누출 의심: {short} ({', '.join(rules.kinds(message))})")
		names = verify._git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", "-M", commit)
		paths = [p.decode("utf-8", errors="replace") for p in names.split(b"\0") if p]
		for index, path in enumerate(paths, 1):
			if any(part.startswith("🔐") for part in path.split("/")):
				problems += 1
				print(f"커밋 금지 경로: {short} 파일 #{index}")
			elif rules.kinds(path):
				problems += 1
				print(f"경로 누출 의심: {short} 파일 #{index} ({', '.join(rules.kinds(path))})")
		added, binaries = _added(commit)
		if binaries:
			problems += binaries
			print(f"검사할 수 없는 바이너리 {binaries}개: {short}")
		for path, lines in added.items():
			kinds = rules.kinds("\n".join(lines))
			if kinds:
				problems += 1
				where = paths.index(path) + 1 if path in paths else "?"
				print(f"새 줄 누출 의심: {short} 파일 #{where} ({', '.join(kinds)})")
	return problems
