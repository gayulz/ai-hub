"""훅·전역 규칙 여섯 곳의 추가 설치·반복 실행·읽기 전용 점검."""

import dataclasses
import json
import subprocess
import sys

from collector.hooks import EVENTS
from hubsetup import config, records
from test_hubsetup import HubSetupCase


class C2SetupTest(HubSetupCase):
	def existing(self):
		originals = {}
		for tool, paths in records.PATHS.items():
			path = self.user / paths[0]
			path.parent.mkdir(parents=True, exist_ok=True)
			groups = [{"matcher": ".*", "hooks": [
				{"type": "command", "command": "acme-first", "timeout": 10},
				{"type": "command", "command": "xyz-second", "statusMessage": "보존"}]}]
			data = {"hooks": {event: groups for event in EVENTS[tool]},
				"env": {"ACME_TEST_SETTING": "합성 비공개 설정"}, "notify": ["acme-notify"]}
			data["hooks"]["PostToolUse" if tool == "codex" else "AfterTool"] = groups
			path.write_text(json.dumps(data, ensure_ascii=False))
			originals[tool] = data
			if tool != "claude":
				(self.user / paths[1]).write_text("# 사용자 규칙\n\nAutopus 합성 구역\n")
		notify = self.user / ".codex/config.toml"
		notify.write_text('notify = ["acme-notify"]\n')
		return originals

	def test_preview_never_changes_or_prints_unrelated_settings(self):
		self.existing()
		before = self.snapshot()
		result = self.command("install")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(before, self.snapshot())
		for value in ("합성 비공개 설정", "ACME_TEST_SETTING", "acme-first", "xyz-second", "acme-notify", "Autopus 합성 구역"):
			self.assertNotIn(value, result.stdout + result.stderr)
		self.assertIn('"UserPromptSubmit": 1', result.stdout)
		self.assertEqual(self.calls(), [])

	def test_install_preserves_hook_order_other_events_notify_and_rules(self):
		originals = self.existing()
		self.apply()
		for tool, paths in records.PATHS.items():
			data = json.loads((self.user / paths[0]).read_text())
			for key in ("env", "notify"):
				self.assertEqual(data[key], originals[tool][key])
			for event, groups in originals[tool]["hooks"].items():
				self.assertEqual(data["hooks"][event][:len(groups)], groups)
				if event in EVENTS[tool]:
					self.assertEqual(len(data["hooks"][event]), len(groups) + 1)
				else:
					self.assertEqual(data["hooks"][event], groups)
			text = (self.user / paths[1]).read_text()
			self.assertIn(records.RULE, text)
			if tool != "claude":
				self.assertTrue(text.startswith("# 사용자 규칙\n\nAutopus 합성 구역\n"))
				self.assertEqual(text.count(records.START), 1)
		self.assertEqual((self.user / ".codex/config.toml").read_text(), 'notify = ["acme-notify"]\n')
		before = self.snapshot()
		self.apply()
		self.assertEqual(before, self.snapshot())
		result = self.command("check")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		for paths in records.PATHS.values():
			for path in paths:
				self.assertIn(f"| 작업 기록: {path} | 통과 |", result.stdout)

	def test_command_runs_from_another_working_directory_with_custom_paths(self):
		ctx = config.context("com.acme")
		ctx = dataclasses.replace(ctx, hub_home=self.user / 'hub xyz "$(acme)`xyz`')
		command = records.hook_command(ctx, "claude")
		result = subprocess.run(["/bin/sh", "-c", command], cwd=self.user, text=True,
			input=json.dumps({"session_id": "s01", "cwd": "/w/xyz", "hook_event_name": "UserPromptSubmit"}),
			capture_output=True, timeout=2)
		self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
		self.assertTrue((ctx.hub_home / "run/presence/claude.json").exists())

	def test_invalid_config_is_not_overwritten_and_check_continues(self):
		self.apply()
		for tool, paths in records.PATHS.items():
			path = self.user / paths[0]
			before = path.read_bytes()
			for broken in ('{"hooks": []}', '{"hooks": {"' + next(iter(EVENTS[tool])) + '": [42]}}', "{acme"):
				with self.subTest(tool=tool, broken=broken):
					path.write_text(broken)
					snapshot = self.snapshot()
					result = self.command("install", "--apply")
					self.assertEqual(result.returncode, 1)
					self.assertEqual(snapshot, self.snapshot())
					result = self.command("check")
					self.assertEqual(result.returncode, 1)
					self.assertIn(f"| 작업 기록: {paths[0]} | 실패 |", result.stdout)
					self.assertIn("| 마지막 백업:", result.stdout)
			path.write_bytes(before)

	def test_missing_misconfigured_and_symlinked_installations_fail_check(self):
		self.apply()
		path = self.user / records.PATHS["codex"][0]
		before = path.read_text()
		data = json.loads(before)
		data["hooks"]["Stop"][-1]["hooks"][0]["command"] += " xyz"
		path.write_text(json.dumps(data))
		self.assertIn("| 작업 기록: .codex/hooks.json | 실패 |", self.command("check").stdout)
		path.write_text(before)
		path = self.user / records.PATHS["gemini"][1]
		before = path.read_text()
		path.unlink()
		self.assertIn("| 작업 기록: .gemini/GEMINI.md | 실패 |", self.command("check").stdout)
		other = self.root / "xyz-rule"
		other.write_text(before)
		path.symlink_to(other)
		snapshot = self.snapshot()
		result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertEqual(snapshot, self.snapshot())
		self.assertEqual(other.read_text(), before)

	def test_partial_markers_keep_all_existing_rules(self):
		self.apply()
		path = self.user / records.PATHS["codex"][1]
		path.write_text("사용자 규칙\n" + records.START + "\n합성 손상 자료")
		before = self.snapshot()
		result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertEqual(before, self.snapshot())
		self.assertNotIn("합성 손상 자료", result.stdout + result.stderr)
