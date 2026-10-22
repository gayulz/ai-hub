"""비밀값 제거.

naver-blog-agent의 blogops/devlog_redact_patterns.py 중 비밀값 부분만 옮겼다
(주소·사람 이름·회사 이름 규칙은 제외: 정본 계획 D5-A에 따라 로컬 볼트에는 이름을 남긴다).
결과는 항상 [보안값]으로 바뀐다.
"""

from __future__ import annotations

import re

from .redact_pii import redact_pii

MASK = "[보안값]"

SECRET_KEY = (
	r"(?:password|passwd|pwd|passphrase|(?:client|api|private|secret|access)[_-]?key(?:[_-]?id)?"
	r"|(?:client[_-]?)?secret|(?:access[_-]?|refresh[_-]?|auth[_-]?)?token)"
)

PRIVATE_KEY = re.compile(
	r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)
# key=value, key: value, "key": "value" 형태. 키 이름 뒤의 값만 가린다.
SECRET_ASSIGN = re.compile(
	rf"(?i)((?<![\w])(?:[a-z][\w.-]*[._-])?{SECRET_KEY}[\"']?[ \t]*(?:=(?!=)|:(?!=))[ \t]*)"
	r"(\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|(?:\\.|[^\s\"])+)"
)
SECRET_TAG = re.compile(rf"(?i)(<{SECRET_KEY}(?:\s[^>]*)?>)([^<]+)(</{SECRET_KEY}>)")
AUTH_HEADER = re.compile(
	r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*)[^\r\n]+"
)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
TOKEN_PREFIX = re.compile(
	r"\b(?:AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
	r"|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,})\b"
)
URL_AUTH = re.compile(
	r"(?i)\b((?:https?|ftp|ssh|mysql|mariadb|postgresql|sqlserver)://)[^\s/@]+(?::[^\s/@]*)?@"
)


def _assign_repl(match: re.Match) -> str:
	value = match.group(2)
	quote = value[0] if value[0] in "\"'" else ""
	if value.strip(quote) == MASK:
		return match.group(0)
	return f"{match.group(1)}{quote}{MASK}{quote}"


def redact(text: str) -> str:
	"""비밀값과 개인 연락처·내부 주소를 [보안값]으로 바꾼 문자열을 돌려준다."""
	text = PRIVATE_KEY.sub(MASK, text)
	text = SECRET_ASSIGN.sub(_assign_repl, text)
	text = SECRET_TAG.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", text)
	text = AUTH_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", text)
	text = JWT.sub(MASK, text)
	text = TOKEN_PREFIX.sub(MASK, text)
	text = URL_AUTH.sub(lambda m: f"{m.group(1)}{MASK}@", text)
	return redact_pii(text)
