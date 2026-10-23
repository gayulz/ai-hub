"""실패·경고 구분과 읽기 전용 보안 경계를 합성 자료로 확인한다."""

import contextlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hubsetup import __main__ as cli
from hubsetup import check
from test_hubsetup import HubSetupCase


class HubSetupCheckTest(HubSetupCase):
	def test_installed_environment_passes_and_check_does_not_change_home(self):
		self.apply()
		before = self.snapshot()
		calls = len(self.calls())
		result = self.command("check")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("| 점검 항목 | 결과 | 안내 |", result.stdout)
		self.assertNotIn("| 실패 |", result.stdout)
		self.assertNotIn("xyz@example.invalid", result.stdout)
		self.assertEqual(before, self.snapshot())
		self.assertTrue(all(args[0] == "print" for args in self.calls()[calls:]))

	def test_missing_ai_clis_warn_without_failing(self):
		self.apply()
		for name in ("claude", "codex", "gemini", "agy"):
			(self.bin / name).unlink()
		result = self.command("check")
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		for name in ("claude", "codex", "gemini", "agy"):
			self.assertIn(f"| 프로그램: {name} | 경고 | 명령 없음 |", result.stdout)

	def test_missing_required_program_fails(self):
		self.apply()
		(self.bin / "age-keygen").unlink()
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("| 프로그램: age-keygen | 실패 |", result.stdout)

	def test_old_python_fails(self):
		self.apply()
		version = mock.Mock(major=3, minor=13)
		version.__ge__ = mock.Mock(return_value=False)
		output = io.StringIO()
		with mock.patch.object(check.sys, "version_info", version), contextlib.redirect_stdout(output):
			result = cli.main(["check", "--label-prefix", "com.acme"])
		self.assertEqual(result, 1)
		self.assertIn("| Python 3.14 이상 | 실패 | 3.13 |", output.getvalue())

	def test_other_user_home_is_detected_in_sources_and_plans(self):
		self.apply()
		self.assertTrue(check.other_home("/Users/xyz/source"))
		self.assertFalse(check.other_home("/Users/acme/source"))
		for name in ("sources.toml", "plans.toml"):
			with self.subTest(name=name):
				path = self.hub / name
				before = path.read_text()
				path.write_text(before + '\n[xyz]\npath = "/Users/xyz/acme"\n')
				result = self.command("check")
				self.assertEqual(result.returncode, 1)
				self.assertIn("다른 사용자 홈 경로", result.stdout)
				self.assertIn(f"| 설정 경로: {name} | 실패 |", result.stdout)
				self.assertNotIn("/Users/xyz", result.stdout)
				path.write_text(before)

	def test_missing_repo_path_fails_but_classify_and_doc_dirs_do_not(self):
		self.assertEqual(check.path_problems({"classify": [{"path": "~/xyz-gone"}],
			"repo": [{"path": "~/source", "doc_dirs": ["xyz"]}]}), [])
		self.apply()
		path = self.hub / "sources.toml"
		path.write_text(path.read_text().replace('[[repo]]\npath = "~/source"', '[[repo]]\npath = "~/xyz"'))
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("repo[0].path: 경로 없음", result.stdout)

	def test_glob_paths_are_checked_without_treating_wildcards_as_literal_names(self):
		self.assertEqual(check.path_problems({"glob": "~/source/*"}), [])
		problems = check.path_problems({"glob": "~/xyz/*"})
		self.assertEqual(problems, ["glob: 경로 없음"])

	def test_key_with_permission_644_fails(self):
		self.apply()
		for name in ("vault-git-crypt.key", "backup.agekey"):
			with self.subTest(name=name):
				path = self.hub / "keys" / name
				path.chmod(0o644)
				result = self.command("check")
				self.assertEqual(result.returncode, 1)
				self.assertIn(f"| 키: {name} | 실패 | 권한 0o644", result.stdout)
				path.chmod(0o600)

	def test_missing_key_and_symlink_key_fail(self):
		self.apply()
		(self.hub / "keys/backup.agekey").unlink()
		path = self.hub / "keys/vault-git-crypt.key"
		path.rename(path.with_name("xyz"))
		path.symlink_to(path.with_name("xyz"))
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		for name in ("vault-git-crypt.key", "backup.agekey"):
			self.assertIn(f"| 키: {name} | 실패 |", result.stdout)

	def test_key_contents_are_never_opened_even_for_unlock_check(self):
		self.apply()
		original = Path.open

		def guard(path, *args, **kwargs):
			if path.is_relative_to(self.hub / "keys") or path.is_relative_to(self.vault / ".git/git-crypt/keys"):
				self.fail("점검 중 키 내용을 읽었습니다")
			return original(path, *args, **kwargs)

		output = io.StringIO()
		with mock.patch.object(Path, "open", guard), contextlib.redirect_stdout(output):
			result = cli.main(["check", "--label-prefix", "com.acme"])
		self.assertEqual(result, 0, output.getvalue())
		self.assertNotIn("synthetic fixture", output.getvalue())

	def test_locked_vault_fails_even_if_internal_key_file_is_present(self):
		self.apply()
		self.git("config", "--local", "filter.git-crypt.required", "false")
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("| 볼트 git-crypt 잠금 해제 | 실패 |", result.stdout)

	def test_missing_author_and_changed_installed_file_fail(self):
		self.apply()
		self.git("config", "--local", "--unset", "user.email")
		(self.hub / "tools/vf").write_text("#!/bin/sh\n# xyz\n")
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("| 볼트 작성자: user.email | 실패 |", result.stdout)
		self.assertIn("| 설치물: vf | 실패 |", result.stdout)

	def test_unregistered_job_fails_and_apply_repairs_it_without_rewriting_files(self):
		self.apply()
		(self.root / "loaded.json").write_text("[]")
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		for module in ("collector", "vaultsync", "backup", "office"):
			self.assertIn(f"| 예약 등록: {module} | 실패 |", result.stdout)
		before = self.snapshot()
		result = self.apply()
		self.assertIn("변경 파일 0개", result.stdout)
		self.assertEqual(before, self.snapshot())
		self.assertEqual(self.command("check").returncode, 0)

	def test_backup_older_than_36_hours_future_time_and_failure_count_fail(self):
		self.apply()
		for stamp, failed in ((datetime.now(timezone.utc) - timedelta(hours=37), 0),
				(datetime.now(timezone.utc) + timedelta(hours=1), 0), (datetime.now(timezone.utc), 1)):
			with self.subTest(stamp=stamp, failed=failed):
				self.backup_stamp(stamp, failed)
				result = self.command("check")
				self.assertEqual(result.returncode, 1)
				self.assertIn("| 마지막 백업: 36시간 이내 | 실패 |", result.stdout)
		self.backup_stamp(datetime.now(timezone.utc) - timedelta(hours=35))
		self.assertEqual(self.command("check").returncode, 0)

	def test_inaccessible_cloud_folder_fails(self):
		self.apply()
		cloud = self.user / "Library/Mobile Documents"
		cloud.rename(cloud.with_name("xyz"))
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("| 백업 iCloud 사본 폴더 | 실패 |", result.stdout)

	def test_broken_config_still_outputs_remaining_checks(self):
		(self.hub / "sources.toml").write_text("[[xyz")
		(self.hub / "plans.toml").write_text("[[xyz")
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		for text in ("| 설정: sources.toml | 실패 |", "| 설정: plans.toml | 실패 |", "| 키: backup.agekey | 통과 |", "| 백업 iCloud 사본 폴더 | 통과 |"):
			self.assertIn(text, result.stdout)
		self.assertNotIn("Traceback", result.stderr)

	def test_symlink_installation_is_a_failure_in_the_table(self):
		self.apply()
		path = self.hub / "tools/vf"
		path.rename(path.with_name("xyz"))
		path.symlink_to(path.with_name("xyz"))
		result = self.command("check")
		self.assertEqual(result.returncode, 1)
		self.assertIn("| 설치물: vf | 실패 |", result.stdout)
