"""운영자가 제공한 정기 점검 사실과 기존 격리 결과를 대조한다.

사실 JSON에는 version, project, base_commit, date, tests, dependencies를 둔다.
tests에는 command, files, file_count, full_not_run_reason과 partial을 둔다.
partial에는 executed, status, counts, files, record와 policy를 둔다. files는
path, sha256, provenance 목록이고 policy는 기록의 policy_sha256과 일치하는
격리 정책 원문이다. record는 기존 기록의 name과 sha256이다.
dependencies에는 declarations와 전이 의존성, 공지, 지원 상태의 미확인 사유를 둔다.
선언별 항목은 path, lines, installed, installed_unverified이며 설치 버전은
운영자 제공 값으로 표시한다.
"""

import json
import os
import re
import stat
import subprocess
from datetime import date
from pathlib import Path

from collector.backfill_common import safe
from orchestrator import checks, draft, isolated_inputs
from . import inputs

HASH = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def read_file(path):
	path = Path(path)
	if not path.is_absolute() or not safe(path):
		raise ValueError("사실 자료 경로가 올바르지 않습니다")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "rb") as stream:
		info = os.fstat(stream.fileno())
		if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
				or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_nlink != 1):
			raise ValueError("사실 자료는 소유자 전용 일반 파일이어야 합니다")
		raw = stream.read(inputs.MAX_OUTPUT + 1)
		if len(raw) > inputs.MAX_OUTPUT:
			raise ValueError("사실 자료 크기가 한도를 넘었습니다")
		return raw


def parse(raw):
	def unique(pairs):
		result = {}
		for key, value in pairs:
			if key in result:
				raise ValueError("중복된 사실 항목입니다")
			result[key] = value
		return result
	try:
		return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
	except (ValueError, UnicodeError, RecursionError):
		raise ValueError("사실 자료의 JSON 형식이 잘못됐습니다") from None


def _text(value, rules, maximum=1000):
	if (not isinstance(value, str) or not value.strip() or len(value) > maximum
			or any(ord(char) < 32 or ord(char) == 127 for char in value)
			or re.search(r"(?<![A-Za-z0-9_])(?:/Users/|/home/|~/)", value)):
		raise ValueError("사실 자료의 글 형식이 잘못됐습니다")
	draft.safe_text(value, rules)
	return value


def _path(value):
	if not isolated_inputs.valid_path(value):
		raise ValueError("사실 자료는 저장소 상대 파일 경로를 사용해야 합니다")
	return value


def _blob(repo, commit, name):
	object_name = commit + ":" + _path(name)
	size = checks.git(repo, "cat-file", "-s", object_name, check=False).strip()
	if not size.isdecimal() or int(size) > inputs.MAX_OUTPUT:
		raise ValueError("기준 커밋의 파일 크기를 확인할 수 없습니다")
	result = subprocess.run(["git", "-C", str(repo), "show", object_name],
		capture_output=True, check=False)
	if result.returncode or len(result.stdout) > inputs.MAX_OUTPUT:
		raise ValueError("기준 커밋에서 선언된 파일을 확인할 수 없습니다")
	try:
		return result.stdout.decode("utf-8")
	except UnicodeError:
		raise ValueError("선언 파일의 글 형식이 잘못됐습니다") from None


def validate(raw, record_raw, record_name, identity, base, selected, rules):
	facts, record = parse(raw), parse(record_raw)
	try:
		if (not isinstance(facts, dict) or set(facts) != {"version", "project", "base_commit", "date", "tests", "dependencies"}
				or type(facts["version"]) is not int or facts["version"] != 1 or facts["project"] != identity
				or facts["base_commit"] != base or not isinstance(base, str) or not COMMIT.fullmatch(base)):
			raise ValueError("사실 자료의 프로젝트 또는 기준 커밋이 다릅니다")
		try:
			if date.fromisoformat(facts["date"]).isoformat() != facts["date"]:
				raise ValueError
		except (TypeError, ValueError):
			raise ValueError("사실 자료의 기준일이 잘못됐습니다") from None
		tests = facts["tests"]
		if not isinstance(tests, dict) or set(tests) != {"command", "files", "file_count", "full_not_run_reason", "partial"}:
			raise ValueError("시험 사실 형식이 잘못됐습니다")
		command = _text(tests["command"], rules, 2000)
		if command != selected.test_command.replace(str(selected.path), "."):
			raise ValueError("등록된 시험 명령과 다릅니다")
		files = tests["files"]
		if (not isinstance(files, list) or not files or len(files) > 10000 or type(tests["file_count"]) is not int
				or tests["file_count"] != len(files) or len(set(files)) != len(files)):
			raise ValueError("시험 파일 목록 또는 개수가 잘못됐습니다")
		for name in files:
			_text(_path(name), rules, 500)
			_blob(selected.path, base, name)
		_text(tests["full_not_run_reason"], rules)
		partial = tests["partial"]
		if not isinstance(partial, dict) or set(partial) != {"executed", "status", "counts", "files", "record", "policy"}:
			raise ValueError("합성 시험 사실 형식이 잘못됐습니다")
		anchor = partial["record"]
		if (not isinstance(anchor, dict) or set(anchor) != {"name", "sha256"} or anchor["name"] != record_name
				or not isinstance(anchor["sha256"], str) or not HASH.fullmatch(anchor["sha256"])
				or anchor["sha256"] != inputs._hash(record_raw)
				or not re.fullmatch(r"[0-9a-f]{64}(?:-retry-[0-9a-f]{64})?\.json", record_name)):
			raise ValueError("격리 기록의 이름 또는 해시가 다릅니다")
		counts = partial["counts"]
		if (not isinstance(record, dict) or record.get("identity") != record_name[:64]
				or record.get("project", identity) != identity or record.get("base_commit", base) != base
				or partial["executed"] is not True or record.get("executed") is not True
				or partial["status"] not in ("passed", "failed") or partial["status"] != record.get("status")
				or not isinstance(counts, dict) or set(counts) != {"ran", "failures", "errors", "skipped"}
				or any(type(n) is not int or n < 0 for n in counts.values()) or record.get("tests") != counts
				or counts["failures"] + counts["errors"] + counts["skipped"] > counts["ran"]):
			raise ValueError("합성 시험의 실행 여부 또는 건수가 격리 기록과 다릅니다")
		process = record.get("process")
		if not isinstance(process, dict) or process.get("status") not in ("passed", "failed"):
			raise ValueError("격리 실행 상태가 잘못됐습니다")
		passed = bool(process["status"] == "passed" and counts["ran"] and not counts["failures"] and not counts["errors"])
		if record.get("ok") is not passed or (partial["status"] == "passed") != passed:
			raise ValueError("격리 실행의 통과 판정이 건수와 다릅니다")
		policy = partial["policy"]
		policy_hash = inputs._hash(json.dumps(policy, sort_keys=True).encode())
		if not isinstance(policy, dict) or record.get("policy_sha256") != policy_hash:
			raise ValueError("격리 정책과 실행 기록이 다릅니다")
		items = partial["files"]
		if not isinstance(items, list) or not items or len(items) > 10000:
			raise ValueError("합성 시험 파일 범위가 없습니다")
		for item in items:
			if (not isinstance(item, dict) or set(item) != {"path", "sha256", "provenance"}
					or not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])):
				raise ValueError("합성 시험 파일 해시 형식이 잘못됐습니다")
			_text(_path(item["path"]), rules, 500)
			_text(item["provenance"], rules, 100)
		manifest = sorted([[item["path"], item["sha256"], item["provenance"]] for item in items])
		if len({item["path"] for item in items}) != len(items) or policy.get("files") != manifest:
			raise ValueError("합성 시험 파일 범위가 격리 정책과 다릅니다")
		deps = facts["dependencies"]
		if not isinstance(deps, dict) or set(deps) != {"declarations", "transitive_unverified", "advisories_unverified", "support_unverified"}:
			raise ValueError("의존성 사실 형식이 잘못됐습니다")
		declarations = deps["declarations"]
		if not isinstance(declarations, list) or not declarations or len(declarations) > 100:
			raise ValueError("의존성 선언 파일 목록이 없습니다")
		for entry in declarations:
			if not isinstance(entry, dict) or set(entry) != {"path", "lines", "installed", "installed_unverified"}:
				raise ValueError("의존성 선언 형식이 잘못됐습니다")
			_text(_path(entry["path"]), rules, 500)
			contents = _blob(selected.path, base, entry["path"]).splitlines()
			if not isinstance(entry["lines"], list) or not entry["lines"] or len(entry["lines"]) > 1000:
				raise ValueError("의존성 선언 줄이 없습니다")
			for line in entry["lines"]:
				if _text(line, rules) not in contents:
					raise ValueError("의존성 선언 줄이 기준 커밋과 다릅니다")
			if not isinstance(entry["installed"], list) or len(entry["installed"]) > 1000:
				raise ValueError("설치 버전 목록이 잘못됐습니다")
			for installed in entry["installed"]:
				if not isinstance(installed, dict) or set(installed) != {"name", "version"}:
					raise ValueError("설치 버전 형식이 잘못됐습니다")
				_text(installed["name"], rules, 200)
				_text(installed["version"], rules, 200)
			_text(entry["installed_unverified"], rules)
		if len({entry["path"] for entry in declarations}) != len(declarations):
			raise ValueError("의존성 선언 파일이 중복됐습니다")
		for key in ("transitive_unverified", "advisories_unverified", "support_unverified"):
			_text(deps[key], rules)
		return facts
	except (KeyError, TypeError, AttributeError, RecursionError):
		raise ValueError("사실 자료의 항목이 잘못됐습니다") from None


def evidence(facts, checks):
	value = {"base_commit": facts["base_commit"], "date": facts["date"]}
	if "tests" in checks:
		tests = facts["tests"]
		value["tests"] = {key: tests[key] for key in ("command", "files", "file_count", "full_not_run_reason")}
		value["partial"] = {key: tests["partial"][key] for key in ("executed", "status", "counts", "files", "record")}
	if "dependencies" in checks:
		value["dependencies"] = facts["dependencies"]
	return value


def summary(facts):
	def text(value):
		return value.replace("`", "'")
	lines = ["", "## 고정된 프로젝트 사실", "", "- 기준 커밋: `" + facts["base_commit"] + "`",
		"- 사실 기준일: " + facts["date"]]
	if tests := facts.get("tests"):
		lines += ["- 등록 시험 명령: `" + text(tests["command"]) + "`",
			"- 제공된 시험 파일 " + str(tests["file_count"]) + "개: " + ", ".join("`" + text(name) + "`" for name in tests["files"]),
			"- 실제 저장소 전체 시험 미실행 사유: " + text(tests["full_not_run_reason"])]
		partial = facts["partial"]
		counts = partial["counts"]
		lines += ["- 별도 합성 격리 결과: " + ("통과" if partial["status"] == "passed" else "실패")
			+ ", 실행 " + str(counts["ran"]) + "개, 실패 " + str(counts["failures"]) + "개, 오류 "
			+ str(counts["errors"]) + "개, 건너뜀 " + str(counts["skipped"]) + "개. 전체 시험 결과가 아닙니다.",
			"- 격리 기록: `" + partial["record"]["name"] + "` · SHA-256 `" + partial["record"]["sha256"] + "`"]
		lines += ["- 격리 정책에 포함된 파일: `" + text(item["path"]) + "` · SHA-256 `" + item["sha256"]
			+ "` · 출처 `" + text(item["provenance"]) + "`"
			for item in partial["files"]]
	if deps := facts.get("dependencies"):
		lines += ["", "### 직접 의존성 선언", ""]
		for entry in deps["declarations"]:
			lines += ["- 선언 파일: `" + text(entry["path"]) + "`"]
			lines += ["  - 선언 줄: `" + text(line) + "`" for line in entry["lines"]]
			lines += ["  - 제공된 설치 버전(별도 확인 안 함): " + text(item["name"]) + " " + text(item["version"])
				for item in entry["installed"]]
			lines += ["  - 설치 상태 미확인: " + text(entry["installed_unverified"])]
		lines += ["- 전이 의존성 미확인: " + text(deps["transitive_unverified"]),
			"- 보안 공지 미확인: " + text(deps["advisories_unverified"]),
			"- 지원 상태 미확인: " + text(deps["support_unverified"])]
	lines += ["- 코드 오류의 존재와 수정 완료 여부는 확인하지 않았습니다."]
	return "\n".join(lines) + "\n"
