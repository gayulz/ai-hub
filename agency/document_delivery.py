"""문서 묶음의 해시와 현재 내용을 확인해 납품을 마친다."""

import hashlib
import json

from collector.backfill_common import locked, now

from . import documents, project
from .departments import load_departments, valid_text

KEYS = {"version", "project", "spec_hash", "documents", "criteria", "reviewer", "made", "hash"}


def validate(identity, bundle):
	try:
		if (not isinstance(bundle, dict) or bundle.keys() != KEYS or type(bundle["version"]) is not int
				or bundle["version"] != 2 or bundle["project"] != identity
				or not valid_text(bundle["reviewer"], 200) or not valid_text(bundle["made"], 100)):
			raise ValueError
		payload = {key: value for key, value in bundle.items() if key != "hash"}
		digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
		if bundle["hash"] != digest:
			raise ValueError
		row = project.load(identity)
		department = load_departments().get(row["department"])
		periodic = (department is not None and department.deliverable == "code"
			and row.get("audit_report_mode") == "explicit-v1" and bool(row.get("schedule_repo")))
		if department is None or not periodic and (department.deliverable != "document" or row["branch"]):
			raise ValueError
		if periodic:
			if any(not task.get("report_seal") or task.get("commit") for task in row["tasks"]):
				raise ValueError("보고 전용 납품에 코드 변경이 있습니다")
			documents.unchanged_repo(row, department)
		spec_hash = hashlib.sha256(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
		if not row["spec_hash"] or bundle["spec_hash"] != row["spec_hash"] or spec_hash != row["spec_hash"]:
			raise ValueError
		criteria = bundle["criteria"]
		if (not isinstance(criteria, list) or len(criteria) != len(row["spec"]["done_criteria"])
				or any(not isinstance(item, dict) or item.keys() != {"criterion", "verdict", "evidence"}
					or item["criterion"] != expected or item["verdict"] != "pass" or not valid_text(item["evidence"], 500)
					for item, expected in zip(criteria, row["spec"]["done_criteria"]))):
			raise ValueError
		if not row["tasks"] or any(task["status"] != "완료" for task in row["tasks"]):
			raise ValueError
		project._check_tasks(row["tasks"])
		if bundle["documents"] != documents.manifest(row):
			raise ValueError("납품 문서가 검수 뒤 바뀌었습니다. 다시 검수하세요")
	except (KeyError, TypeError, UnicodeError, RecursionError):
		raise ValueError("문서 납품 묶음의 형식 또는 hash가 잘못됐습니다") from None
	except ValueError as exc:
		if str(exc):
			raise
		raise ValueError("문서 납품 묶음의 형식 또는 hash가 잘못됐습니다") from None
	return bundle


def approve(identity, bundle_hash, bundle):
	from . import delivery
	workdir = project._folder(identity) / "work"
	row = project.load(identity)
	department = load_departments().get(row["department"])
	periodic = department is not None and department.deliverable == "code" and row.get("audit_report_mode") == "explicit-v1"
	repo = documents.unchanged_repo(row, department).path if periodic else workdir
	with locked("agency-repo:" + str(repo)):
		delivery._waiting(identity)
		if delivery.load_bundle(identity)["hash"] != bundle_hash or bundle["hash"] != bundle_hash:
			raise ValueError("승인 hash가 납품 묶음과 다릅니다")
		delivery._read(project._folder(identity) / "report.md", missing="")
		stamp = now().isoformat(timespec="seconds")
		project.transition(identity, "납품 중")
		paths = [item["path"] for item in bundle["documents"]]
		try:
			delivery._append(identity, "## 납품\n\n- 승인 시각: " + stamp + "\n- 묶음 hash: " + bundle_hash
				+ "\n- 문서:\n" + "\n".join("  - " + path for path in paths))
			if delivery.load_bundle(identity)["hash"] != bundle_hash:
				raise ValueError("납품 묶음이 승인 뒤 바뀌었습니다")
			project.transition(identity, "완료")
		except (OSError, ValueError, RuntimeError):
			project.transition(identity, "막힘", reason="납품 실패: 문서 또는 납품 기록을 확인하세요")
			return {"status": "막힘", "documents": paths}
		return {"status": "완료", "documents": paths}
