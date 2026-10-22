"""원문 ↔ 토큰 변환. clean은 이름을 토큰으로, smudge는 토큰을 이름으로 되돌린다.

긴 값부터 바꾼다(부분 일치 방지). ASCII 이름은 영문·숫자 경계에서만 맞춘다.
개인 연락처·내부 주소는 collector.redact_pii 의 패턴으로 찾아 같은 사전에 넣는다.
"""

from __future__ import annotations

import re

from collector import redact_pii

from .store import Dictionary

PII_KINDS = (("이메일", redact_pii.EMAIL), ("전화번호", redact_pii.PHONE), ("IP", redact_pii.IPV4))


def _term_pattern(value: str) -> re.Pattern:
	escaped = re.escape(value)
	if value.isascii():
		return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
	return re.compile(escaped)


def encode(text: str, dictionary: Dictionary) -> str:
	# 1) 사전에 있는 이름을 긴 값부터 토큰으로
	for entry in sorted(dictionary.entries, key=lambda e: len(e["value"]), reverse=True):
		if entry["value"] and entry["value"] in text:
			text = _term_pattern(entry["value"]).sub(lambda _m, t=entry["token"]: t, text)
	# 2) 개인 연락처·내부 주소는 찾아서 새 토큰을 만든다
	for kind, pattern in PII_KINDS:
		for value in sorted(set(pattern.findall(text)), key=len, reverse=True):
			token = dictionary.token_for(value, kind)
			text = text.replace(value, token)
	pattern = redact_pii.host_pattern()
	if pattern is not None:
		for value in sorted(set(m.group(0) for m in pattern.finditer(text)), key=len, reverse=True):
			text = text.replace(value, dictionary.token_for(value, "호스트"))
	return text


def decode(text: str, dictionary: Dictionary) -> str:
	for token, value in sorted(dictionary.by_token().items(), key=lambda kv: len(kv[0]), reverse=True):
		text = text.replace(token, value)
	return text
