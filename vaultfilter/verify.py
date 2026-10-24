"""푸시 전 검사: 전송할 이력의 파일 경로·본문과 커밋 메시지에서 누출을 찾는다.

볼트는 10-22부터 git-crypt로 암호화해 저장하므로 본문은 암호화 여부만 확인한다(`verify encrypted`).
결과에는 커밋·파일 순번과 종류만 적고, 찾은 값이나 원문 경로는 출력하지 않는다.
"""

from __future__ import annotations

import re
import subprocess
import sys

from collector import redact as secrets, redact_pii

from .store import Dictionary

AI_SIGNATURE = re.compile(
	r"(?im)^(co-authored-by:.*(claude|anthropic|codex|openai|gemini)|generated-by:"
	r"|signed-off-by:.*(claude|codex|gemini)|.*🤖|.*🐙 autopus)"
)
CRYPT_HEADER = b"\x00GITCRYPT\x00"
SECRET_LITERAL = re.compile(
	rf"(?i)(?<![\w.-])(?:{secrets.SECRET_KEY}|[\w.-]+[_-](?:key|secret|token))"
	r"[\"']?[ \t]*[:=][ \t]*[\"']?[A-Za-z0-9_+/=-]{16,}(?![\w.-])"
)


def _find_leaks(text: str, values: list[str]) -> list[str]:
	# 대소문자와 붙은 글자에 상관없이 찾는다
	kinds = []
	lowered = text.lower()
	if any(value.lower() in lowered for value in values):
		kinds.append("사전 원문")
	for name, pattern in (("이메일", redact_pii.EMAIL), ("전화번호", redact_pii.PHONE), ("IP", redact_pii.IPV4)):
		if pattern.search(text):
			kinds.append(name)
	host = redact_pii.host_pattern()
	if host is not None and host.search(text):
		kinds.append("사내 호스트")
	if any(pattern.search(text) for pattern in (secrets.PRIVATE_KEY, secrets.JWT, secrets.TOKEN_PREFIX, SECRET_LITERAL)):
		kinds.append("비밀값")
	return kinds


def _git(*args: str) -> bytes:
	return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def _values() -> list[str]:
	dictionary = Dictionary()
	if not dictionary.path.is_file() or not dictionary.entries:
		raise ValueError("마스킹 사전이 없거나 비어 있습니다")
	return [e["value"] for e in dictionary.entries if len(e["value"]) >= 2]


def _inspect_encrypted(name: str, blob: bytes, location: str, values: list[str]) -> int:
	"""암호화 저장소용: .gitattributes 말고는 모두 git-crypt 머리로 시작해야 한다."""
	if name == ".gitattributes":
		if b"* filter=git-crypt" in blob and not _find_leaks(blob.decode("utf-8", errors="replace"), values):
			return 0
		print(f"암호화 규칙이 없거나 누출이 의심되는 .gitattributes: {location}")
		return 1
	if blob.startswith(CRYPT_HEADER):
		return 0
	print(f"암호화되지 않은 파일: {location}")
	return 1


def check_encrypted(revs: str = "HEAD") -> int:
	return _scan(revs, _inspect_encrypted)


def _scan(revs: str, inspect) -> int:
	"""전송할 이력의 작성자·경로를 검사하고, 파일 본문은 inspect 로 검사한다."""
	values = _values()
	problems = 0
	seen_blobs = set()
	for commit in _git("rev-list", revs).decode().splitlines():
		metadata = _git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", commit).decode("utf-8")
		if any(value.lower() in metadata.lower() for value in values):
			problems += 1
			print(f"작성자 정보에 사전 원문: {commit[:12]}")
		listing = _git("ls-tree", "-r", "-z", commit).split(b"\0")
		for index, entry in enumerate(filter(None, listing), 1):
			meta, path = entry.split(b"\t", 1)
			mode, _, sha = meta.decode().split()
			name = path.decode("utf-8", errors="strict")
			location = f"{commit[:12]} 파일 #{index}"
			if any(part.startswith("🔐") for part in name.split("/")):
				problems += 1
				print(f"커밋 금지 경로: {location}")
				continue
			kinds = _find_leaks(name, values)
			if kinds:
				problems += 1
				print(f"경로 누출 의심: {location} ({', '.join(kinds)})")
			if mode not in ("100644", "100755"):
				problems += 1
				print(f"검사할 수 없는 파일 유형: {location}")
				continue
			if sha in seen_blobs:
				continue
			seen_blobs.add(sha)
			problems += inspect(name, _git("cat-file", "blob", sha), location, values)
	return problems


def check_messages(revs: str) -> int:
	text = _git("log", "--format=%B%x00", revs).decode("utf-8", errors="replace")
	values = _values()
	problems = 0
	for message in filter(None, text.split("\0")):
		if AI_SIGNATURE.search(message):
			problems += 1
			print("커밋 메시지에 AI 서명이 있습니다")
		if _find_leaks(message, values):
			problems += 1
			print("커밋 메시지에 사전 원문 또는 개인 연락처가 있습니다")
	return problems


def check_message_file(path: str) -> int:
	with open(path, encoding="utf-8", errors="replace") as fh:
		message = fh.read()
	values = _values()
	problems = 0
	if AI_SIGNATURE.search(message):
		problems += 1
		print("커밋 메시지에 AI 서명이 있습니다")
	if _find_leaks(message, values):
		problems += 1
		print("커밋 메시지에 사전 원문 또는 개인 연락처가 있습니다")
	return problems


def main(argv: list[str]) -> int:
	if argv[:1] == ["message-file"] and len(argv) == 2:
		count = check_message_file(argv[1])
	elif argv[:1] == ["encrypted"] and len(argv) <= 2:
		count = check_encrypted(argv[1] if len(argv) > 1 else "HEAD")
	elif argv[:1] == ["messages"]:
		count = check_messages(argv[1] if len(argv) > 1 else "HEAD")
	elif argv[:1] == ["public"] and len(argv) >= 2:
		from .public import check_public
		count = check_public(argv[1:])
	else:
		print("사용법: verify encrypted [rev-range] | verify messages <rev-range> | verify message-file <파일> | verify public <rev-list 인자>", file=sys.stderr)
		return 2
	print("검사 통과" if count == 0 else f"검사 실패: {count}건")
	return 0 if count == 0 else 1
