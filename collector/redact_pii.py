"""개인 연락처·내부 주소 마스킹 (2026-10-22 사용자 결정: 보안값으로 취급).

이메일, 전화번호, IPv4, 사내 도메인 호스트를 [보안값]으로 바꾼다.
사내 도메인 목록은 저장소에 넣지 않고 ~/.ai-hub/redact_hosts.toml 에만 둔다.
"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache

from .config import home

MASK = "[보안값]"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 한국 휴대폰·지역번호 형식만 잡는다(0으로 시작하거나 +82). 날짜·금액과 겹치지 않게 자릿수를 제한한다.
PHONE = re.compile(r"(?<![\d-])(?:\+82[-\s]?|0)(?:1[016-9]|2|[3-6]\d)[-\s]?\d{3,4}[-\s]?\d{4}(?![\d-])")
# 옥텟 0~255. 세 점 이하(버전 번호 3.14.6 등)는 잡히지 않는다. 네 번째 옥텟 뒤에 .숫자가 이어지면 제외한다.
OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4 = re.compile(rf"(?<![\d.])(?:{OCTET}\.){{3}}{OCTET}(?!\.?\d)")


@lru_cache(maxsize=1)
def host_pattern() -> re.Pattern | None:
	"""사내 도메인과 그 하위 호스트 패턴. 파일이 없거나 비어 있으면 None."""
	path = home() / "redact_hosts.toml"
	if not path.exists():
		return None
	with path.open("rb") as fh:
		domains = [d.strip().lower() for d in tomllib.load(fh).get("domains", []) if d.strip()]
	if not domains:
		return None
	alternatives = "|".join(re.escape(d) for d in domains)
	return re.compile(rf"(?i)(?<![\w-])(?:[a-z0-9-]+\.)*(?:{alternatives})(?![\w-])")


def redact_pii(text: str) -> str:
	text = EMAIL.sub(MASK, text)
	text = PHONE.sub(MASK, text)
	text = IPV4.sub(MASK, text)
	pattern = host_pattern()
	if pattern is not None:
		text = pattern.sub(MASK, text)
	return text


def count_pii(text: str) -> dict[str, int]:
	"""종류별 개수만 센다(내용은 돌려주지 않는다). 측정용."""
	counts = {"email": len(EMAIL.findall(text)), "phone": len(PHONE.findall(text)),
		"ipv4": len(IPV4.findall(text))}
	pattern = host_pattern()
	counts["host"] = len(pattern.findall(text)) if pattern else 0
	return counts
