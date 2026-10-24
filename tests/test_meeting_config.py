"""역할별 명시 설정과 AI 호출 전 안전 경계."""

import json
import os
from unittest import mock

from orchestrator import meeting_prompts
from collector.config import ConfigError
from test_meeting import MeetingCase


class MeetingConfigTest(MeetingCase):
	def test_missing_invalid_company_or_unsupported_plan_costs_zero_calls(self):
		cases = [self.plan_text.split("[orchestration.meeting]")[0],
			self.plan_text.replace('qa=["p01"]', 'qa=["missing"]'),
			self.plan_text.replace('owner="personal"', 'owner="company"'),
			self.plan_text.replace('allow=["personal"]', 'allow=["study"]'),
			self.plan_text.replace('qa=["p01"]', 'company=["p01"]'),
			self.plan_text.replace('qa=["p01"]', 'qa="p01"'),
			self.plan_text.replace('tool="codex"', 'tool="ollama"\nmodel="m01"'),
			self.plan_text.replace('implementer=["p01"]', 'planner=["missing"]\nimplementer=["p01"]')]
		for text in cases:
			with self.subTest(config=text[-55:]):
				self.write_plans(text)
				with self.assertRaises((ValueError, ConfigError)):
					self.analyze()
		self.mock_call.assert_not_called()

	def test_unsafe_input_target_or_oversize_never_calls(self):
		inputs = ["", "a" * 2001, "synthetic-corp 안건", "password=" + "b" * 24,
			"/" + "Users/other-user/data", "mail " + "user" + "@" + "example.invalid", "a\x00b",
			"~" + "/Obsidian/notes", "~" + "/workspace/project"]
		for text in inputs:
			with self.subTest(text=text[:15]), self.assertRaises(ValueError):
				self.engine.analyze(text, "ai-hub")
		with self.assertRaises(ValueError):
			self.engine.analyze("안건", "unlisted")
		self.mock_call.assert_not_called()

	def test_canonical_home_aliases_and_sensitive_values_are_masked_for_every_call(self):
		values = ["~" + "/.ai-hub/settings", "~" + "/Obsidian/notes", "~" + "/workspace/project",
			"`~" + "/Obsidian/local notes/private`", '"~' + '/workspace/local projects/private"',
			"/" + "home/other-user/private", "/" + "Users/other-user/private",
			"C:" + "\\Users\\other-user\\private", "synthetic-corp", "SYNTHETIC-CORP",
			"synthetic-user", "password=" + "s" * 24, "ghp_" + "s" * 36,
			"user" + "@" + "example.invalid"]
		(self.repo / "RULES.md").write_text("개인 정본을 유지한다.\n" + "\n".join(values) + "\n원천을 바꾸지 않는다.")
		state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		for _, call in self.ai.calls:
			prompt = call["prompt"]
			canonical = json.loads(prompt.rsplit("\n", 1)[1])["정본"]["RULES.md"]
			self.assertIn("개인 정본을 유지한다.", canonical)
			self.assertIn("원천을 바꾸지 않는다.", canonical)
			self.assertIn("[보안값]", canonical)
			for value in values:
				self.assertNotIn(value, prompt)
				self.assertNotIn(value, canonical)
			self.assertNotIn("private", canonical)

	def test_bounded_large_canonical_rules_are_sent_whole_without_truncation(self):
		value = "첫 규칙을 유지한다.\n" + "합성 규칙은 읽기 전용이다.\n" * 11000 + "마지막 규칙도 반드시 유지한다."
		self.assertGreaterEqual(len(value), 162011)
		self.assertGreaterEqual(len(value.encode("utf-8")), 315968)
		(self.repo / "RULES.md").write_text(value)
		state = self.start(self.analyze())
		self.assertEqual(state["status"], "completed")
		for _, call in self.ai.calls:
			canonical = json.loads(call["prompt"].rsplit("\n", 1)[1])["정본"]["RULES.md"]
			self.assertEqual(canonical, value)

	def test_oversized_raw_rules_combined_prompt_or_control_characters_cost_zero_calls(self):
		for value in ("a" * 512001, "가" * 180000, "금지\x01문자"):
			(self.repo / "RULES.md").write_text(value)
			with self.assertRaises(ValueError):
				self.analyze()
		(self.repo / "RULES.md").write_text("규칙\n" * 64000)
		(self.repo / "AGENTS.md").write_text("규칙\n" * 64000)
		with self.assertRaises(ValueError):
			self.analyze()
		self.mock_call.assert_not_called()

	def test_hardlinked_canonical_rules_cost_zero_calls(self):
		os.link(self.repo / "RULES.md", self.root / "linked-rules.md")
		with self.assertRaises(ValueError):
			self.analyze()
		self.mock_call.assert_not_called()

	def test_rule_growth_after_stat_is_rejected_before_masking_or_truncation(self):
		path = self.repo / "RULES.md"
		info = path.stat()
		path.write_text("~" + "/workspace/" + "a" * 512001)
		with mock.patch("orchestrator.meeting_prompts.os.fstat", return_value=info), self.assertRaises(ValueError):
			self.analyze()
		self.mock_call.assert_not_called()

	def test_moderator_reuses_planner_and_explicit_model_or_implementer(self):
		self.write_plans(self.plan_text.replace('implementer=["p01"]',
			'planner=["p01"]\nimplementer=["missing"]\n[orchestration.models]\np01="explicit-model"'))
		state = self.analyze()
		self.assertEqual(state["status"], "participants_pending")
		self.assertEqual(self.ai.calls[0][0].model, "explicit-model")

	def test_settings_removed_after_analysis_do_not_confirm_or_call_participants(self):
		state = self.analyze()
		self.write_plans(self.plan_text.replace('qa=["p01"]', 'qa=["missing"]'))
		with self.assertRaises(ValueError):
			self.start(state)
		stored = self.engine.status(state["id"])
		self.assertFalse(stored["confirmed"])
		self.assertEqual(stored["status"], "participants_pending")
		self.assertEqual(len(self.ai.calls), 1)

	def test_exact_agenda_limit_and_no_model_guess(self):
		state = self.engine.analyze("가" * 2000, "ai-hub")
		self.assertEqual(state["status"], "participants_pending")
		self.assertIsNone(self.ai.calls[0][0].model)

	def test_role_names_match_personal_scene_identifiers(self):
		import json
		from pathlib import Path
		people = json.loads((Path(__file__).resolve().parents[1] / "office/static/scene.json").read_text())["people"]
		for role, person in meeting_prompts.ROLES.items():
			self.assertIn({"name": person["name"], "id": person["id"]},
				[{"name": p["name"], "id": p["id"]} for p in people if p["department"] != "company"])
