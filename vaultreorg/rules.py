"""R1 설정·날짜 근거·폴더 분류와 읽기 전 경로 검사."""

from __future__ import annotations

import json
import re
import tomllib
import unicodedata
from datetime import date, datetime
from pathlib import Path

from collector.backfill_common import child, safe
from collector.config import ConfigError, home

EXCLUDED = {".obsidian", ".git", ".trash"}
TYPES = {"Career": "career", "Study": "study", "Blog": "blog", "MOC": "moc",
	"Setup": "reference", "AI & Prompts": "reference", "Projects": "project", "Inbox": "daily"}
OLD_DATES = "oldvault-dates-261024.json"


def base() -> Path:
	return child(home(), "vaultreorg")


def protected(path: Path) -> bool:
	return any(part.startswith("🔐") for part in path.parts)


def relative(value: str) -> Path:
	if not isinstance(value, str) or not value or "\x00" in value:
		raise ConfigError("노트 상대 경로 형식 오류")
	path = Path(value)
	if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
		raise ConfigError("볼트 안의 .md 상대 경로가 필요합니다")
	return path


def target(vault: Path, value: str) -> Path:
	path = relative(value)
	if protected(path) or EXCLUDED.intersection(path.parts):
		raise ConfigError("제외 대상 노트 경로입니다")
	return child(vault, value)


def vault_root(vault: Path) -> Path:
	vault = vault.absolute()
	if protected(vault) or EXCLUDED.intersection(vault.parts) or not safe(vault) or not vault.is_dir():
		raise ConfigError("볼트는 비밀 경로·링크가 아닌 기존 폴더여야 합니다")
	return vault


def folder(path: Path) -> str:
	if len(path.parts) == 1:
		return "root"
	name = path.parts[0]
	while name and (name[0].isspace() or unicodedata.category(name[0])[0] in "SMZ" or name[0] == "\u200d"):
		name = name[1:]
	return name


def note_type(path: Path) -> str | None:
	name = folder(path)
	if name == "Company":
		# macOS 파일 이름은 자모가 분리된 NFD일 수 있어 NFC로 맞춘 뒤 비교한다
		return "troubleshoot" if re.search("오류|에러|장애|이슈|해결", unicodedata.normalize("NFC", path.name)) else "reference"
	return TYPES.get(name)


def inventory(vault: Path, counts: dict):
	for path in sorted(vault.iterdir()):
		if path.name.startswith("🔐"):
			counts["protected"] += 1
		elif path.name in EXCLUDED:
			counts["excluded"] += 1
		elif path.is_symlink():
			counts["symlink"] += 1
		elif path.is_dir():
			yield from inventory(path, counts)
		elif path.suffix == ".md" and path.is_file():
			yield path


def error(name: str, line: int, reason: str):
	raise ConfigError(f"{name} {line}행: {reason}")


def toml_file(name: str) -> tuple[dict, str]:
	path = child(base(), name)
	if not path.exists():
		return {}, ""
	try:
		text = path.read_text(encoding="utf-8")
	except UnicodeDecodeError as exc:
		error(name, exc.object[:exc.start].count(b"\n") + 1, "UTF-8 형식 오류")
	try:
		return tomllib.loads(text), text
	except tomllib.TOMLDecodeError as exc:
		line = getattr(exc, "lineno", None)
		if line is None:
			match = re.search(r"at line (\d+)", str(exc))
			line = int(match[1]) if match else text.count("\n") + 1
		error(name, line, "TOML 형식 오류")


def field_line(lines: list[str], name: str, default: int, stop: int | None = None) -> int:
	for index in range(default - 1, stop or len(lines)):
		if re.match(r"\s*" + re.escape(name) + r"\s*=", lines[index]):
			return index + 1
	return default


def iso_date(value) -> str | None:
	if type(value) is date:
		return value.isoformat()
	if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
		return None
	try:
		return date.fromisoformat(value).isoformat()
	except ValueError:
		return None


def overrides() -> dict[str, dict[str, str]]:
	name = "r1-overrides.toml"
	data, text = toml_file(name)
	lines = text.splitlines()
	starts = [i + 1 for i, line in enumerate(lines) if re.fullmatch(r"\s*\[\[note\]\]\s*(?:#.*)?", line)]
	notes = data.get("note", [])
	if set(data) - {"note"} or not isinstance(notes, list):
		error(name, 1, "[[note]] 목록이 필요합니다")
	result = {}
	for index, item in enumerate(notes):
		line = starts[index] if index < len(starts) else 1
		stop = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
		if not isinstance(item, dict) or "path" not in item or set(item) - {"path", "type", "sensitivity", "created"}:
			error(name, line, "path와 선택 키 type·sensitivity·created를 사용하세요")
		try:
			path = relative(item["path"]).as_posix()
		except ConfigError:
			error(name, field_line(lines, "path", line, stop), "path는 볼트 안의 .md 상대 경로여야 합니다")
		if path in result:
			error(name, line, "노트 경로가 중복됐습니다")
		values = {}
		for field in ("type", "sensitivity", "created"):
			if field not in item:
				continue
			value = item[field]
			valid = isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]*", value))
			if field == "sensitivity":
				valid = isinstance(value, str) and value in ("personal", "company")
			elif field == "created":
				value = iso_date(value)
				valid = value is not None
			if not valid:
				error(name, field_line(lines, field, line, stop), f"{field} 값 형식 오류")
			values[field] = value
		result[path] = values
	return result


def tags_map() -> dict[str, str]:
	name = "r1-tags.toml"
	data, text = toml_file(name)
	mapping = data.get("map", {})
	if set(data) - {"map"} or not isinstance(mapping, dict):
		error(name, 1, "[map] 표가 필요합니다")
	for index, (old, new) in enumerate(mapping.items(), 2):
		if not isinstance(new, str) or not old or not new or any(c in old + new for c in "\r\n\x00"):
			line = next((i + 1 for i, row in enumerate(text.splitlines())
				if re.match(r"\s*(?:'" + re.escape(old) + "'|\"" + re.escape(old) + "\"|" + re.escape(old) + r")\s*=", row)), index)
			error(name, line, "태그 대응 값은 비어 있지 않은 한 줄 문자열이어야 합니다")
	return mapping


def old_dates() -> tuple[dict[str, str], dict[str, str]]:
	path = child(home(), OLD_DATES)
	if not path.exists():
		return {}, {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
			raise ValueError
		by_path, by_hash = {}, {}
		for item in data["notes"]:
			old = relative(item["path"])
			if protected(old) or EXCLUDED.intersection(old.parts):
				continue
			days = []
			for field in ("birth", "mtime"):
				try:
					days.append(datetime.fromisoformat(item[field]).date().isoformat())
				except (ValueError, TypeError, KeyError):
					pass
			if not days:
				continue
			day = min(days)
			name, digest = old.as_posix(), item.get("sha256", "")
			by_path[name] = min(by_path.get(name, day), day)
			if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
				digest = digest.lower()
				by_hash[digest] = min(by_hash.get(digest, day), day)
		return by_path, by_hash
	except (ValueError, TypeError, KeyError, ConfigError) as exc:
		raise ConfigError("옛 볼트 날짜 목록 형식 오류") from exc


def created(path: Path, digest: str, dates) -> tuple[str | None, str]:
	by_path, by_hash = dates
	if path.as_posix() in by_path:
		return by_path[path.as_posix()], "옛 볼트 경로"
	if digest in by_hash:
		return by_hash[digest], "옛 볼트 내용"
	match = re.match(r"^(\d{2})(\d{2})(\d{2})-", path.name)
	if match:
		day = iso_date(f"20{match[1]}-{match[2]}-{match[3]}")
		if day:
			return day, "파일 이름"
	for match in re.finditer(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", path.stem):
		day = iso_date(match[0])
		if day:
			return day, "파일 이름"
	return None, "없음"
