"""R1 읽기 전용 계획·분류·날짜·설정 오류와 비밀 경로 보호."""

import json
import stat
from datetime import datetime
from pathlib import Path
from unittest import mock

from collector.backfill_common import sha
from vaultreorg import frontmatter as fm, rules

from test_vaultreorg_support import ReorgCase


class ReportTest(ReorgCase):
	def test_module_report_is_readonly_numbered_private_and_body_free(self):
		self.write("📖 Study/261024-p01.md", "# 본문 흔적 p01\n")
		self.settings("261024-04-other.md", "합성 기존 문서")
		before = self.snapshot()
		clock = datetime(2026, 10, 24, 12)
		with mock.patch("collector.backfill_common.now", return_value=clock):
			self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
		plan = self.plan()
		self.assertEqual(before, self.snapshot())
		path = Path(plan["report"])
		self.assertEqual(path.name, "261024-05-r1-report.md")
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE((self.base / "r1-plan.json").stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o700)
		for output in (self.report_text(), json.dumps(plan), self.stdout.getvalue()):
			self.assertNotIn("본문 흔적", output)
		last = self.stdout.getvalue().splitlines()[-1]
		self.assertIn(str(path), last)
		self.assertIn("'R1 Study 반영해'", last)
		result = self.run_module("report")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(before, self.snapshot())
		self.assertNotEqual(self.plan()["report"], plan["report"])

	def test_protected_excluded_and_symlink_paths_are_not_opened_or_hashed(self):
		self.write("📖 Study/261024-ok.md", "허용 본문")
		self.write("🔐 p01/inner.md", "보호 폴더 본문")
		self.write("📖 Study/🔐 p02.md", "보호 노트 본문")
		for name in (".obsidian", ".git", ".trash"):
			self.write(name + "/p01.md", "제외 본문")
		self.write("📖 Study/data.png", "이미지")
		outside = self.root / "outside"
		outside.mkdir()
		(outside / "p01.md").write_text("링크 본문", encoding="utf-8")
		(self.vault / "linked").symlink_to(outside, target_is_directory=True)
		(self.vault / "p01.md").symlink_to(outside / "p01.md")
		original_open, original_iterdir = Path.open, Path.iterdir
		opened, walked = [], []
		def guarded_open(path, *args, **kwargs):
			if path.is_relative_to(self.vault):
				self.assertFalse(rules.protected(path))
				self.assertFalse(rules.EXCLUDED.intersection(path.relative_to(self.vault).parts))
				self.assertFalse(path.is_symlink())
				opened.append(path)
			return original_open(path, *args, **kwargs)
		def guarded_iterdir(path):
			if path != self.vault and path.is_relative_to(self.vault):
				self.assertFalse(rules.protected(path))
				self.assertFalse(rules.EXCLUDED.intersection(path.relative_to(self.vault).parts))
				self.assertFalse(path.is_symlink())
				walked.append(path)
			return original_iterdir(path)
		with mock.patch.object(Path, "open", guarded_open), mock.patch.object(Path, "iterdir", guarded_iterdir), \
			mock.patch("vaultreorg.report.sha", wraps=sha) as hashing:
			self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
			self.assertEqual(len(opened), 1)
			self.assertEqual(len(walked), 1)
			self.assertEqual(hashing.call_count, 3)
		plan = self.plan()
		self.assertEqual(plan["skipped"], {"protected": 2, "excluded": 3, "symlink": 2})
		self.assertEqual(list(self.notes()), ["📖 Study/261024-ok.md"])
		for text in (self.report_text(), json.dumps(plan, ensure_ascii=False), self.stdout.getvalue()):
			self.assertNotIn("🔐", text)
			self.assertNotIn("보호 폴더 본문", text)
			self.assertNotIn("링크 본문", text)

	def test_all_folder_rules_and_company_filename_rules(self):
		folders = {"💼 Career": "career", "📖 Study": "study", "✍️ Blog": "blog", "🗺️ MOC": "moc",
			"🧰 Setup": "reference", "🤖 AI & Prompts": "reference", "🗂️ Projects": "project",
			"📥 Inbox": "daily", "🏢 Company": "reference"}
		for folder in (*folders, "🌱 Personal", "xyz"):
			self.write(folder + "/261024-p01.md")
		self.write("261024-root.md")
		for word in ("오류", "에러", "장애", "이슈", "해결"):
			self.write(f"🏢 Company/261024-{word}.md")
		self.write("🏢 Company/장애/261024-p01.md")
		self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
		notes = self.notes()
		for folder, kind in folders.items():
			with self.subTest(folder=folder):
				note = notes[folder + "/261024-p01.md"]
				self.assertEqual(note["additions"]["type"], kind)
				self.assertEqual(note["additions"]["sensitivity"], "company" if kind == "reference" and "Company" in folder else "personal")
		for word in ("오류", "에러", "장애", "이슈", "해결"):
			self.assertEqual(notes[f"🏢 Company/261024-{word}.md"]["additions"]["type"], "troubleshoot")
		self.assertEqual(notes["🏢 Company/장애/261024-p01.md"]["additions"]["type"], "reference")
		for path in ("🌱 Personal/261024-p01.md", "xyz/261024-p01.md", "261024-root.md"):
			self.assertNotIn("type", notes[path]["additions"])
			self.assertTrue(notes[path]["needs_type"])
			self.assertIn(path, self.report_text())
		self.assertEqual(notes["261024-root.md"]["folder"], "root")
		self.assertIn("'R1 root 반영해'", self.stdout.getvalue())

	def test_dictionary_matches_body_and_path_case_insensitively_by_original_index(self):
		self.write("📖 Study/261024-p01.md", "본문 ACME 그리고 XyZ")
		self.write("📖 Study/261024-ACME.md", "한글")
		self.write("🏢 Company/261024-p01.md", "acme xyz")
		self.write("📖 Study/261024-clean.md", "x")
		self.assertEqual(self.command("report"), 0)
		notes = self.notes()
		self.assertEqual(notes["📖 Study/261024-p01.md"]["company_entries"], [1, 3])
		self.assertEqual(notes["📖 Study/261024-ACME.md"]["company_entries"], [1])
		self.assertEqual(notes["🏢 Company/261024-p01.md"]["company_entries"], [])
		self.assertEqual(notes["📖 Study/261024-clean.md"]["company_entries"], [])
		self.assertEqual(notes["📖 Study/261024-p01.md"]["additions"]["sensitivity"], "personal")
		self.assertIn("사전 항목 1, 3", self.report_text())
		self.assertNotIn("본문 ACME", self.report_text())

	def test_created_evidence_precedence_and_real_calendar_dates(self):
		paths = ["📖 Study/261024-path.md", "📖 Study/261024-hash.md", "📖 Study/240229-file.md",
			"📖 Study/p01-2024-02-29.md", "📖 Study/250229-invalid.md", "📖 Study/2026-02-30-invalid.md",
			"📖 Study/p01.md"]
		for index, path in enumerate(paths):
			self.write(path, f"합성 본문 {index}")
		digest = sha((self.vault / paths[1]).read_bytes())
		self.dates([{"path": paths[0], "birth": "2023-04-03T12:00:00+09:00", "mtime": "2023-03-02T00:00:00+09:00", "sha256": "a" * 64},
			{"path": "old/p01.md", "birth": "2021-01-02T00:00:00+09:00", "mtime": "2021-02-03T00:00:00+09:00", "sha256": digest},
			{"path": "old/p02.md", "birth": "2020-12-30T00:00:00+09:00", "mtime": "2020-12-31T00:00:00+09:00", "sha256": digest},
			{"path": "old/p03.md", "birth": "2000-01-01T00:00:00+09:00", "sha256": sha((self.vault / paths[0]).read_bytes())}])
		self.assertEqual(self.command("report"), 0)
		notes = self.notes()
		for path, day, source in zip(paths[:4], ["2023-03-02", "2020-12-30", "2024-02-29", "2024-02-29"],
			["옛 볼트 경로", "옛 볼트 내용", "파일 이름", "파일 이름"]):
			self.assertEqual(notes[path]["additions"]["created"], day)
			self.assertEqual(notes[path]["created_source"], source)
		for path in paths[4:]:
			self.assertNotIn("created", notes[path]["additions"])
			self.assertTrue(notes[path]["missing_created"])
			self.assertEqual(notes[path]["created_source"], "없음")

	def test_old_month_cluster_warning_at_fifty_and_not_filename_dates(self):
		old = []
		for index in range(99):
			path = f"📖 Study/p{index:02d}.md"
			self.write(path, f"합성 {index}")
			day = "2020-05-01" if index < 50 else "2020-06-01"
			old.append({"path": path, "birth": day + "T00:00:00+09:00"})
		self.write("📖 Study/200501-file.md")
		self.dates(old)
		self.assertEqual(self.command("report"), 0)
		notes = self.notes()
		self.assertEqual(sum(note["possible_move_day"] for note in notes.values()), 50)
		self.assertFalse(notes["📖 Study/200501-file.md"]["possible_move_day"])
		self.assertEqual(notes["📖 Study/p00.md"]["additions"]["created"], "2020-05-01")
		self.assertIn("옮긴 날일 수 있음", self.report_text())

	def test_overrides_take_precedence_and_existing_keys_still_win(self):
		self.write("🌱 Personal/p01.md")
		self.write("📖 Study/261024-p02.md")
		self.write("📖 Study/p03.md", "---\ntype: custom\nsensitivity: personal\ncreated: '2001-01-01'\n---\n본문")
		self.settings("r1-overrides.toml", "[[note]]\npath='🌱 Personal/p01.md'\ntype='reference'\nsensitivity='company'\ncreated=2020-02-29\n"
			"[[note]]\npath='📖 Study/261024-p02.md'\ntype='blog'\ncreated='2021-01-01'\n"
			"[[note]]\npath='📖 Study/p03.md'\ntype='study'\nsensitivity='company'\ncreated='2026-01-01'\n")
		self.assertEqual(self.command("report"), 0, self.stderr.getvalue())
		notes = self.notes()
		self.assertEqual(notes["🌱 Personal/p01.md"]["additions"], {"type": "reference", "sensitivity": "company", "created": "2020-02-29"})
		self.assertFalse(notes["🌱 Personal/p01.md"]["needs_type"])
		self.assertEqual(notes["📖 Study/261024-p02.md"]["additions"]["type"], "blog")
		self.assertEqual(notes["📖 Study/261024-p02.md"]["additions"]["created"], "2021-01-01")
		self.assertEqual(notes["📖 Study/p03.md"]["additions"], {})
		self.assertEqual(notes["📖 Study/p03.md"]["status"], "unchanged")

	def test_tags_report_counts_notes_masks_dictionary_values_and_lists_unsupported(self):
		for name in ("p01", "p02"):
			self.write(f"📖 Study/261024-{name}.md", "---\ntags: [JAVA_17, 'JAVA_17', 한글, 'ACME Tag']\n---\n본문")
		self.write("📖 Study/261024-p03.md", "---\ntags: p01\n---\n본문")
		self.settings("r1-tags.toml", "[map]\n'한글'='korean-tag'\n")
		self.assertEqual(self.command("report"), 0)
		text = self.report_text()
		self.assertIn("| `JAVA_17` | `java-17` | 2 |", text)
		self.assertIn("| `한글` | `korean-tag` | 2 |", text)
		self.assertNotIn("ACME", text)
		self.assertNotIn("acme", text)
		self.assertIn("[사전 항목 1]", text)
		self.assertTrue(self.notes()["📖 Study/261024-p03.md"]["unsupported_tags"])

	def test_toml_syntax_and_semantic_errors_have_line_number_exit_two_without_writes(self):
		self.write("📖 Study/261024-p01.md")
		cases = [("r1-overrides.toml", "[[note]]\npath='p01.md'\ntype=[\n", 4),
			("r1-overrides.toml", "[[note]]\npath='p01.md'\ncreated='2026-02-30'\n", 3),
			("r1-overrides.toml", "[[note]]\npath='../p01.md'\n", 2),
			("r1-overrides.toml", "[[note]]\npath='p01.md'\nsensitivity='xyz'\n", 3),
			("r1-overrides.toml", "[[note]]\npath='p01.md'\ntype=[]\n", 3),
			("r1-tags.toml", "[map]\n'JAVA_17'=3\n", 2),
			("r1-tags.toml", "[map]\n'JAVA_17'='xyz\n", 3)]
		for name, text, line in cases:
			with self.subTest(name=name, text=text):
				for old in ("r1-overrides.toml", "r1-tags.toml"):
					(self.base / old).unlink(missing_ok=True)
				self.settings(name, text)
				before = self.snapshot()
				self.assertEqual(self.command("report"), 2, self.stderr.getvalue())
				self.assertIn(f"{line}행", self.stderr.getvalue())
				self.assertEqual(before, self.snapshot())
				self.assertFalse((self.base / "r1-plan.json").exists())

	def test_malformed_frontmatter_is_reported_without_body_leak(self):
		self.write("📖 Study/p01.md", "---\ntags: [p01]\n닫힘 없는 본문")
		self.assertEqual(self.command("report"), 0)
		self.assertEqual(self.notes()["📖 Study/p01.md"]["status"], "failed")
		self.assertNotIn("닫힘 없는 본문", self.report_text())
