"""별도 AGY 텍스트 검토를 고정된 diff와 연결한다."""
import hashlib
import json
import re
from pathlib import Path

from agency.inputs import _read


def _unique(pairs):
	value = {}
	for key, item in pairs:
		if key in value:
			raise ValueError("검토 근거에 중복 키가 있습니다")
		value[key] = item
	return value


def load(path):
	data = _read(Path(path), 65536)
	try:
		value = json.loads(data, object_pairs_hook=_unique)
	except (UnicodeError, ValueError):
		raise ValueError("검토 근거의 JSON 형식이 잘못됐습니다") from None
	if (not isinstance(value, dict) or set(value) != {"run_id", "base_commit", "commit", "diff_sha256", "source", "reviewer", "review"}
			or value["source"] != "agy-text" or not isinstance(value["reviewer"], str)
			or not re.fullmatch(r"[a-z0-9_-]{1,32}", value["reviewer"]) or not isinstance(value["review"], dict)
			or set(value["review"]) != {"verdict", "summary", "findings"}
			or value["review"]["verdict"] != "pass" or value["review"]["findings"] != []
			or not isinstance(value["review"]["summary"], str) or not 1 <= len(value["review"]["summary"]) <= 4000
			or not isinstance(value["diff_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["diff_sha256"])):
		raise ValueError("검토 근거의 판정이나 출처가 잘못됐습니다")
	from vaultfilter.public import Rules
	from .draft import safe_text
	safe_text(value["review"]["summary"], Rules())
	return value, hashlib.sha256(data).hexdigest()


def validate(loaded, run, material):
	from . import checks, test_isolation
	value, digest = loaded
	passed = []
	for item in run.checks:
		counts, isolation = item.get("counts"), item.get("isolation")
		if (item.get("name") == "테스트" and item.get("ok") is True and item.get("executed") is True
				and isinstance(counts, dict) and type(counts.get("ran")) is int and counts["ran"] > 0
				and counts.get("failures") == 0 and counts.get("errors") == 0
				and isinstance(isolation, dict) and isolation.get("status") == "passed"
				and isolation.get("executed") is True and isolation.get("tests") == counts):
			passed.append(item)
	if (run.kind != "code" or not test_isolation.requires(run.repo) or not checks.checks_passed(run.checks) or len(passed) != 1
			or value["run_id"] != run.run_id or value["base_commit"] != run.base_commit
			or value["commit"] != run.handoff.get("reverify_commit")
			or value["diff_sha256"] != hashlib.sha256(material.encode("utf-8")).hexdigest()):
		raise ValueError("검토 근거가 고정 커밋·차이 또는 격리 시험과 일치하지 않습니다")
	review = dict(value["review"])
	review["summary"] = "AGY " + value["reviewer"] + " 별도 텍스트 검토를 근거 파일로 연결: " + review["summary"]
	return review, digest, value["diff_sha256"]
