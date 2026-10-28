"""직접 지정한 개인 자료를 버전별로 저장한다."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from collector.backfill_common import safe, sha

from . import storage

MAX_BYTES = 256_000


def source_file(path):
	path = Path(path).absolute()
	if not safe(path):
		raise ValueError("비밀 경로 또는 링크는 읽지 않습니다")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "rb") as stream:
		if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
			raise ValueError("일반 텍스트 파일을 지정하세요")
		content = stream.read(MAX_BYTES + 1)
	if len(content) > MAX_BYTES:
		raise ValueError("입력 크기 한도입니다")
	try:
		text = content.decode("utf-8")
	except UnicodeError:
		raise ValueError("UTF-8 텍스트 파일이 필요합니다") from None
	return text, path.name, hashlib.sha256(os.fsencode(path)).hexdigest()


def text_value(text):
	if not isinstance(text, str) or not text.strip() or "\x00" in text:
		raise ValueError("빈 내용이나 제어 문자는 허용하지 않습니다")
	try:
		if len(text.encode("utf-8")) > MAX_BYTES:
			raise ValueError("입력 크기 한도입니다")
	except UnicodeError:
		raise ValueError("UTF-8 텍스트가 필요합니다") from None
	return text


def checked(row):
	try:
		storage.identity(row["id"], "m")
		text_value(row["content"])
		if (sha(row["content"]) != row["sha256"] or type(row["ai_allowed"]) is not bool
				or type(row["version"]) is not int or row["version"] < 1
				or not isinstance(row["source"], str) or not row["source"]
				or not isinstance(row["registered_at"], str) or not row["registered_at"]
				or not isinstance(row["source_key"], str)):
			raise ValueError
		if row["previous_id"]:
			storage.identity(row["previous_id"], "m")
		return row
	except (KeyError, TypeError, ValueError, UnicodeError):
		raise ValueError("개인 자료 기록이 잘못됐습니다") from None


def records(root):
	return [checked(row) for row in storage.records(root, "materials", "m")]


def add(root, text, source="직접 입력", source_key="", previous=""):
	text_value(text)
	rows = records(root)
	prior = None
	if previous:
		prior = checked(storage.get(root, "materials", previous, "m"))
		if any(row.get("previous_id") == previous for row in rows):
			raise ValueError("가장 최근 자료 버전을 지정하세요")
		source_key = prior["source_key"]
	elif source_key:
		matching = [row for row in rows if row.get("source_key") == source_key]
		prior = max(matching, key=lambda row: row["version"], default=None)
	if prior and prior["sha256"] == sha(text):
		return prior, False
	row = {"id": storage.new_id("m"), "source": source, "source_key": source_key,
		"registered_at": storage.now(), "sha256": sha(text), "content": text,
		"ai_allowed": False, "previous_id": prior["id"] if prior else "",
		"version": prior["version"] + 1 if prior else 1}
	storage.save(root, "materials", row, "m")
	storage.event(root, "자료 등록", row["id"])
	return row, True


def permission(root, key, allowed):
	row = checked(storage.get(root, "materials", key, "m"))
	row["ai_allowed"] = allowed
	storage.save(root, "materials", row, "m")
	storage.event(root, "자료 허용" if allowed else "자료 허용 해제", key)
	return row


def allowed(root):
	rows = records(root)
	old = {row.get("previous_id") for row in rows if row.get("previous_id")}
	return [row for row in rows if row.get("id") not in old and row.get("ai_allowed") is True]
