"""Claude 훅은 합성 홈의 기존 위치를 보존하고 모호한 등록을 거부한다."""

import copy
import json
from pathlib import Path
from unittest import mock

from collector.hooks import EVENTS
from hubsetup import config, records, templates
from test_hubsetup import HubSetupCase


class HubSetupRecordsTest(HubSetupCase):
	def setUp(self):
		super().setUp()
		self.ctx = config.context("com.acme")

	def settings(self, events=None):
		command = records.hook_command(self.ctx, "claude")
		return {"hooks": {event: [{"hooks": [
			{"type": "command", "command": command, "timeout": 5}]}]
			for event in (EVENTS["claude"] if events is None else events)},
			"env": {"ACME_PRIVATE": "합성 비공개 설정"}}

	def write_settings(self, name, data):
		path = self.user / ".claude" / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
		path.chmod(0o640)
		return path

	def test_global_only_hooks_are_not_registered_again_in_local_settings(self):
		data = self.settings()
		data["hooks"]["UserPromptSubmit"][0]["hooks"][:0] = [
			{"type": "command", "command": "acme-first"},
			{"type": "command", "command": "xyz-second"}]
		global_path = self.write_settings("settings.json", data)
		local_path = self.write_settings("settings.local.json", {"env": {"ACME_LOCAL": "보존"}})
		before = self.snapshot()
		items = templates.render(self.ctx)
		item = next(item for item in items if item.path.parent == global_path.parent and item.path.suffix == ".json")
		for event in EVENTS["claude"]:
			count = sum(records.owned(hook) for group in data["hooks"][event] for hook in group["hooks"])
			if item.path == local_path:
				count += sum(records.owned(hook) for group in json.loads(item.text)["hooks"][event] for hook in group["hooks"])
			self.assertEqual(count, 1, "전역 훅이 있는데 렌더가 로컬에 중복 등록했습니다")
		self.assertEqual(item.path, global_path)
		self.assertEqual(item.text, global_path.read_text())
		self.assertEqual(before, self.snapshot())
		with mock.patch.object(records, "current", wraps=records.current) as current:
			records.render_record(self.ctx, "claude", "hooks")
		self.assertEqual([call.args[0].path for call in current.call_args_list], [global_path, local_path])

	def test_existing_location_and_unselected_file_are_preserved_on_repeated_install(self):
		for name, other in (("settings.json", "settings.local.json"), ("settings.local.json", "settings.json")):
			with self.subTest(name=name):
				data = self.settings()
				data["hooks"]["UserPromptSubmit"].insert(0, {"matcher": "acme", "hooks": [
					{"type": "command", "command": "acme-first", "timeout": 10},
					{"type": "prompt", "prompt": "합성 사용자 훅"}]})
				path = self.write_settings(name, data)
				other_path = self.write_settings(other, {"env": {"ACME_OTHER": "합성 비공개 설정"}})
				original = self.snapshot()
				for _ in range(2):
					item = records.render_record(self.ctx, "claude", "hooks")
					self.assertEqual(item.path, path)
					self.assertTrue(templates.matches(item))
					result = self.apply()
					for file in (path, other_path):
						key = str(file.relative_to(self.user))
						self.assertEqual(self.snapshot()[key], original[key])
					for value in ("acme-first", "합성 사용자 훅", "합성 비공개 설정", records.hook_command(self.ctx, "claude")):
						self.assertNotIn(value, result.stdout + result.stderr)
				before = self.snapshot()
				self.assertIn("변경 파일 0개", self.apply().stdout)
				self.assertEqual(before, self.snapshot())

	def test_no_managed_hooks_defaults_to_local_and_preserves_global_user_hooks(self):
		path = self.user / ".claude/settings.local.json"
		self.assertEqual(records.render_record(self.ctx, "claude", "hooks").path, path)
		data = {"hooks": {"UserPromptSubmit": [{"hooks": [
			{"type": "command", "command": "acme-first"}]}]}, "env": {"ACME_PRIVATE": "합성 비공개 설정"}}
		global_path = self.write_settings("settings.json", data)
		before = self.snapshot()[str(global_path.relative_to(self.user))]
		self.apply()
		self.assertEqual(self.snapshot()[str(global_path.relative_to(self.user))], before)
		self.assertTrue(records.valid_hooks(self.ctx, "claude", path.read_text()))
		self.assertEqual(path.stat().st_mode & 0o777, 0o600)
		snapshot = self.snapshot()
		self.apply()
		self.assertEqual(snapshot, self.snapshot())

	def test_partial_events_are_added_only_to_the_existing_location(self):
		for name, other in (("settings.json", "settings.local.json"), ("settings.local.json", "settings.json")):
			with self.subTest(name=name):
				data = self.settings(["Stop"])
				data["hooks"]["UserPromptSubmit"] = [{"matcher": "acme", "hooks": [
					{"type": "command", "command": "acme-first"}, {"type": "command", "command": "xyz-second"}]}]
				original = copy.deepcopy(data)
				path = self.write_settings(name, data)
				other_path = self.write_settings(other, {"env": {"ACME_OTHER": "보존"}})
				before = self.snapshot()[str(other_path.relative_to(self.user))]
				item = records.render_record(self.ctx, "claude", "hooks")
				self.assertEqual(item.path, path)
				self.assertEqual(item.mode, 0o640)
				self.assertEqual(json.loads(item.preview[0]), {"UserPromptSubmit": 0, "Stop": 1, "SessionEnd": 0})
				self.assertEqual(json.loads(item.preview[1]), dict.fromkeys(EVENTS["claude"], 1))
				preview = self.command("install")
				self.assertEqual(preview.returncode, 0, preview.stderr)
				self.assertIn(str(path), preview.stdout)
				for value in ("ACME_PRIVATE", "합성 비공개 설정", "acme-first", "xyz-second", records.hook_command(self.ctx, "claude")):
					self.assertNotIn(value, preview.stdout + preview.stderr)
				self.apply()
				merged = json.loads(path.read_text())
				self.assertEqual(merged["hooks"]["Stop"], original["hooks"]["Stop"])
				self.assertEqual(merged["hooks"]["UserPromptSubmit"][:-1], original["hooks"]["UserPromptSubmit"])
				self.assertEqual(merged["env"], original["env"])
				self.assertTrue(records.valid_hooks(self.ctx, "claude", path.read_text()))
				self.assertEqual(path.stat().st_mode & 0o777, 0o640)
				self.assertEqual(self.snapshot()[str(other_path.relative_to(self.user))], before)
				before = self.snapshot()
				self.apply()
				self.assertEqual(before, self.snapshot())

	def test_managed_hooks_in_both_files_including_split_events_are_rejected_before_writes(self):
		for first, second in ((None, None), (["UserPromptSubmit"], ["Stop", "SessionEnd"]), (["AcmeExtra"], ["Stop"])):
			with self.subTest(first=first, second=second):
				self.write_settings("settings.json", self.settings(first))
				self.write_settings("settings.local.json", self.settings(second))
				before = self.snapshot()
				for args in (("install",), ("install", "--apply")):
					result = self.command(*args)
					self.assertEqual(result.returncode, 1)
					self.assertIn("전역·로컬", result.stderr)
					self.assertEqual(before, self.snapshot())
					self.assertEqual(self.calls(), [])
					self.assertNotIn(records.hook_command(self.ctx, "claude"), result.stdout + result.stderr)
					self.assertNotIn("합성 비공개 설정", result.stdout + result.stderr)

	def test_duplicate_managed_hooks_in_one_required_event_are_rejected(self):
		for name in ("settings.json", "settings.local.json"):
			for extra_group in (False, True):
				with self.subTest(name=name, extra_group=extra_group):
					data = self.settings()
					duplicate = copy.deepcopy(data["hooks"]["Stop"][0])
					duplicate["hooks"][0]["command"] += " acme-private-value"
					if extra_group:
						data["hooks"]["Stop"].append(duplicate)
					else:
						data["hooks"]["Stop"][0]["hooks"].extend(duplicate["hooks"])
					path = self.write_settings(name, data)
					before = self.snapshot()
					with self.assertRaises(config.SetupError):
						records.merge_hooks(path.read_text(), "claude", records.hook_command(self.ctx, "claude"))
					result = self.command("install", "--apply")
					self.assertEqual(result.returncode, 1)
					self.assertIn("중복", result.stderr)
					self.assertNotIn("acme-private-value", result.stdout + result.stderr)
					self.assertEqual(before, self.snapshot())
					self.assertEqual(self.calls(), [])
					path.unlink()

	def test_invalid_json_or_structure_is_rejected_in_either_file_even_when_unselected(self):
		broken = ["", " ", "{acme-private-value", "[]", "null", '{"hooks": {}, "hooks": {}}',
			'{"env": {"ACME_PRIVATE": NaN}}', '{"env": {"ACME_PRIVATE": Infinity}}',
			'{"env": {"ACME_PRIVATE": -Infinity}}']
		broken.extend(json.dumps({"hooks": value}) for value in (None, [], "acme-private-value"))
		bad_groups = [None, {}, 42, [None], [[]], [{}], [{"hooks": None}], [{"hooks": {}}],
			[{"hooks": [None]}], [{"hooks": [[]]}], [{"hooks": [{}]}],
			[{"hooks": [{"type": []}]}], [{"hooks": [{"type": "command", "command": []}]}],
			[{"hooks": [{"type": "command"}]}], [{"hooks": [], "matcher": None}],
			[{"hooks": [{"type": "command", "command": "acme-private-value", "timeout": True}]}]]
		for event in ("Stop", "AcmeExtra"):
			broken.extend(json.dumps({"hooks": {event: groups}}) for groups in bad_groups)
		for selected in ("settings.json", "settings.local.json"):
			for invalid in ("settings.json", "settings.local.json"):
				for text in broken:
					with self.subTest(selected=selected, invalid=invalid, text=text):
						self.write_settings("settings.json", {})
						self.write_settings("settings.local.json", {})
						self.write_settings(selected, self.settings())
						path = self.user / ".claude" / invalid
						path.write_text(text, encoding="utf-8")
						before = self.snapshot()
						with self.assertRaises(config.SetupError) as caught:
							templates.render(self.ctx)
						self.assertNotIn("acme-private-value", str(caught.exception))
						self.assertEqual(before, self.snapshot())
						self.assertEqual(self.calls(), [])

	def test_unselected_file_errors_prevent_apply_before_any_files_are_written(self):
		for name, other in (("settings.json", "settings.local.json"), ("settings.local.json", "settings.json")):
			self.write_settings(name, self.settings())
			path = self.write_settings(other, {})
			for broken in (b"{acme-private-value", b"", b"\xff"):
				with self.subTest(name=name, broken=broken):
					path.write_bytes(broken)
					before = self.snapshot()
					result = self.command("install", "--apply")
					self.assertEqual(result.returncode, 1)
					self.assertNotIn("acme-private-value", result.stdout + result.stderr)
					self.assertEqual(before, self.snapshot())
					self.assertEqual(self.calls(), [])
			path.write_text("{}")

	def test_symlinked_file_and_parent_are_rejected_without_reading_the_target(self):
		outside = self.root / "acme-settings"
		outside.write_text(json.dumps(self.settings()))
		for name, other in (("settings.json", "settings.local.json"), ("settings.local.json", "settings.json")):
			for dangling in (False, True):
				with self.subTest(name=name, dangling=dangling):
					path = self.write_settings(name, {})
					self.write_settings(other, self.settings())
					path.unlink()
					path.symlink_to(outside.with_name("missing") if dangling else outside)
					before = self.snapshot()
					original = Path.read_bytes

					def guard(candidate):
						self.assertNotIn(candidate, (path, outside), "심볼릭 링크 대상을 읽었습니다")
						return original(candidate)

					with mock.patch.object(Path, "read_bytes", guard):
						with self.assertRaisesRegex(config.SetupError, "심볼릭 링크"):
							templates.render(self.ctx)
					result = self.command("install", "--apply")
					self.assertEqual(result.returncode, 1)
					self.assertEqual(before, self.snapshot())
					self.assertEqual(self.calls(), [])
					path.unlink()
		folder = self.user / ".claude"
		folder.rename(self.user / "claude-original")
		folder.symlink_to(self.user / "claude-original", target_is_directory=True)
		with self.assertRaisesRegex(config.SetupError, "심볼릭 링크"):
			templates.render(self.ctx)
		self.assertEqual(outside.read_text(), json.dumps(self.settings()))

	def test_valid_hooks_requires_exactly_one_expected_command_per_event_and_broad_matcher(self):
		for matcher in (None, "", "*", ".*"):
			data = self.settings()
			for groups in data["hooks"].values():
				if matcher is not None:
					groups[0]["matcher"] = matcher
			self.assertTrue(records.valid_hooks(self.ctx, "claude", json.dumps(data)))
		for field, value in (("command", "acme-other"), ("type", "prompt"), ("timeout", 10), ("timeout", None)):
			with self.subTest(field=field, value=value):
				data = self.settings()
				data["hooks"]["Stop"][0]["hooks"][0][field] = value
				self.assertFalse(records.valid_hooks(self.ctx, "claude", json.dumps(data)))
		for matcher in ("acme", None, [], 42):
			data = self.settings()
			data["hooks"]["Stop"][0]["matcher"] = matcher
			self.assertFalse(records.valid_hooks(self.ctx, "claude", json.dumps(data)))
		for missing in EVENTS["claude"]:
			data = self.settings()
			del data["hooks"][missing]
			self.assertFalse(records.valid_hooks(self.ctx, "claude", json.dumps(data)))
		data = self.settings()
		data["hooks"]["Stop"][0]["hooks"] *= 2
		self.assertFalse(records.valid_hooks(self.ctx, "claude", json.dumps(data)))
