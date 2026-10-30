"""격리 시험 기록을 정기 점검 근거에 연결한다."""

import hashlib
import json
import re
from pathlib import Path

from . import inputs


def attach(base: bytes, evidence_dir: Path, name: str) -> bytes:
	if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}(?:-retry-[0-9a-f]{64})?\.json", name):
		raise ValueError("격리 시험 기록 이름이 잘못됐습니다")
	try:
		raw = inputs._read(evidence_dir / "isolation" / name, inputs.MAX_OUTPUT)
		result = json.loads(raw)
		value = json.loads(base)
		counts = result["tests"]
		if (not isinstance(result, dict) or result.get("identity") != name[:64]
				or result.get("executed") is not True or result.get("status") not in ("passed", "failed")
				or not isinstance(counts, dict) or set(counts) != {"ran", "failures", "errors", "skipped"}
				or any(type(number) is not int or number < 0 for number in counts.values())
				or not any(item["kind"] == "tests" for item in value["checks"])):
			raise ValueError
		process = result.get("process")
		if not isinstance(process, dict) or process.get("status") not in ("passed", "failed"):
			raise ValueError
		passed = bool(process["status"] == "passed" and counts["ran"] and not counts["failures"] and not counts["errors"])
		if result.get("ok") is not passed or (result["status"] == "passed") != passed:
			raise ValueError
		value["synthetic_test"] = {"name": name, "sha256": hashlib.sha256(raw).hexdigest(),
			"status": result["status"], "counts": counts}
		return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
	except (OSError, ValueError, TypeError, KeyError):
		raise ValueError("격리 시험 기록을 확인할 수 없습니다") from None


def execute(evidence_dir: Path, policy, command: tuple[str, ...]) -> str | None:
	from orchestrator.test_isolation import run_isolated
	evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
	result = run_isolated(policy, command, evidence_dir / "isolation")
	identity = result.get("identity")
	if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
		return None
	if result.get("executed") is True and result.get("status") in ("passed", "failed"):
		return identity + ".json"
	if result.get("reason") == "같은 조건의 시험 기록이 있어 자동으로 반복하지 않습니다":
		try:
			previous = json.loads(inputs._read(evidence_dir / "isolation" / (identity + ".json"), inputs.MAX_OUTPUT))
			if previous.get("identity") == identity and previous.get("executed") is True and previous.get("status") in ("passed", "failed"):
				return identity + ".json"
		except (OSError, ValueError, TypeError, AttributeError):
			pass
	return None


def verify(base: bytes, actual: bytes, evidence_dir: Path) -> dict:
	try:
		value = json.loads(actual)
		test = value.get("synthetic_test")
		expected = base if test is None else attach(base, evidence_dir, test["name"])
		if actual != expected:
			raise ValueError
		return value
	except (ValueError, TypeError, KeyError, AttributeError):
		raise ValueError("점검 근거 또는 요청서가 바뀌었습니다") from None


def summary(value: dict) -> str:
	test = value.get("synthetic_test")
	if not test:
		return ""
	counts = test["counts"]
	status = "통과" if test["status"] == "passed" else "실패"
	return ("\n\n## 합성 격리 시험\n\n- " + status + ": 실행 " + str(counts["ran"])
		+ "개, 실패 " + str(counts["failures"]) + "개, 오류 " + str(counts["errors"])
		+ "개, 건너뜀 " + str(counts["skipped"]) + "개. 실제 저장소 전체 시험은 미검증입니다.")


def prepare(identity, task, target, commit):
	from orchestrator import isolated_inputs
	from . import documents
	base = documents._unverified_evidence(identity, task)
	if (documents.evidence_path(identity, task["id"]).exists() or not target.isolated_inputs
			or not any(item["kind"] == "tests" for item in json.loads(base)["checks"])):
		return documents.prepare_evidence(identity, task)
	try:
		files = isolated_inputs.snapshot(target, commit=commit)
		policy, command = isolated_inputs.policy(target, files)
	except (OSError, ValueError, RuntimeError):
		return documents.prepare_evidence(identity, task)
	return documents.prepare_evidence(identity, task, policy=policy, command=command)
