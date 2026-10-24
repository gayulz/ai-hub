"""임시 홈과 합성 Git 저장소로 대상 목록의 경계를 검사한다."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.config import ConfigError
from orchestrator import checks, targets


def init_repo(path: Path) -> Path:
	path.mkdir()
	for args in (("init", "-q", "-b", "main"), ("config", "user.name", "p01"), ("config", "user.email", "p01")):
		subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)
	return path


def write_repos(root: Path, *rows: dict) -> None:
	lines = []
	for row in rows:
		lines += ["[[repo]]"] + [key + " = " + json.dumps(value, ensure_ascii=False) for key, value in row.items()]
	(root / "repos.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class TargetsTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		self.repo = init_repo(self.root / "repo")
		self.other = init_repo(self.root / "other")
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}), mock.patch.object(targets, "REPO", self.repo)):
			patch.start()
			self.addCleanup(patch.stop)
		self.row = {"id": "blog-x", "path": str(self.other), "test_command": "python3 -m unittest"}

	def test_missing_file_only_has_builtin_and_unknown_id_is_safe(self):
		self.assertEqual(targets.targets(), {"ai-hub": targets.Target("ai-hub", self.repo)})
		self.assertEqual(targets.target("ai-hub").test_command, checks.DEFAULT_TEST)
		for value in ("missing", "../xyz", [], None):
			with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^목록에 없는 대상 저장소입니다$"):
				targets.target(value)

	def test_valid_policies_and_optional_defaults(self):
		row = {**self.row, "commit_pattern": r"feat\(clip\): .+", "forbidden": ["재기동은 별도 승인"],
			"restart_paths": ["src/", "config.py"], "restart_notice": "사용자 승인 뒤 재기동", "claude_trailer": "합성 서명"}
		write_repos(self.hub, row)
		value = targets.target("blog-x")
		self.assertEqual(value.path, self.other)
		self.assertEqual(value.commit_pattern, row["commit_pattern"])
		self.assertEqual(value.forbidden, tuple(row["forbidden"]))
		self.assertEqual(value.restart_paths, tuple(row["restart_paths"]))
		self.assertEqual((value.restart_notice, value.claude_trailer), (row["restart_notice"], row["claude_trailer"]))
		self.assertEqual(targets.for_repo(str(self.other)), value)
		write_repos(self.hub, self.row)
		self.assertEqual(targets.target("blog-x"), targets.Target("blog-x", self.other, self.row["test_command"]))

	def test_invalid_fields_and_duplicate_id_never_echo_values(self):
		cases = [{"id": identity} for identity in ("ai-hub", "Blog-x", "a" * 32, "1repo", "", 1)]
		cases += [{"test_command": command} for command in ("", " ", "a" * 301, 'python3 "', [], 1)]
		cases += [{"commit_pattern": "["}, {"commit_pattern": "a{999999999999999999999999}"},
			{"commit_pattern": 1}, {"forbidden": "private-payload"},
			{"forbidden": [1]}, {"forbidden": ["a"] * 11}, {"forbidden": ["a" * 301]}, {"restart_paths": "src/"},
			{"restart_paths": [1]}, {"restart_paths": ["src/"] * 11}, {"restart_paths": ["src/"]},
			{"restart_notice": ""}, {"restart_notice": "a" * 201}, {"restart_notice": 1},
			{"claude_trailer": 1}, {"claude_trailer": "a" * 201}, {"private-payload": "private-payload"}]
		cases += [{"restart_paths": [value], "restart_notice": "합성 안내"}
			for value in ("/src", "../src", "src/../a", "C:/src", "src\\a", "", ".")]
		for change in cases:
			with self.subTest(change=change):
				write_repos(self.hub, {**self.row, **change})
				with self.assertRaises(ConfigError) as raised:
					targets.targets()
				self.assertRegex(str(raised.exception), r"repos\.toml:\d+:")
				self.assertNotIn("private-payload", str(raised.exception))
		write_repos(self.hub, self.row, self.row)
		with self.assertRaises(ConfigError):
			targets.targets()

	def test_bad_toml_unknown_root_and_table_shapes_have_safe_line_numbers(self):
		path = self.hub / "repos.toml"
		for source in ('[[repo]\nid="private-payload"', 'private-payload = 1\n', '[repo]\nid="blog-x"', 'repo=[1]',
				'[[repo]]\nforbidden=' + '[' * 1500):
			with self.subTest(source=source):
				path.write_text(source)
				with self.assertRaises(ConfigError) as raised:
					targets.targets()
				self.assertRegex(str(raised.exception), r"repos\.toml:\d+:")
				self.assertNotIn("private-payload", str(raised.exception))
		path.write_text('\n[[repo]]\nid="Blog-x"\n')
		with self.assertRaisesRegex(ConfigError, "repos.toml:3:"):
			targets.targets()

	def test_path_must_be_distinct_real_git_root_without_symlink_components(self):
		plain = self.root / "plain"
		plain.mkdir()
		child = self.other / "child"
		child.mkdir()
		link = self.root / "link"
		link.symlink_to(self.other, target_is_directory=True)
		for path in ("relative", self.root / "missing", plain, self.repo, child, link, link / "child"):
			with self.subTest(path=path):
				write_repos(self.hub, {**self.row, "path": str(path)})
				with self.assertRaises(ConfigError) as raised:
					targets.targets()
				self.assertNotIn(str(path), str(raised.exception))
