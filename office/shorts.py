"""숏폼 접수만 확정한다. 원본·납품 폴더와 제작 엔진에는 쓰지 않는다."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector.config import ConfigError, home, load_sources
from orchestrator import question_cards
from orchestrator.draft import EMAIL, string
from vaultfilter.public import Rules

from .common import action_id, normal, regular, unavailable
from .meetings import masked
from .shorts_steps import STEPS

ACTIONS = ("shorts_intake",)
KEYS = {"action", "id", "story", "source", "save", "conditions"}
PRESETS = {"pictures": ("사진", "Pictures"), "movies": ("영상", "Movies"),
	"desktop": ("바탕화면", "Desktop"), "downloads": ("다운로드", "Downloads"),
	"documents": ("문서", "Documents"), "icloud": ("iCloud Drive", "Library/Mobile Documents/com~apple~CloudDocs")}
SAVE_PRESETS = ("movies", "desktop", "documents")
# shortcut: 8000 코드 포인트는 임시 방어값이며 정식 데이터 계약은 3c-2에서 정한다.
STORY_MAX = 8000
CONFIRMED = "접수를 확정했습니다. 제작은 따로 시작합니다."
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, ConfigError, RecursionError)


class Invalid(ValueError):
	def __init__(self, message: str, field: str = "summary"):
		super().__init__(message)
		self.field = field


class Conflict(ValueError):
	pass


def steps() -> dict:
	def presets(saving=False):
		return [{"id": key, "label": label} for key, (label, part) in PRESETS.items()
			if (not saving or key in SAVE_PRESETS) and not (Path.home() / part).is_symlink()
			and (Path.home() / part).is_dir()]
	return {"steps": STEPS, "source_presets": presets(), "save_presets": presets(True)}


def checked_text(value, field: str, maximum: int, rules: Rules) -> str:
	try:
		if isinstance(value, str) and re.search(re.escape(str(Path.home())) + r"(?=$|[/\\\s])", value):
			raise ValueError
		return question_cards.text(value, "접수 내용", maximum, rules=rules)
	except (ValueError, TypeError):
		raise Invalid("글의 길이·형식 또는 회사·서비스 이름, 비밀값, 이메일·홈 경로가 포함돼 거부했습니다.", field) from None


def resolve_path(selection, rules: Rules, *, saving=False, source: Path | None = None) -> Path:
	"""프리셋·직접 경로의 확장과 원본 보호 검사를 이곳에서 함께 한다."""
	field = "save" if saving else "source"
	def reject(message):
		raise Invalid(message, field)
	def real(value):
		return Path(unicodedata.normalize("NFC", os.path.realpath(value)))
	def within(candidate, folder):
		# 없는 저장 폴더도 검사하고, 존재하는 조상은 파일 신원으로 비교한다.
		if candidate.is_relative_to(folder):
			return True
		for parent in (candidate, *candidate.parents):
			try:
				if os.path.samefile(parent, folder):
					return True
			except FileNotFoundError:
				continue
		return False
	try:
		if not isinstance(selection, dict):
			raise ValueError
		if selection.get("kind") == "preset":
			question_cards.exact(selection, ("kind", "id"))
			key = selection["id"]
			if not isinstance(key, str) or key not in PRESETS or (saving and key not in SAVE_PRESETS):
				raise ValueError
			raw = str(Path.home() / PRESETS[key][1])
		else:
			question_cards.exact(selection, ("kind", "value"))
			if selection["kind"] != "path":
				raise ValueError
			raw = string(selection["value"], "폴더", 4096)
		# 경로에만 홈 경로 검사를 제외한다. 이름·비밀값·이메일·제어 문자는 그대로 검사한다.
		if (any(kind != "홈 경로" for kind in rules.kinds(raw)) or EMAIL.search(raw)
				or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
			reject("폴더 이름에 회사·서비스 이름, 비밀값 또는 이메일이 포함돼 거부했습니다.")
		if not (raw.startswith("~/") or Path(raw).is_absolute()):
			reject("홈 줄임 표기 또는 절대 경로로 입력해 주세요.")
		given = Path(unicodedata.normalize("NFC", os.path.expanduser(raw)))
		path, user = real(given), real(Path.home())
		if path == user or not path.is_relative_to(user):
			reject("사용자 홈 아래의 폴더만 사용할 수 있습니다.")
		if any(part.startswith("\U0001f510") for p in (given, path) for part in p.parts):
			reject("보호된 이름의 폴더는 사용할 수 없습니다.")
		if any(within(path, real(p)) for p in (home(), Path.home() / ".ai-hub")):
			reject("작업 기록 폴더는 사용할 수 없습니다.")
		try:
			vault = load_sources(regular(home() / "sources.toml", home())).vault
		except ERRORS:
			reject("볼트 위치 설정을 확인할 수 없어 폴더를 허용하지 않습니다.")
		if within(path, real(vault)):
			reject("볼트 폴더는 사용할 수 없습니다.")
		for candidate in (given, path):
			for parent in (candidate, *candidate.parents):
				if parent == user:
					break
				if os.path.lexists(parent / ".git"):
					reject("Git 저장소 안의 폴더는 사용할 수 없습니다.")
		if given.is_symlink():
			reject("폴더 자체가 심볼릭 링크이면 사용할 수 없습니다.")
		if selection["kind"] == "preset" and not path.is_dir():
			reject("선택한 기본 폴더가 없습니다. 폴더를 다시 선택해 주세요.")
		if saving:
			if (path.exists() and not path.is_dir()) or (not path.exists() and not path.parent.is_dir()):
				reject("저장 폴더 또는 그 부모가 존재하는 폴더여야 합니다.")
			if source is not None and (within(path, source) or within(source, path)):
				reject("원본 보호를 위해 소스와 저장 폴더는 서로 포함할 수 없습니다.")
		elif not path.is_dir():
			reject("소스는 존재하는 폴더여야 합니다.")
		return path
	except Invalid:
		raise
	except ERRORS:
		reject("폴더 선택의 형식이나 접근 권한을 확인해 주세요.")


def validate(payload: dict) -> dict:
	try:
		question_cards.exact(payload, KEYS)
		if payload["action"] != "shorts_intake":
			raise ValueError
		action_id(payload["action"], payload["id"])
	except (ValueError, TypeError, KeyError):
		raise Invalid("허용된 접수 동작과 본문 키·식별자가 필요합니다.") from None
	try:
		rules = Rules()
	except ERRORS:
		raise Invalid("글 검사 설정을 확인할 수 없어 접수를 거부했습니다.", "story") from None
	story = checked_text(payload["story"], "story", STORY_MAX, rules)
	values = payload["conditions"]
	by_id, seen = {step["id"]: step for step in STEPS}, {}
	try:
		if not isinstance(values, list) or len(values) != len(STEPS):
			raise ValueError
		for row in values:
			question_cards.exact(row, ("id", "value"))
			key = row["id"]
			if not isinstance(key, str) or key not in by_id or key in seen:
				raise ValueError
			seen[key] = checked_text(row["value"], key, 300, rules)
	except Invalid:
		raise
	except (ValueError, TypeError, KeyError):
		raise Invalid("모든 제작 조건을 빠짐없이 한 번씩 확인해 주세요.", "conditions") from None
	source = resolve_path(payload["source"], rules)
	save = resolve_path(payload["save"], rules, saving=True, source=source)
	return {"story": story, "source_dir": str(source), "save_dir": str(save),
		"conditions": [{"id": step["id"], "label": step["label"], "value": seen[step["id"]]} for step in STEPS]}


@contextmanager
def directory(path: Path):
	"""상위 경로까지 링크를 따라가지 않는 디렉터리 핸들."""
	path = path.absolute()
	fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
	try:
		for part in path.parts[1:]:
			child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
			os.close(fd)
			fd = child
		yield fd
	finally:
		os.close(fd)


def store(root: Path, data: dict) -> None:
	with directory(root) as base:
		try:
			os.mkdir("shorts", mode=0o700, dir_fd=base)
		except FileExistsError:
			pass
		parent = os.open("shorts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
	try:
		os.fchmod(parent, 0o700)
		if data["id"] in os.listdir(parent):
			raise Conflict("이미 접수한 요청입니다")
		name = ".intake-" + uuid.uuid4().hex
		os.mkdir(name, mode=0o700, dir_fd=parent)
		folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
		try:
			os.fchmod(folder, 0o700)
			fd = os.open("intake.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
			with os.fdopen(fd, "w", encoding="utf-8") as stream:
				os.fchmod(stream.fileno(), 0o600)
				json.dump(data, stream, ensure_ascii=False, allow_nan=False)
				stream.flush()
				os.fsync(stream.fileno())
			os.rename("intake.tmp", "intake.json", src_dir_fd=folder, dst_dir_fd=folder)
			os.fsync(folder)
			# 완성된 폴더를 게시하므로 기존 intake.json이 있는 폴더는 원자적으로 덮어쓸 수 없다.
			if data["id"] in os.listdir(parent):
				raise Conflict("이미 접수한 요청입니다")
			try:
				os.rename(name, data["id"], src_dir_fd=parent, dst_dir_fd=parent)
			except OSError:
				if data["id"] in os.listdir(parent):
					raise Conflict("이미 접수한 요청입니다") from None
				raise
			os.fsync(parent)
		finally:
			for temporary in ("intake.tmp", "intake.json"):
				if name in os.listdir(parent):
					try:
						os.unlink(temporary, dir_fd=folder)
					except FileNotFoundError:
						pass
			os.close(folder)
			if name in os.listdir(parent):
				os.rmdir(name, dir_fd=parent)
	finally:
		os.close(parent)


def execute(root: Path, payload: dict, audit) -> tuple[int, dict]:
	started, status, code, field = time.monotonic(), 200, 0, "summary"
	try:
		with directory(root) as base:
			if "shorts" in os.listdir(base):
				with directory(root / "shorts") as parent:
					if payload["id"] in os.listdir(parent):
						raise Conflict("이미 접수한 요청입니다")
		data = {"version": 1, "id": payload["id"], "created": datetime.now().astimezone().isoformat(timespec="seconds"),
			"status": "intake_confirmed", **validate(payload), "confirmed_by": "office"}
		store(root, data)
		message = CONFIRMED
	except Invalid as exc:
		status, code, message, field = 400, 2, str(exc), exc.field
	except Conflict as exc:
		status, code, message = 409, 2, str(exc)
	except ERRORS:
		status, code, message = 409, 2, "접수 기록을 확인할 수 없습니다. 최근 접수를 확인해 주세요."
	entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": "shorts_intake", "id": payload["id"],
		"exit_code": code, "seconds": round(time.monotonic() - started, 3), "ok": code == 0,
		"message": CONFIRMED if code == 0 else "접수 요청을 처리하지 못했습니다."}
	try:
		audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		status, code, message = 500, 2, "활동 기록 저장에 실패했습니다. 최근 접수를 확인해 주세요."
	if code == 0:
		return status, {"ok": True, "message": message, "draft": {key: data[key] for key in ("id", "status", "created")}}
	return status, {"ok": False, "message": message, "field": field}


def read_state(root: Path) -> dict:
	items, broken = [], False
	try:
		with directory(root / "shorts") as parent:
			rules = Rules()
			for name in os.listdir(parent):
				if name.startswith("."):
					continue
				try:
					action_id("shorts_intake", name)
					folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
					try:
						fd = os.open("intake.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
					finally:
						os.close(folder)
					with os.fdopen(fd, "r", encoding="utf-8") as stream:
						info = os.fstat(stream.fileno())
						if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
							raise ValueError
						row = json.load(stream)
					question_cards.exact(row, ("version", "id", "created", "status", "story", "source_dir", "save_dir", "conditions", "confirmed_by"))
					if (type(row["version"]) is not int or row["version"] != 1 or row["id"] != name
							or row["status"] != "intake_confirmed" or row["confirmed_by"] != "office"):
						raise ValueError
					created = datetime.fromisoformat(row["created"])
					if created.tzinfo is None:
						raise ValueError
					for key in ("source_dir", "save_dir"):
						if not isinstance(row[key], str) or not Path(row[key]).is_absolute():
							raise ValueError
					if not isinstance(row["conditions"], list) or len(row["conditions"]) != len(STEPS):
						raise ValueError
					for value, step in zip(row["conditions"], STEPS):
						question_cards.exact(value, ("id", "label", "value"))
						if value["id"] != step["id"] or value["label"] != step["label"]:
							raise ValueError
						string(value["value"], "조건", 300)
					story = string(row["story"], "이야기", STORY_MAX)
					preview = masked(question_cards.HOME_PATH.sub("[경로 생략]", story), rules)
					items.append((created, {"id": name, "created": row["created"], "status": row["status"], "story_preview": preview[:40]}))
				except ERRORS:
					broken = True
	except FileNotFoundError:
		pass
	except ERRORS:
		broken = True
	items.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
	return {**(unavailable("일부 접수 기록을 확인할 수 없습니다") if broken else normal()), "items": [row for _, row in items[:20]]}
