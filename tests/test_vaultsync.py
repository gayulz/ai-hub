"""볼트 자동 기록 테스트. 임시 Git 저장소와 합성 이름만 쓴다."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import redact_pii
from vaultsync import __main__ as cli
from vaultsync import mirror, sync

TOKEN = "ghp_" + "A1b2C3d4E5" * 4


def sh(cwd, *args):
	subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class VaultSyncTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		root = Path(self.tmp.name)
		self.home, self.vault, self.remote, self.backup = root / "hub", root / "vault", root / "remote.git", root / "icloud"
		(self.home / "redaction").mkdir(parents=True)
		(self.home / "redaction" / "dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "⟦회사명-1⟧"}]}), encoding="utf-8")
		(self.home / "sources.toml").write_text(
			f'vault = "{self.vault}"\n\n[vaultsync]\nattachments_backup = "{self.backup}"\n', encoding="utf-8")
		self.env = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.home)})
		self.env.start()
		redact_pii.host_pattern.cache_clear()
		sh(root, "git", "init", "-q", "--bare", str(self.remote))
		sh(root, "git", "init", "-q", "-b", "main", str(self.vault))
		for key, value in (("user.name", "test"), ("user.email", "test@localhost")):
			sh(self.vault, "git", "config", key, value)
		self.write(".gitignore", "*.png\n**/🔐*\n")
		self.write("notes/old.md", "# 지울 노트\n")
		sh(self.vault, "git", "add", ".gitignore", "notes/old.md")
		sh(self.vault, "git", "commit", "-q", "-m", "시작")
		sh(self.vault, "git", "remote", "add", "origin", str(self.remote))
		sh(self.vault, "git", "push", "-q", "-u", "origin", "main")
		self.notify = mock.patch.object(cli, "notify")
		self.notified = self.notify.start()

	def tearDown(self):
		self.notify.stop()
		self.env.stop()
		redact_pii.host_pattern.cache_clear()
		self.tmp.cleanup()

	def write(self, relative, text):
		path = self.vault / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
		return path

	def status(self):
		return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.vault,
			capture_output=True, text=True).stdout

	def test_problem_files_are_excluded_with_reasons(self):
		self.write("notes/ok.md", "acme 회의, 연락 dev@acme.test")
		self.write("notes/acme-plan.md", "경로에 이름")
		self.write("notes/token.md", f"git 토큰\n{TOKEN}\n")  # 본문은 git-crypt가 암호화하므로 막지 않는다
		self.write("notes/data.csv", "a,b")
		result = sync.plan(self.vault)
		self.assertEqual(sorted(result.committed), ["notes/ok.md", "notes/token.md"])
		reasons = dict(result.excluded)
		self.assertIn("경로", reasons["notes/acme-plan.md"])
		self.assertEqual(reasons["notes/data.csv"], "허용하지 않는 파일 종류")

	def test_run_commits_pushes_deletions_and_leaves_excluded_files(self):
		self.write("notes/new.md", "# 새 노트\n")
		self.write("notes/acme-plan.md", "경로에 이름")
		(self.vault / "notes/old.md").unlink()
		self.assertEqual(cli.main(["run"]), 0)
		pushed = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main"],
			capture_output=True, text=True).stdout.split()
		self.assertIn("notes/new.md", pushed)
		self.assertNotIn("notes/old.md", pushed)
		self.assertNotIn("notes/acme-plan.md", pushed)
		self.assertEqual(self.status().strip(), "?? notes/acme-plan.md")  # 뺀 파일은 작업 폴더에 그대로
		self.notified.assert_called_once()
		log = (self.home / "logs" / "vaultsync.log").read_text(encoding="utf-8")
		self.assertIn("push 성공", log)

	def test_nothing_changed_makes_no_commit(self):
		before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.vault, capture_output=True, text=True).stdout
		self.assertEqual(cli.main(["run"]), 0)
		after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.vault, capture_output=True, text=True).stdout
		self.assertEqual(before, after)

	def test_human_git_work_is_left_alone(self):
		self.write("notes/mine.md", "직접 스테이징")
		sh(self.vault, "git", "add", "notes/mine.md")
		self.assertIn("스테이징", sync.busy(self.vault))
		sh(self.vault, "git", "reset", "-q")
		(self.vault / ".git" / "index.lock").write_text("")
		self.assertIn("index.lock", sync.busy(self.vault))
		self.assertEqual(sync.plan(self.vault).committed, [])

	def test_dry_run_changes_nothing(self):
		self.write("notes/new.md", "# 새 노트\n")
		self.write("📎 Attachments/shot.png", "png")
		self.assertEqual(cli.main(["run", "--dry-run"]), 0)
		self.assertEqual(self.status().strip(), "?? notes/new.md")
		self.assertFalse(self.backup.exists())
		self.assertFalse((self.home / "logs" / "vaultsync.log").exists())

	def test_mirror_copies_ignored_attachments_only_once(self):
		self.write("📎 Attachments/shot.png", "png")
		self.write("🌱 Personal/🔐 ID.md", "비밀")
		self.write("🔐 folder/inner.png", "비밀 폴더")
		self.assertEqual(mirror.mirror(self.vault, self.backup), 1)
		self.assertTrue((self.backup / "📎 Attachments/shot.png").is_file())
		self.assertFalse(any("🔐" in str(p) for p in self.backup.rglob("*")))
		self.assertEqual(mirror.mirror(self.vault, self.backup), 0)  # 바뀌지 않았으면 다시 복사하지 않는다


if __name__ == "__main__":
	unittest.main()
