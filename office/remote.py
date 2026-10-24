"""태일스케일 같은 사설망에서 들어오는 요청의 허용 규칙. 설정이 없으면 아무것도 허용하지 않는다.

~/.ai-hub/office.toml 의 [remote] 표:
	hosts  = ["<기기 이름>.<tailnet 이름>.ts.net:8790"]   허용할 Host(브라우저가 보내는 그대로: HTTP serve 는 포트가 붙고 HTTPS serve 는 붙지 않는다)
	logins = ["<태일스케일 로그인>"]                  허용할 사용자(serve 가 요청에 붙이는 Tailscale-User-Login 머리글과 비교)
서버는 계속 127.0.0.1 에만 열려 있고, 두 조건을 모두 맞춘 요청만 원격으로 인정한다.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from collector.config import home

from .common import read_toml

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net(?::[0-9]{1,5})?")
LOGIN = re.compile(r"[^\s<>\"',;]{1,120}")


def rules(root: Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
	"""(허용 Host, 허용 로그인). 설정이 없거나 틀리면 둘 다 비어 있다(원격 불허)."""
	root = root or home()
	try:
		section = read_toml(root / "office.toml", root).get("remote", {})
	except (OSError, ValueError, tomllib.TOMLDecodeError):
		return frozenset(), frozenset()
	if not isinstance(section, dict) or set(section) - {"hosts", "logins"}:
		return frozenset(), frozenset()
	hosts, logins = section.get("hosts", []), section.get("logins", [])
	if (not isinstance(hosts, list) or not isinstance(logins, list) or not 1 <= len(hosts) <= 5 or not 1 <= len(logins) <= 5
			or any(not isinstance(v, str) or not HOST.fullmatch(v) for v in hosts)
			or any(not isinstance(v, str) or not LOGIN.fullmatch(v) for v in logins)):
		return frozenset(), frozenset()
	return frozenset(hosts), frozenset(v.lower() for v in logins)


def allowed(host: str | None, login: str | None, root: Path | None = None) -> bool:
	"""Host 와 로그인이 모두 설정에 있을 때만 원격 요청으로 인정한다."""
	hosts, logins = rules(root)
	return bool(host and login and host in hosts and login.lower() in logins)
