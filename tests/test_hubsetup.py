"""설치는 임시 HOME·볼트 Git·가짜 launchctl에서만 실행한다."""

import dataclasses
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hubbackup import targets
from hubsetup import config, templates

LAUNCHCTL = '''
import json
import os
import plistlib
import sys
from pathlib import Path
root = Path(os.environ["ACME_LAUNCHCTL_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as output:
	output.write(json.dumps(args) + "\\n")
record = root / "loaded.json"
loaded = json.loads(record.read_text()) if record.exists() else []
if os.environ.get("ACME_LAUNCHCTL_FAIL") == args[0]:
	sys.exit(5)
if args[0] == "print" and len(args) == 2:
	sys.exit(0 if args[1] in loaded else 113)
elif args[0] == "bootout" and len(args) == 2:
	if args[1] not in loaded:
		sys.exit(113)
	loaded.remove(args[1])
elif args[0] == "bootstrap" and len(args) == 3:
	with open(args[2], "rb") as incoming:
		label = plistlib.load(incoming)["Label"]
	target = args[1] + "/" + label
	if target in loaded:
		sys.exit(5)
	loaded.append(target)
else:
	sys.exit(2)
record.write_text(json.dumps(loaded))
'''


class HubSetupCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.user = self.root / "acme"
		self.hub = self.user / ".ai-hub"
		self.vault = self.user / "vault"
		self.bin = self.root / "bin"
		self.bin.mkdir()
		(self.hub / "keys").mkdir(parents=True)
		self.real_git = shutil.which("git")
		self.assertIsNotNone(self.real_git)
		(self.bin / "git").symlink_to(self.real_git)
		(self.bin / "python3").symlink_to(sys.executable)
		for name in ("git-crypt", "age", "age-keygen", "claude", "codex", "gemini", "agy"):
			self.executable(name, "#!/bin/sh\nexit 0\n")
		self.executable("launchctl", "#!" + sys.executable + "\n" + LAUNCHCTL)
		patch = mock.patch.dict(os.environ, {"HOME": str(self.user), "AI_HUB_HOME": str(self.hub),
			"PATH": str(self.bin), "ACME_LAUNCHCTL_ROOT": str(self.root),
			"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1"})
		patch.start()
		self.addCleanup(patch.stop)
		self.git("init", "-q", str(self.vault), at=self.root)
		for path in ("source/docs", "archive", "attachments"):
			(self.user / path).mkdir(parents=True)
		(self.hub / "sources.toml").write_text('''vault = "~/vault"
[[classify]]
path = "~/source"
class = "personal"
[[repo]]
path = "~/source"
class = "personal"
doc_dirs = ["docs"]
[ignore]
repos = ["~/archive"]
[vaultsync]
attachments_backup = "~/attachments"
[hubsetup]
author_name = "acme"
author_email = "xyz@example.invalid"
''', encoding="utf-8")
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "acme"
tool = "codex"
owner = "personal"
allow = ["personal"]
[routing]
personal = ["acme"]
''', encoding="utf-8")
		for name in ("vault-git-crypt.key", "backup.agekey"):
			path = self.hub / "keys" / name
			path.write_bytes(b"acme synthetic fixture, not a key")
			path.chmod(0o600)
		key = self.vault / ".git/git-crypt/keys/default"
		key.parent.mkdir(parents=True)
		key.write_bytes(b"acme synthetic fixture, not a key")
		for operation in ("clean", "smudge"):
			self.git("config", "--local", "filter.git-crypt." + operation, "git-crypt " + operation)
		self.git("config", "--local", "filter.git-crypt.required", "true")
		targets.backup_dir().mkdir(parents=True)
		self.backup_stamp(datetime.now(timezone.utc))

	def executable(self, name, text):
		path = self.bin / name
		path.write_text(text, encoding="utf-8")
		path.chmod(0o755)
		return path

	def git(self, *args, at=None):
		result = subprocess.run([self.real_git, "-C", str(at or self.vault), *args], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		return result.stdout.strip()

	def command(self, *args):
		return subprocess.run([sys.executable, "-m", "hubsetup", *args, "--label-prefix", "com.acme"],
			capture_output=True, text=True)

	def apply(self, *args):
		result = self.command("install", "--apply", *args)
		self.assertEqual(result.returncode, 0, result.stderr)
		return result

	def calls(self):
		path = self.root / "calls.jsonl"
		return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

	def snapshot(self):
		return {str(p.relative_to(self.user)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
			for p in self.user.rglob("*") if p.is_file()}

	def backup_stamp(self, stamp, failed=0):
		(self.hub / "backup-state.json").write_text(json.dumps({"files": {}, "sources": {},
			"last_backup": stamp.isoformat(), "last_failed": failed}), encoding="utf-8")


class HubSetupInstallTest(HubSetupCase):
	def test_render_has_no_remaining_placeholders_and_schedules_are_preserved(self):
		ctx = config.context("com.acme")
		items = templates.render(ctx)
		self.assertEqual(len(items), 8)
		for item in items:
			self.assertIsNone(re.search(r"\$(?:user_home|hub_home|repo|python|path|label_prefix|handoff|hub_export|hub_environment)\b", item.text))
		jobs = {item.label: plistlib.loads(item.text.encode()) for item in items if item.label}
		self.assertEqual(jobs["com.acme.ai-hub-collector"]["StartCalendarInterval"], {"Minute": 10})
		self.assertTrue(jobs["com.acme.ai-hub-collector"]["RunAtLoad"])
		for module, minute in (("vaultsync", 30), ("backup", 45)):
			data = jobs[f"com.acme.ai-hub-{module}"]
			self.assertEqual(data["StartCalendarInterval"], [{"Hour": 12, "Minute": minute}, {"Hour": 23, "Minute": minute}])
			self.assertFalse(data["RunAtLoad"])
		backup = jobs["com.acme.ai-hub-backup"]
		self.assertEqual(backup["ProgramArguments"], [sys.executable, "-m", "hubbackup", "run"])
		self.assertEqual(backup["WorkingDirectory"], str(ctx.repo))
		for name in ("StandardOutPath", "StandardErrorPath"):
			self.assertEqual(backup[name], str(self.hub / "logs/hubbackup.log"))
		self.assertEqual(backup["EnvironmentVariables"]["HOME"], str(self.user))
		self.assertNotIn("AI_HUB_HOME", backup["EnvironmentVariables"])
		office = jobs["com.acme.ai-hub-office"]
		self.assertEqual(office["ProgramArguments"], [sys.executable, "-m", "office"])
		self.assertTrue(office["RunAtLoad"])
		self.assertTrue(office["KeepAlive"])
		self.assertNotIn("StartCalendarInterval", office)
		for name in ("StandardOutPath", "StandardErrorPath"):
			self.assertEqual(office[name], str(self.hub / "logs/office.log"))
		self.assertEqual(backup["EnvironmentVariables"]["PATH"],
			f"{self.user}/.nvm/versions/node/v22.22.1/bin:{self.user}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin")

	def test_default_install_changes_nothing_including_git_config(self):
		before = self.snapshot()
		result = self.command("install")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("--- 현재", result.stdout)
		self.assertIn("+++ 템플릿", result.stdout)
		self.assertIn("변경 파일 8개", result.stdout)
		self.assertEqual(before, self.snapshot())
		self.assertEqual(self.calls(), [])
		self.assertNotIn("xyz@example.invalid", result.stdout)

	def test_apply_is_idempotent_and_uses_expected_launchctl_arguments(self):
		first = self.apply()
		self.assertIn("변경 파일 8개", first.stdout)
		self.assertEqual(self.git("config", "--local", "user.name"), "acme")
		self.assertEqual(self.git("config", "--local", "user.email"), "xyz@example.invalid")
		domain = f"gui/{os.getuid()}"
		for module in templates.MODULES:
			label = f"com.acme.ai-hub-{module}"
			self.assertIn(["print", domain + "/" + label], self.calls())
			self.assertIn(["bootstrap", domain, str(self.user / "Library/LaunchAgents" / (label + ".plist"))], self.calls())
		for item in templates.render(config.context("com.acme")):
			self.assertTrue(templates.matches(item))
		before, calls = self.snapshot(), len(self.calls())
		second = self.apply()
		self.assertIn("변경 파일 0개 · 작성자 설정 0개 · 예약 등록 0개", second.stdout)
		self.assertEqual(before, self.snapshot())
		self.assertTrue(all(args[0] == "print" for args in self.calls()[calls:]))

	def test_changed_loaded_plist_is_booted_out_then_bootstrapped(self):
		self.apply()
		path = self.user / "Library/LaunchAgents/com.acme.ai-hub-vaultsync.plist"
		path.write_text(path.read_text().replace("<integer>30</integer>", "<integer>31</integer>"))
		before = len(self.calls())
		result = self.apply()
		self.assertIn("변경 파일 1개", result.stdout)
		domain = f"gui/{os.getuid()}"
		calls = self.calls()[before:]
		bootout = ["bootout", domain + "/com.acme.ai-hub-vaultsync"]
		bootstrap = ["bootstrap", domain, str(path)]
		self.assertIn(bootout, calls)
		self.assertIn(bootstrap, calls)
		self.assertLess(calls.index(bootout), calls.index(bootstrap))

	def test_failed_bootout_keeps_the_old_plist_so_a_retry_reloads_it(self):
		self.apply()
		path = self.user / "Library/LaunchAgents/com.acme.ai-hub-vaultsync.plist"
		old = path.read_text().replace("<integer>30</integer>", "<integer>31</integer>")
		path.write_text(old)
		with mock.patch.dict(os.environ, {"ACME_LAUNCHCTL_FAIL": "bootout"}):
			result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertIn("예약 작업 해제에 실패", result.stderr)
		self.assertEqual(path.read_text(), old)
		result = self.apply()
		self.assertIn("변경 파일 1개", result.stdout)
		self.assertIn("예약 등록 1개", result.stdout)

	def test_extra_hooks_and_tools_are_never_changed_and_hook_is_warned(self):
		hook = self.vault / ".git/hooks/post-commit"
		hook.write_text("#!/bin/sh\n# acme 기존 훅\n")
		hook.chmod(0o755)
		extra = self.hub / "tools/d10_acme"
		extra.parent.mkdir()
		extra.write_bytes(b"acme")
		before = {p: (p.read_bytes(), p.stat().st_mode) for p in (hook, extra)}
		self.apply()
		self.apply()
		for path, value in before.items():
			self.assertEqual((path.read_bytes(), path.stat().st_mode), value)
		result = self.command("check")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("| 템플릿에 없는 훅 | 경고 | post-commit |", result.stdout)
		self.assertNotIn(".sample", result.stdout)

	def test_missing_author_is_skipped_and_arguments_override_settings(self):
		path = self.hub / "sources.toml"
		path.write_text(path.read_text().split("[hubsetup]")[0])
		result = self.apply()
		self.assertIn("작성자 설정 건너뜀", result.stdout)
		missing = subprocess.run([self.real_git, "-C", str(self.vault), "config", "--local", "--get", "user.name"], capture_output=True)
		self.assertEqual(missing.returncode, 1)
		self.apply("--author-name", "xyz", "--author-email", "acme@example.invalid")
		self.assertEqual(self.git("config", "--local", "user.name"), "xyz")
		self.assertEqual(self.git("config", "--local", "user.email"), "acme@example.invalid")

	def test_wrong_execution_permission_is_previewed_and_repaired(self):
		self.apply()
		path = self.vault / ".git/hooks/pre-push"
		path.chmod(0o644)
		result = self.command("install")
		self.assertIn("권한: 0o644 → 0o755", result.stdout)
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
		self.apply()
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

	def test_missing_launchctl_prevents_any_apply_writes(self):
		(self.bin / "launchctl").unlink()
		before = self.snapshot()
		result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertIn("launchctl", result.stderr)
		self.assertEqual(before, self.snapshot())

	def test_failed_bootstrap_is_reported_and_next_apply_recovers_remaining_jobs(self):
		with mock.patch.dict(os.environ, {"ACME_LAUNCHCTL_FAIL": "bootstrap"}):
			result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertIn("예약 작업 등록에 실패", result.stderr)
		result = self.apply()
		self.assertIn("변경 파일 3개 · 작성자 설정 0개 · 예약 등록 4개", result.stdout)

	def test_template_paths_escape_shell_and_xml_and_propagate_custom_hub_home(self):
		python = self.user / 'python xyz & "$(xyz)`acme`'
		python.symlink_to(sys.executable)
		ctx = dataclasses.replace(config.context("com.acme"), hub_home=self.user / 'xyz & "$(acme)`xyz`',
			repo=self.user / 'repo & "$(xyz)`acme`', python=python)
		items = templates.render(ctx)
		for item in items:
			if item.label:
				data = plistlib.loads(item.text.encode())
				self.assertEqual(data["WorkingDirectory"], str(ctx.repo))
				self.assertEqual(data["EnvironmentVariables"]["AI_HUB_HOME"], str(ctx.hub_home))
		wrapper = next(item for item in items if item.path.name == "vf")
		self.assertIn('export AI_HUB_HOME="$HOME/', wrapper.text)
		ctx.repo.mkdir()
		(ctx.repo / "vaultfilter.py").write_text('import os\nprint(os.environ["AI_HUB_HOME"])\nprint("acme 인자")\n')
		wrapper.path.parent.mkdir(parents=True)
		wrapper.path.write_text(wrapper.text)
		wrapper.path.chmod(0o755)
		result = subprocess.run([str(wrapper.path), "xyz"], cwd=self.user, capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.splitlines()[0], str(ctx.hub_home))
		self.assertNotIn("command not found", result.stderr)

	def test_symlink_install_target_is_rejected_without_overwriting_target(self):
		outside = self.root / "xyz"
		outside.write_bytes("acme 보존".encode())
		(self.vault / ".git/hooks/commit-msg").symlink_to(outside)
		before = self.snapshot()
		result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertIn("심볼릭 링크", result.stderr)
		self.assertEqual(before, self.snapshot())
		self.assertEqual(outside.read_bytes(), "acme 보존".encode())

	def test_vault_subdirectory_is_not_mistaken_for_the_git_root(self):
		(self.vault / "xyz").mkdir()
		path = self.hub / "sources.toml"
		path.write_text(path.read_text().replace('vault = "~/vault"', 'vault = "~/vault/xyz"'))
		result = self.command("install", "--apply")
		self.assertEqual(result.returncode, 1)
		self.assertIn("최상위 폴더", result.stderr)
		self.assertEqual(self.calls(), [])


if __name__ == "__main__":
	unittest.main()
