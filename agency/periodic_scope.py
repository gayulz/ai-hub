"""사용자가 선택한 부분 보고 기준과 이전 최종 검수를 보존한다."""
import json
import tempfile
from pathlib import Path

from collector.backfill_common import locked
from vaultfilter.public import Rules
from . import documents, inputs, periodic_facts, periodic_recovery, project
from .departments import load_departments


def _encoded(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _scope(value):
	if not isinstance(value, dict) or set(value) != {"goal", "criteria"}:
		raise ValueError("부분 보고 기준이 잘못됐습니다")
	criteria = value["criteria"]
	if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
		raise ValueError("부분 보고의 완료 기준이 필요합니다")
	for text in [value["goal"], *criteria]:
		periodic_facts._text(text, Rules(), 300)
	return value


def _failed(row):
	from . import acceptance
	history = row.get("periodic_final_reviews")
	if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
		raise ValueError("이전 최종 검수 기록이 없습니다")
	last = history[-1]
	answer = last.get("answer")
	if (last.get("state") != "complete" or not acceptance._valid(answer, row["spec"]["done_criteria"])
			or inputs._hash(_encoded(answer)) != last.get("answer_sha256")
			or all(item["verdict"] == "pass" for item in answer["criteria"])):
		raise ValueError("이전 실패 또는 미검증 판정을 확인할 수 없습니다")


def verify(row):
	anchor = row.get("periodic_scope")
	if anchor is None:
		return None
	if not isinstance(anchor, dict) or set(anchor) != {"path", "sha256"}:
		raise ValueError("부분 보고 보존 기록이 잘못됐습니다")
	path = Path(anchor["path"])
	folder = project._folder(row["id"])
	if (not periodic_recovery._inside(path, folder) or path.name != "manifest.json"
			or not path.parent.name.startswith("periodic-scope-")):
		raise ValueError("부분 보고 보존 경로가 잘못됐습니다")
	raw = periodic_facts.read_file(path)
	if inputs._hash(raw) != anchor["sha256"]:
		raise ValueError("부분 보고 보존 기록이 바뀌었습니다")
	manifest = periodic_facts.parse(raw)
	if (not isinstance(manifest, dict) or set(manifest) != {"project", "scope", "original_sha256", "report_sha256"}
			or manifest["project"] != row["id"]):
		raise ValueError("부분 보고 프로젝트가 다릅니다")
	_scope(manifest["scope"])
	original = periodic_facts.read_file(path.parent / "original-project.json")
	report = periodic_facts.read_file(path.parent / "previous-report.md")
	if inputs._hash(original) != manifest["original_sha256"] or inputs._hash(report) != manifest["report_sha256"]:
		raise ValueError("이전 프로젝트 또는 검수 보고가 바뀌었습니다")
	old = periodic_facts.parse(original)
	_failed(old)
	if (any(row.get(key) != old.get(key) for key in ("spec", "spec_hash", "tasks", "periodic_recovery", "audit_base"))
			or row.get("periodic_final_reviews", [])[:len(old["periodic_final_reviews"])] != old["periodic_final_reviews"]):
		raise ValueError("확정 명세, 작업 또는 이전 검수 기록이 바뀌었습니다")
	return manifest["scope"]


def initial(row):
	if verify(row) is None:
		return False
	path = Path(row["periodic_scope"]["path"])
	old = periodic_facts.parse(periodic_facts.read_file(path.parent / "original-project.json"))
	return row["periodic_final_reviews"] == old["periodic_final_reviews"]


def guard(row):
	if not row.get("periodic_recovery") or row.get("status") != "검수":
		return
	folder = project._folder(row["id"])
	if (folder / "delivery.json").exists():
		raise ValueError("기존 납품 기록을 덮어쓸 수 없습니다")
	if (folder / "report.md").exists():
		if verify(row) is None:
			raise ValueError("기존 최종 검수를 덮어쓸 수 없습니다")
		archive = Path(row["periodic_scope"]["path"]).parent / "previous-report.md"
		if periodic_facts.read_file(folder / "report.md") != periodic_facts.read_file(archive):
			raise ValueError("기존 최종 검수가 보존본과 다릅니다")


def prepare(identity, scope):
	_scope(scope)
	row = project.load(identity)
	department = load_departments().get(row["department"])
	if (row.get("status") != "막힘" or row.get("blocked_from") != "검수" or not row.get("periodic_recovery")
			or row.get("periodic_scope") or department is None):
		raise ValueError("최종 검수에 막힌 복구 보고만 범위를 바꿀 수 있습니다")
	selected = documents.unchanged_repo(row, department)
	with locked("agency-repo:" + str(selected.path)):
		row = project.load(identity)
		if (row.get("status") != "막힘" or row.get("blocked_from") != "검수" or row.get("periodic_scope")
				or documents.unchanged_repo(row, department).path != selected.path):
			raise ValueError("부분 보고 준비 중 프로젝트가 바뀌었습니다")
		_failed(row)
		if any(task.get("status") != "완료" or not task.get("report_seal") or task.get("commit") for task in row["tasks"]):
			raise ValueError("봉인된 보고 작업만 부분 납품할 수 있습니다")
		documents.manifest(row)
		folder = project._folder(identity)
		if (folder / "delivery.json").exists():
			raise ValueError("기존 납품 기록이 있습니다")
		original = periodic_facts.read_file(folder / "project.json")
		if periodic_facts.parse(original) != row:
			raise ValueError("원장이 읽는 중 바뀌었습니다")
		report = periodic_facts.read_file(folder / "report.md")
		review_text = report.decode("utf-8")
		if any("- " + item["criterion"] + ": " + item["verdict"] not in review_text
				for item in row["periodic_final_reviews"][-1]["answer"]["criteria"]):
			raise ValueError("이전 검수 보고와 판정이 다릅니다")
		manifest = {"project": identity, "scope": scope, "original_sha256": inputs._hash(original),
			"report_sha256": inputs._hash(report)}
		raw = _encoded(manifest)
		destination = Path(tempfile.mkdtemp(prefix="periodic-scope-", dir=folder))
		for name, content in (("original-project.json", original), ("previous-report.md", report), ("manifest.json", raw)):
			periodic_recovery._write(destination / name, content, readonly=True)
		anchor = {"path": str(destination / "manifest.json"), "sha256": inputs._hash(raw)}
		with project._edit(identity, "periodic_scope_approved") as current:
			if current != row or periodic_facts.read_file(folder / "report.md") != report:
				raise ValueError("부분 보고 준비 중 원장 또는 검수가 바뀌었습니다")
			current["periodic_scope"] = anchor
			verify(current)
			current["status"], current["blocked_from"], current["blocked_reason"] = "검수", "", ""
	return {"tasks": [{key: task[key] for key in ("id", "title", "depends_on")} for task in row["tasks"]],
		"size": "small", "plan": "explicit-partial-scope", "meeting": False}
