"""마스킹 사전 저장소: ~/.ai-hub/redaction/dictionary.json (Git 밖, 원래 값을 담는다).

항목은 {"value": 원래 값, "kind": 종류, "token": ⟦종류-번호⟧} 이다. 같은 값은 항상 같은 토큰을 받는다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from collector.config import home

KINDS = ("회사명", "서비스명", "사람이름", "이메일", "전화번호", "IP", "호스트")


def dictionary_path() -> Path:
	return home() / "redaction" / "dictionary.json"


class Dictionary:
	def __init__(self, path: Path | None = None):
		self.path = path or dictionary_path()
		self.entries: list[dict] = []
		if self.path.exists():
			self.entries = json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])

	def save(self) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_name(self.path.name + ".tmp")
		tmp.write_text(json.dumps({"entries": self.entries}, ensure_ascii=False, indent=1), encoding="utf-8")
		os.replace(tmp, self.path)

	def token_for(self, value: str, kind: str) -> str:
		"""값에 대한 토큰. 없으면 같은 종류의 다음 번호로 새로 만든다(저장은 호출자가 save로 한다)."""
		for entry in self.entries:
			if entry["value"] == value:
				return entry["token"]
		if kind not in KINDS:
			raise ValueError(f"알 수 없는 종류: {kind}")
		number = sum(1 for e in self.entries if e["kind"] == kind) + 1
		token = f"⟦{kind}-{number}⟧"
		self.entries.append({"value": value, "kind": kind, "token": token})
		return token

	def by_token(self) -> dict[str, str]:
		return {e["token"]: e["value"] for e in self.entries}
