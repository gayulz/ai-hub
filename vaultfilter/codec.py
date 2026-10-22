"""원문 ↔ 토큰 변환. clean은 이름을 토큰으로, smudge는 토큰을 이름으로 되돌린다.

긴 값부터 바꾼다(부분 일치 방지). 이름은 대소문자와 붙은 글자에 상관없이 찾는다(ABC, abcusr, insertAbcUser).
찾은 표기마다 토큰을 따로 받으므로 되돌리면 원래 글자 그대로 돌아온다.
개인 연락처·내부 주소는 collector.redact_pii 의 패턴으로 찾아 같은 사전에 넣는다.
"""

from __future__ import annotations

import re

from collector import redact_pii

from .store import Dictionary

PII_KINDS = (("이메일", redact_pii.EMAIL), ("전화번호", redact_pii.PHONE), ("IP", redact_pii.IPV4))


def encode(text: str, dictionary: Dictionary) -> str:
	# 1) 개인 연락처·내부 주소를 먼저 통째로 찾는다(이메일 전체를 하나로 가리기 위해)
	for kind, pattern in PII_KINDS:
		for value in sorted(set(pattern.findall(text)), key=len, reverse=True):
			token = dictionary.token_for(value, kind)
			text = text.replace(value, token)
	pattern = redact_pii.host_pattern()
	if pattern is not None:
		for value in sorted(set(m.group(0) for m in pattern.finditer(text)), key=len, reverse=True):
			text = text.replace(value, dictionary.token_for(value, "호스트"))
	# 2) 사전의 이름을 긴 값부터 토큰으로. 남의 단어 속 일치(Desktop)도 가리지만 되돌림은 정확하다
	for entry in sorted(dictionary.entries, key=lambda e: len(e["value"]), reverse=True):
		if not entry["value"]:
			continue
		forms = set(re.findall(re.escape(entry["value"]), text, re.IGNORECASE))
		for form in sorted(forms, key=len, reverse=True):
			text = text.replace(form, dictionary.token_for(form, entry["kind"]))
	return text


def decode(text: str, dictionary: Dictionary) -> str:
	for token, value in sorted(dictionary.by_token().items(), key=lambda kv: len(kv[0]), reverse=True):
		text = text.replace(token, value)
	return text
