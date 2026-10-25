"""요약의 정보 경계와 외부 실행 없는 패키지 구성을 검사한다."""

import ast
import copy
import json
import sys
from pathlib import Path
from unittest import mock

from shorts import attempts
from shorts.summary import summary
from test_shorts_support import HASH, ShortsCase


class SummaryTest(ShortsCase):
	def test_review_has_only_safe_artifact_identifiers_and_separate_status_columns(self):
		run = self.pending()
		value = summary(run)
		self.assertEqual(set(value), {"round", "stage", "stage_label", "status", "status_label", "approval_status",
			"attempt_status", "artifacts", "blocked_reason", "next_action"})
		self.assertEqual((value["round"], value["stage_label"], value["approval_status"]), (1, "대본·장면", "awaiting_user"))
		self.assertEqual(value["artifacts"], [{"name": "script.txt", "kind": "script", "identifier": run["artifacts"][0]["sha256"][:12]}])
		self.assertEqual(value["attempt_status"], [])
		self.assertEqual(value["blocked_reason"], "산출물 확인이 필요합니다")

	def test_paths_external_ids_error_text_and_notes_never_leave_summary(self):
		self.to_stage("prepare")
		from shorts import approvals
		self.call(approvals.start_stage)
		row = self.call(attempts.intend, "check_environment", HASH)
		self.call(attempts.mark_running, row["attempt_id"], external_id="remote-raw-001")
		run = self.call(attempts.recover)
		run.update(source_dir=str(self.root), save_dir=str(self.hub), error="외부 도구 오류 원문")
		run["attempts"][0].update(output="외부 출력 원문", resolution={"note": "대조 메모 원문"})
		value = summary(run)
		text = json.dumps(value, ensure_ascii=False)
		for forbidden in (str(self.root), str(self.hub), "remote-raw-001", row["attempt_id"],
				"외부 도구 오류 원문", "외부 출력 원문", "대조 메모 원문"):
			self.assertNotIn(forbidden, text)
		self.assertEqual(value["status"], "needs_check")
		self.assertEqual(value["attempt_status"], [{"status": "needs_check", "description": "외부 작업 결과 미상"}])
		self.assertEqual(value["approval_status"], "none")

	def test_all_string_values_mask_dictionary_secrets_emails_and_any_home_or_absolute_path(self):
		run = self.pending()
		values = ("acme.txt", "xyz.txt", "ghp_" + "a" * 36, "p01" + "@" + "test.invalid",
			"/" + "home" + "/" + "p01" + "/x", "/" + "Users" + "/" + "p02" + "/x",
			str(self.root / "output"), "~/output", "C:\\output", "bad\nvalue")
		for raw in values:
			with self.subTest(value=raw):
				tainted = copy.deepcopy(run)
				tainted["artifacts"][0]["name"] = raw
				tainted["review"]["artifacts"] = [raw]
				self.assertEqual(summary(tainted)["artifacts"][0]["name"], "[가림]")
		with mock.patch("shorts.summary.Rules") as rules:
			rules.return_value.kinds.side_effect = lambda value: ["사전 이름"] if value == "awaiting_user" else []
			value = summary(run)
			self.assertEqual(value["status"], "[가림]")
			self.assertEqual(value["approval_status"], "[가림]")

	def test_missing_rules_fail_closed_and_unknown_block_reason_has_no_raw_echo(self):
		run = self.pending()
		run["blocked_reason"] = "출력해서는 안 되는 오류 원문"
		value = summary(run)
		self.assertEqual(value["blocked_reason"], "차단 사유를 확인해 주세요")
		with mock.patch("shorts.summary.Rules", side_effect=ValueError("합성 설정 실패")):
			value = summary(run)
		self.assertEqual(value["stage"], "[가림]")
		self.assertEqual(value["artifacts"][0]["name"], "[가림]")

	def test_package_has_no_forbidden_imports_external_execution_or_cycles_and_uses_tabs(self):
		package = Path(__file__).resolve().parents[1] / "shorts"
		graph = {}
		for path in package.glob("*.py"):
			text = path.read_text(encoding="utf-8")
			self.assertLessEqual(len(text.splitlines()), 500, path.name)
			self.assertFalse(any(line.startswith(" ") for line in text.splitlines()), path.name)
			self.assertFalse(any(0x1F000 <= ord(c) <= 0x1FAFF for c in text), path.name)
			tree = ast.parse(text)
			graph[path.stem] = set()
			for node in ast.walk(tree):
				if isinstance(node, ast.Import):
					for alias in node.names:
						self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
						self.assertNotIn(alias.name, ("subprocess", "socket", "urllib", "http"))
				if isinstance(node, ast.ImportFrom):
					if node.level:
						graph[path.stem].update([node.module] if node.module else [a.name for a in node.names])
					elif node.module in ("collector.config", "vaultfilter.public"):
						self.assertEqual([a.name for a in node.names], ["home" if node.module == "collector.config" else "Rules"])
					else:
						self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
				if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
					self.assertNotIn(node.func.attr, ("system", "popen", "Popen", "urlopen", "create_connection"))
		def visit(name, active):
			self.assertNotIn(name, active, "패키지 순환 import")
			for child in graph.get(name, ()):
				visit(child, active | {name})
		for name in graph:
			visit(name, set())
