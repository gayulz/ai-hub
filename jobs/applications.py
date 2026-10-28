"""수동 공고와 마감·상태를 저장한다."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from collector.backfill_common import sha

from . import materials, storage

STATES = ("접수", "자료 확인 대기", "초안 작성", "검토 대기", "막힘")
UNKNOWN = "미확인"


def field(value, label, maximum=200):
	if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
		raise ValueError(label + " 입력을 확인하세요")
	return value.strip()


def url_key(url):
	if url == UNKNOWN:
		return ""
	url = field(url, "공고 URL", 4096)
	try:
		parts = urlsplit(url)
		if (parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username
				or parts.password or any(c.isspace() for c in url) or parts.port == 0):
			raise ValueError
	except ValueError:
		raise ValueError("공고 URL 형식이 잘못됐습니다") from None
	return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def deadline(value):
	if value == UNKNOWN:
		return value
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		if parsed.tzinfo is None or parsed.utcoffset() is None:
			raise ValueError
	except (ValueError, TypeError, AttributeError):
		raise ValueError("마감은 시간대를 포함한 ISO 시각 또는 미확인으로 입력하세요") from None
	return parsed.isoformat()


def checked(row):
	try:
		storage.identity(row["id"], "a")
		if (url_key(row["url"]) != row["url_key"] or sha(materials.text_value(row["body"])) != row["body_sha256"]
				or deadline(row["deadline"]) != row["deadline"] or row["status"] not in STATES
				or not isinstance(row["documents"], list)):
			raise ValueError
		for key in ("company", "role", "created_at", "updated_at"):
			field(row[key], key)
		ids = [storage.identity(doc["id"], "v") for doc in row["documents"]]
		if len(ids) != len(set(ids)):
			raise ValueError
		return row
	except (KeyError, TypeError, ValueError, UnicodeError):
		raise ValueError("공고 기록이 잘못됐습니다") from None


def records(root):
	return [checked(row) for row in storage.records(root, "applications", "a")]


def get(root, key):
	return checked(storage.get(root, "applications", key, "a"))


def register(root, url, text, company, role, due):
	url = field(url, "공고 URL", 4096)
	key, digest = url_key(url), sha(materials.text_value(text))
	company, role, due = field(company, "회사"), field(role, "직무"), deadline(due)
	for row in records(root):
		if (key and row.get("url_key") == key) or row.get("body_sha256") == digest:
			return row, False
	row = {"id": storage.new_id("a"), "url": url, "url_key": key, "body": text,
		"body_sha256": digest, "company": company, "role": role, "deadline": due,
		"created_at": storage.now(), "updated_at": storage.now(), "status": "접수", "documents": []}
	storage.save(root, "applications", row, "a")
	storage.event(root, "접수")
	return row, True


def set_status(root, row, status, version_id="", error=""):
	checked(row)
	if status not in STATES:
		raise ValueError("지원 상태가 잘못됐습니다")
	row.update(status=status, updated_at=storage.now())
	storage.save(root, "applications", row, "a")
	storage.event(root, status, version_id, error)


def deadlines(root):
	rows = records(root)
	def order(row):
		value = row["deadline"]
		return (1, datetime.max.replace(tzinfo=timezone.utc)) if value == UNKNOWN else (
			0, datetime.fromisoformat(value).astimezone(timezone.utc))
	return sorted(rows, key=order)
