"""합성 Git 볼트에서 폴더 승인·재실행·실패 복구를 검사한다."""

import json
import subprocess
import unicodedata
from pathlib import Path
from unittest import mock

from collector import vault as vault_io
from collector.backfill_common import locked, sha
from vaultreorg import frontmatter as fm
from vaultsync import sync

from test_vaultreorg_support import ReorgCase


class ApplyTest(ReorgCase):
	def prepare(self):
		self.career = self.write("💼 Career/261024-p01.md", "본문 p01\r\n혼합\n끝")
		self.study = self.write("📖 Study/261024-p02.md", "본문 p02\n")
		self.init_git("💼 Career", "📖 Study")
		self.head = self.git("rev-parse", "HEAD")
		self.assertEqual(self.command("report"), 0, self.stderr.getvalue())

	def test_apply_one_folder_one_commit_and_idempotent_without_push(self):
		self.prepare()
		career_before, study_before = self.career.read_bytes(), self.study.read_bytes()
		unrelated = self.write("📖 Study/261024-other.md", "건드리면 안 되는 합성 노트")
		original_git = sync.git
		calls = []
		def recording(vault, *args):
			calls.append(args)
			self.assertNotIn("push", args)
			return original_git(vault, *args)
		with mock.patch.object(sync, "git", side_effect=recording):
			self.assertEqual(self.command("apply", "--folder", "Career"), 0, self.stderr.getvalue())
		self.assertEqual(self.study.read_bytes(), study_before)
		self.assertEqual(fm.split(self.career.read_bytes()).body, career_before)
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)
		message = self.git("log", "-1", "--format=%B").decode().strip()
		self.assertEqual(message, "볼트 재정리 R1: 머리말 정리 · 파일 1개")
		self.assertNotIn("Career", message)
		self.assertNotIn("Study", message)
		self.assertNotIn("Co-Authored-By", message)
		changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD").decode().split("\0")
		self.assertEqual(changed, ["💼 Career/261024-p01.md", ""])
		self.assertIn(unrelated.name.encode(), self.git("ls-files", "--others"))
		add = next(args for args in calls if "add" in args)
		self.assertIn("--pathspec-file-nul", add)
		self.assertTrue(any(arg.startswith("--pathspec-from-file=") for arg in add))
		self.assertIn("--literal-pathspecs", add)
		self.assertEqual(self.notes()["💼 Career/261024-p01.md"]["status"], "applied")
		before = self.snapshot()
		with mock.patch.object(vault_io, "atomic_write", wraps=vault_io.atomic_write) as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 0)
			writing.assert_not_called()
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)

	def test_current_hash_mismatch_skips_and_other_notes_still_commit(self):
		self.prepare()
		self.career.write_text("사용자가 바꾼 본문", encoding="utf-8")
		before = self.career.read_bytes()
		self.assertEqual(self.command("apply", "--folder", "Career"), 0)
		self.assertEqual(self.career.read_bytes(), before)
		note = self.notes()["💼 Career/261024-p01.md"]
		self.assertEqual(note["status"], "skipped")
		self.assertEqual(note["reason"], "보고서 이후 바뀜")
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
		self.assertEqual(self.command("apply", "--folder", "Study"), 0)
		self.assertEqual(self.notes()["📖 Study/261024-p02.md"]["status"], "applied")

	def test_real_busy_states_refuse_before_any_note_or_plan_write(self):
		self.prepare()
		before = self.snapshot()
		plan = (self.base / "r1-plan.json").read_bytes()
		index_lock = self.vault / ".git/index.lock"
		index_lock.touch()
		self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertIn("index.lock", self.stdout.getvalue())
		index_lock.unlink()
		self.assertEqual(self.snapshot(), before)
		self.assertEqual((self.base / "r1-plan.json").read_bytes(), plan)
		other = self.write("📖 Study/other.md", "개인 작업")
		self.git("add", "--", str(other.relative_to(self.vault)))
		with mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
			writing.assert_not_called()
		self.assertIn("스테이징", self.stdout.getvalue())
		self.assertEqual((self.base / "r1-plan.json").read_bytes(), plan)
		self.git("reset", "-q")
		self.git("checkout", "-q", "-b", "xyz")
		self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertIn("main", self.stdout.getvalue())

	def test_vaultsync_and_per_note_locks_block_writes(self):
		self.prepare()
		with locked("vaultsync", exact=self.hub / "run/vaultsync.lock"), mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 2)
			writing.assert_not_called()
		self.assertIn("실행 중", self.stderr.getvalue())
		with locked("vault:" + str(self.career.absolute())), mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
			writing.assert_not_called()
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

	def test_commit_failure_unstages_only_keeps_applied_notes_and_retry_does_not_rewrite(self):
		self.prepare()
		hook = self.vault / ".git/hooks/pre-commit"
		hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
		hook.chmod(0o700)
		self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertIn("git commit 실패", self.stdout.getvalue())
		self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
		note = self.notes()["💼 Career/261024-p01.md"]
		self.assertEqual(note["status"], "applied")
		self.assertEqual(sha(self.career.read_bytes()), note["after"])
		self.assertIsNone(note["commit"])
		hook.unlink()
		with mock.patch.object(vault_io, "atomic_write", wraps=vault_io.atomic_write) as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 0)
			writing.assert_not_called()
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)
		self.assertTrue(self.notes()["💼 Career/261024-p01.md"]["commit"])
		self.assertNotIn("commit_error", self.plan())

	def test_add_failure_keeps_notes_unstaged(self):
		self.prepare()
		original_git = sync.git
		def fail_add(vault, *args):
			if "add" in args:
				return subprocess.CompletedProcess(args, 1, b"", b"synthetic")
			return original_git(vault, *args)
		with mock.patch.object(sync, "git", side_effect=fail_add):
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")
		self.assertEqual(self.notes()["💼 Career/261024-p01.md"]["status"], "applied")

	def test_tampered_after_or_body_hash_refuses_before_write(self):
		self.prepare()
		original = self.plan()
		for field in ("after", "body_hash"):
			with self.subTest(field=field):
				plan = json.loads(json.dumps(original))
				plan["notes"][0][field] = "0" * 64
				(self.base / "r1-plan.json").write_text(json.dumps(plan), encoding="utf-8")
				with mock.patch.object(vault_io, "atomic_write") as writing:
					self.assertEqual(self.command("apply", "--folder", "Career"), 1)
					writing.assert_not_called()
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

	def test_readback_failure_restores_original_and_records_failure(self):
		self.prepare()
		before = self.career.read_bytes()
		atomic = vault_io.atomic_write
		count = 0
		def corrupt_once(path, text):
			nonlocal count
			count += 1
			atomic(path, text + "오류" if count == 1 else text)
		with mock.patch.object(vault_io, "atomic_write", side_effect=corrupt_once):
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertEqual(self.career.read_bytes(), before)
		self.assertIn("원본 복원함", self.notes()["💼 Career/261024-p01.md"]["reason"])
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

	def test_apply_reconstructs_frozen_tags_not_changed_mapping(self):
		self.write("📖 Study/261024-p01.md", "---\ntags: ['JAVA_17']\n---\n본문\n")
		self.init_git("📖 Study")
		self.settings("r1-tags.toml", "[map]\nJAVA_17='jvm'\n")
		self.assertEqual(self.command("report"), 0)
		self.settings("r1-tags.toml", "[map]\nJAVA_17='xyz'\n")
		result = self.run_module("apply", "--folder", "Study")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn(b"tags: ['jvm']\n", (self.vault / "📖 Study/261024-p01.md").read_bytes())

	def test_symlink_replacement_and_malicious_plan_paths_are_never_opened(self):
		self.prepare()
		outside = self.root / "outside.md"
		outside.write_text("외부 합성 본문", encoding="utf-8")
		self.career.unlink()
		self.career.symlink_to(outside)
		original_open = Path.open
		def guarded(path, *args, **kwargs):
			self.assertNotEqual(path, self.career)
			self.assertNotEqual(path, outside)
			return original_open(path, *args, **kwargs)
		with mock.patch.object(Path, "open", guarded):
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertEqual(outside.read_text(encoding="utf-8"), "외부 합성 본문")
		original = self.plan()
		for path in ("../outside.md", "🔐 p01.md", ".obsidian/p01.md", str(outside)):
			with self.subTest(path=path):
				plan = json.loads(json.dumps(original))
				plan["notes"][0]["path"] = path
				(self.base / "r1-plan.json").write_text(json.dumps(plan), encoding="utf-8")
				self.assertEqual(self.command("apply", "--folder", "Career"), 2)
				self.assertNotIn(path, self.stderr.getvalue())

	def test_root_folder_is_supported(self):
		path = self.write("261024-root.md", fm.BOM + "본문\r\n".encode())
		before = path.read_bytes()
		self.init_git("261024-root.md")
		self.assertEqual(self.command("report"), 0)
		self.assertEqual(self.command("apply", "--folder", "root"), 0)
		after = path.read_bytes()
		self.assertTrue(after.startswith(fm.BOM))
		self.assertEqual(fm.split(after).body, before[len(fm.BOM):])
		self.assertNotIn("type", self.notes()["261024-root.md"]["additions"])

	def test_literal_wildcard_names_do_not_stage_other_notes(self):
		self.write("📖 Study/261024-[p01].md")
		other = self.write("📖 Study/261024-p.md")
		self.init_git("📖 Study")
		self.assertEqual(self.command("report"), 0)
		other.write_text("바뀐 본문", encoding="utf-8")
		self.assertEqual(self.command("apply", "--folder", "Study"), 0)
		changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD").decode().split("\0")
		self.assertEqual(changed, ["📖 Study/261024-[p01].md", ""])

	def test_temporary_symlink_does_not_overwrite_another_file(self):
		self.prepare()
		outside = self.root / "outside.md"
		outside.write_text("외부 합성 본문", encoding="utf-8")
		self.career.with_name(f".{self.career.name}.tmp").symlink_to(outside)
		before = self.career.read_bytes()
		self.assertEqual(self.command("apply", "--folder", "Career"), 1)
		self.assertEqual(outside.read_text(encoding="utf-8"), "외부 합성 본문")
		self.assertEqual(self.career.read_bytes(), before)
		self.assertIn("임시 파일", self.notes()["💼 Career/261024-p01.md"]["reason"])

	def test_rendered_body_mismatch_never_writes_even_when_after_hash_matches(self):
		self.prepare()
		plan = self.plan()
		note = plan["notes"][0]
		changed = b"---\ntype: career\n---\nchanged body"
		note["after"] = sha(changed)
		(self.base / "r1-plan.json").write_text(json.dumps(plan), encoding="utf-8")
		with mock.patch.object(fm, "render", return_value=(changed, [], False)), mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("apply", "--folder", "Career"), 1)
			writing.assert_not_called()

	def test_atomic_apply_preserves_bodies_existing_keys_crlf_and_bom(self):
		bodies = {}
		for index in range(8):
			nl = "\r\n" if index & 1 else "\n"
			bom = fm.BOM if index & 2 else b""
			body = ("# 본문" + nl + "중간\n끝\r\n").encode()
			header = (f"---{nl}aliases: ['p01']{nl}source: xyz{nl}type: reference{nl}tistory-id: 8{nl}---{nl}").encode() if index & 4 else b""
			path = self.write(f"📖 Study/261024-p{index}.md", bom + header + body)
			bodies[path] = (body, bom, header, nl)
		self.init_git("📖 Study")
		self.assertEqual(self.command("report"), 0)
		self.assertEqual(self.command("apply", "--folder", "Study"), 0)
		for path, (body, bom, header, nl) in bodies.items():
			with self.subTest(path=path.name):
				data = path.read_bytes()
				self.assertEqual(fm.split(data).body, body)
				self.assertEqual(fm.split(data).bom, bom)
				self.assertIn((f"sensitivity: personal{nl}created: 2026-10-24{nl}---{nl}").encode(), data)
				if header:
					self.assertTrue(data.startswith(bom + header[:-(len(nl) + 3)]))
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)

	def test_decomposed_hangul_path_in_git_index_is_committed(self):
		# git 색인이 NFD 경로를 갖고 있으면 pathspec 글자 배열이 달라 오류 없이 빠지던 문제(10-24)
		name = unicodedata.normalize("NFD", "💼 Career/한글 노트.md")
		self.write(name, "본문\n")
		self.write("📖 Study/261024-p02.md", "본문 p02\n")
		self.git("init", "-q", "-b", "main")
		for key, value in (("user.name", "xyz"), ("user.email", "p01"), ("commit.gpgsign", "false")):
			self.git("config", key, value)
		self.git("-c", "core.precomposeunicode=false", "add", "--", name, "📖 Study")
		self.git("commit", "-q", "-m", "합성 시작")
		self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
		self.assertEqual(self.command("apply", "--folder", "Career"), 0, self.stderr.getvalue())
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)
		self.assertEqual(self.git("status", "--porcelain").strip(), b"")
		self.assertIn("파일 1개", self.git("log", "-1", "--format=%s").decode())

	def test_ignored_note_is_reported_and_others_still_commit(self):
		self.write("💼 Career/261024-p01.md", "본문 p01\n")
		self.write("💼 Career/261024-p02.md", "본문 p02\n")
		self.write(".gitignore", "💼 Career/261024-p02.md\n")
		self.init_git(".gitignore", "💼 Career/261024-p01.md")
		self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
		self.assertEqual(self.command("apply", "--folder", "Career"), 0, self.stderr.getvalue())
		self.assertIn("무시 목록", self.stdout.getvalue())
		self.assertIn("파일 1개", self.git("log", "-1", "--format=%s").decode())
