"""공급자 호출 인자를 임시 폴더로 확인한다(실제 AI 호출 없음)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from collector.config import Plan
from orchestrator import providers


class CodexArgumentsTest(unittest.TestCase):
	def argv(self, folder, writer=False):
		plan = Plan("p01", "codex", "합성", "personal", ("personal",), None, None)
		return providers._codex(plan, writer, Path(folder), Path("schema.json"), Path("last.json"))[0]

	def test_outside_git_skips_trust_check_but_keeps_sandbox(self):
		with tempfile.TemporaryDirectory() as folder:
			argv = self.argv(folder)
		self.assertIn("--skip-git-repo-check", argv)
		self.assertEqual(argv[argv.index("-s") + 1], "read-only")

	def test_inside_git_does_not_add_the_flag(self):
		with tempfile.TemporaryDirectory() as folder:
			subprocess.run(["git", "init", "-q", folder], check=True)
			self.assertNotIn("--skip-git-repo-check", self.argv(folder, writer=True))


if __name__ == "__main__":
	unittest.main()
