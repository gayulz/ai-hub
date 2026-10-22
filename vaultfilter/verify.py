"""푸시 전 검사: 저장될 내용(HEAD 트리)과 커밋 메시지에 사전 원문·개인 연락처·AI 서명이 남았는지 본다.

결과에는 위치와 종류만 적고, 찾은 값은 출력하지 않는다.
"""

from __future__ import annotations

import re
import subprocess
import sys

from collector import redact_pii

from .store import Dictionary

AI_SIGNATURE = re.compile(
	r"(?im)^(co-authored-by:.*(claude|anthropic|codex|openai|gemini)|generated-by:"
	r"|signed-off-by:.*(claude|codex|gemini)|.*🤖|.*🐙 autopus)"
)


def _find_leaks(text: str, values: list[str]) -> list[str]:
	# 필터(codec.encode)와 같은 규칙: 대소문자와 붙은 글자에 상관없이 찾는다
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
	return kinds


def _git(*args: str) -> bytes:
	return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def check_tree() -> int:
	values = [e["value"] for e in Dictionary().entries if len(e["value"]) >= 2]
	problems = 0
	listing = _git("ls-tree", "-r", "-z", "HEAD").split(b"\0")
	for entry in filter(None, listing):
		meta, path = entry.split(b"\t", 1)
		sha = meta.split()[2].decode()
		if not path.decode().endswith(".md"):
			continue
		text = _git("cat-file", "blob", sha).decode("utf-8", errors="replace")
		kinds = _find_leaks(text, values)
		if kinds:
			problems += 1
			print(f"누출 의심: {path.decode()} ({', '.join(kinds)})")
	return problems


def check_messages(revs: str) -> int:
	text = _git("log", "--format=%B%x00", revs).decode("utf-8", errors="replace")
	values = [e["value"] for e in Dictionary().entries if len(e["value"]) >= 2]
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
	values = [e["value"] for e in Dictionary().entries if len(e["value"]) >= 2]
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
	elif argv[:1] == ["tree"]:
		count = check_tree()
	elif argv[:1] == ["messages"]:
		count = check_messages(argv[1] if len(argv) > 1 else "HEAD")
	else:
		print("사용법: verify tree | verify messages <rev-range>", file=sys.stderr)
		return 2
	print("검사 통과" if count == 0 else f"검사 실패: {count}건")
	return 0 if count == 0 else 1
