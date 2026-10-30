"""보관함 파서·기존 요약·문서 색인·Git 전체 이력의 합성 검증."""

import gzip
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from collector import backfill, backfill_agy, backfill_output, backfill_repos, config, pipeline, sessions, vault
from collector.backfill_store import Store
from collector.ledger import Ledger
from test_backfill_support import BackfillCase


class BackfillSessionSourcesTest(BackfillCase):
	def test_cross_day_session_uses_last_day_for_daily_note(self):
		path = self.codex()
		rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
		rows[-1]["timestamp"] = "2026-08-02T01:10:00Z"
		path.write_bytes(gzip.compress("\n".join(json.dumps(row) for row in rows).encode()))
		self.assertEqual(self.prepare(), 0)
		self.assertEqual(self.batch()[1][0]["data"]["day"], "2026-08-02")

	def test_legacy_unprepared_session_uses_last_day(self):
		path = self.codex()
		rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
		rows[-1]["timestamp"] = "2026-08-02T01:10:00Z"
		path.write_bytes(gzip.compress("\n".join(json.dumps(row) for row in rows).encode()))
		self.assertEqual(self.prepare(), 0)
		batch, items = self.batch()
		task = items[0]["data"]
		task["day"] = "2026-08-01"
		sources = config.load_sources()
		store = Store(self.hub / "ledger.db")
		try:
			result, reason = backfill.prepare_session(store, batch["id"], task, sources, sources.repos[0],
				config.load_plans(), self.clock, set())
		finally:
			store.close()
		self.assertEqual(reason, "")
		self.assertEqual(task["day"], "2026-08-02")
		self.assertTrue(result["target"].endswith("2026-08-02.md"))

	def test_zstd_archive_uses_standard_library_when_available(self):
		try:
			from compression import zstd
		except ImportError:
			self.skipTest("이 Python에는 표준 zstd 해제가 없음")
		path = self.codex()
		data = gzip.decompress(path.read_bytes())
		path.unlink()
		target = self.hub / "archive/codex/archived_sessions/s01.jsonl.zst"
		target.parent.mkdir(parents=True)
		target.write_bytes(zstd.compress(data))
		self.prepare()
		self.assertEqual(self.ai.call_count, 1)
		self.assertEqual(self.batch()[1][0]["key"], "codex:s01")

	def test_archive_with_unknown_cwd_is_deferred_without_ai(self):
		self.codex(cwd=None)
		self.prepare()
		self.ai.assert_not_called()
		store = Store(self.hub / "ledger.db", readonly=True)
		scope = store.scope(store.rows("c4_scopes")[0]["id"])
		self.assertEqual(scope["data"]["unclassified"], 1)
		self.assertEqual(scope["data"]["deferred"][0]["id"], "codex:s01")
		store.close()

	def test_changed_live_source_is_detected_by_metadata_without_reading_it(self):
		self.codex()
		original = backfill.sessions.archive.ROOTS["codex"] / "sessions/2026/08/01/s01.jsonl"
		original.parent.mkdir(parents=True)
		original.write_text("이 본문을 읽으면 실패", encoding="utf-8")
		opening = Path.open
		def checked(path, *args, **kwargs):
			if path == original:
				raise AssertionError("원천 본문 접근")
			return opening(path, *args, **kwargs)
		with mock.patch.object(Path, "open", new=checked):
			self.prepare()
		self.ai.assert_not_called()
		self.assertIn("보관함", self.batch()[1][0]["data"]["hold_reason"])

	def test_conflicting_or_normalized_workspaces_do_not_send_company_data(self):
		self.codex(cwd=str(self.repo / ".." / "xyz"))
		self.prepare()
		self.ai.assert_not_called()
		self.assertEqual(self.snapshot(self.vault), {})

	def test_gzip_archive_keeps_c1_key_and_digest_without_original(self):
		path = self.codex()
		text = gzip.decompress(path.read_bytes()).decode()
		raw = self.root / "s01.jsonl"
		raw.write_text(text, encoding="utf-8")
		original = sessions._codex_file(raw)
		archived = backfill.sessions.read(path, "codex", {})[0]
		self.assertEqual(original.key, archived.key)
		self.assertEqual(pipeline.digest(original), pipeline.digest(archived))
		raw.unlink()
		self.assertEqual(self.prepare(), 0)
		prompt = self.ai.call_args.args[1]
		self.assertIn("수정 완료", prompt)
		self.assertNotIn("중간 진행", prompt)

	def test_questions_and_split_final_only_excludes_tools_and_analysis(self):
		rows = [{"type": "session_meta", "payload": {"id": "s01", "cwd": str(self.repo),
			"timestamp": "2026-08-01T01:00:00Z"}}]
		for role, text, channel, mid in (("user", "첫 질문", None, None),
				("assistant", "옛 최종 답", "final", "old"), ("user", "둘째 질문", None, None),
				("assistant", "비공개 생각", "analysis", "hidden"),
				("assistant", "완료 답 앞", "final", "last"), ("assistant", "완료 답 뒤", "final", "last")):
			rows.append({"type": "response_item", "timestamp": "2026-08-01T01:10:00Z", "payload": {
				"type": "message", "id": mid, "role": role, "channel": channel,
				"content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}})
		rows.append({"type": "response_item", "payload": {"type": "function_call_output", "output": "도구 덤프"}})
		self.gzip("codex/archived_sessions/s01.jsonl.gz", "\n".join(json.dumps(r) for r in rows))
		self.prepare()
		prompt = self.ai.call_args.args[1]
		for text in ("첫 질문", "둘째 질문", "완료 답 앞", "완료 답 뒤"):
			self.assertIn(text, prompt)
		for text in ("옛 최종 답", "비공개 생각", "도구 덤프"):
			self.assertNotIn(text, prompt)

	def test_incomplete_session_is_held_and_large_paste_is_trimmed(self):
		self.codex(sid="unfinished", answer=None)
		self.codex(sid="dump", question="\n".join(f"파일 원문 {i}" for i in range(100)) + "\nimport acme")
		self.prepare()
		self.assertEqual([t["state"] for t in self.batch()[1]].count("held"), 1)
		self.assertEqual(self.ai.call_count, 1)
		prompt = self.ai.call_args.args[1]
		self.assertIn("(긴 붙여넣기·파일 본문 생략)", prompt)
		self.assertIn("파일 원문 19", prompt)
		self.assertNotIn("파일 원문 20", prompt)
		self.assertNotIn("import acme", prompt)

	def test_long_prose_is_split_without_omission_and_resumes_parts(self):
		question = "가나다라마바사 " * 1500
		self.plans(limit=1)
		self.codex(question=question)
		self.prepare()
		batch, tasks = self.batch()
		self.assertGreater(tasks[0]["data"]["parts"], 1)
		self.assertEqual(tasks[0]["state"], "held")
		self.assertEqual(self.ai.call_count, 1)
		for _ in range(tasks[0]["data"]["parts"] - 1):
			self.clock += timedelta(days=1)
			self.command("--resume", batch["id"])
		self.assertEqual(self.ai.call_count, tasks[0]["data"]["parts"])
		self.assertEqual(self.batch()[1][0]["state"], "prepared")
		prompts = [call.args[1] for call in self.ai.call_args_list]
		body = "".join(p[len(backfill.summarize.PROMPT_HEAD):].split("\n", 1)[1]
			[:-len(backfill.summarize.PROMPT_TAIL)] for p in prompts)
		self.assertEqual(body, "[사용자 질문] " + question.strip() + "\n[AI 마지막 완료 답] 수정 완료")
		self.assertTrue(all(len(p) <= backfill.summarize.MAX_PROMPT_CHARS for p in prompts))

	def test_since_boundary_subagent_and_active_sessions(self):
		self.codex(sid="before", day="2026-07-21")
		self.codex(sid="boundary", day="2026-07-22")
		self.codex(sid="sub", thread_source="subagent")
		self.codex(sid="exec", originator="codex_exec")
		self.codex(sid="future", day="2026-10-24")
		self.prepare()
		self.assertEqual([t["key"] for t in self.batch()[1]], ["codex:boundary"])

	def test_claude_and_gemini_archives_use_last_text_answer(self):
		rows = []
		for kind, content in (("user", "Claude 합성 질문"), ("assistant", "중간 답"),
				("assistant", "Claude 완료 답")):
			rows.append({"type": kind, "entrypoint": "cli", "sessionId": "c01", "cwd": str(self.repo),
				"timestamp": "2026-08-01T01:00:00Z", "message": {"content": content, "stop_reason": "end_turn"}})
		self.gzip("claude/acme/c01.jsonl.gz", "\n".join(json.dumps(r) for r in rows))
		self.gzip("gemini/acme/chats/session-g01.json.gz", json.dumps({"sessionId": "g01",
			"cwd": str(self.repo), "startTime": "2026-08-01T01:00:00Z", "lastUpdated": "2026-08-01T01:10:00Z",
			"messages": [{"type": "user", "content": "Gemini 합성 질문"},
				{"type": "gemini", "content": "중간 답"}, {"type": "gemini", "content": "Gemini 완료 답"}]}))
		self.prepare()
		self.assertEqual(self.ai.call_count, 2)
		self.assertTrue(all("중간 답" not in c.args[1] for c in self.ai.call_args_list))

	def test_matching_codex_summary_reuses_without_ai_and_bad_metadata_does_not(self):
		self.codex(sid="good")
		self.codex(sid="stale")
		self.codex(sid="wrong")
		folder = self.root / "summaries"
		folder.mkdir()
		for sid, cwd, updated in (("good", str(self.repo), "2026-08-02T01:00:00Z"),
				("stale", str(self.repo), "2026-07-31T01:00:00Z"),
				("wrong", str(self.root / "xyz"), "2026-08-02T01:00:00Z")):
			(folder / (sid + ".md")).write_text(f"thread_id: {sid}\ncwd: {cwd}\nupdated_at: {updated}\n\n"
				"# 기존 요약\n\n- 검토한 결정\n```python\nprint('코드')\n```\n", encoding="utf-8")
		self.prepare()
		self.assertEqual(self.ai.call_count, 2)
		good = next(t for t in self.batch()[1] if t["key"] == "codex:good")
		self.assertEqual(good["data"]["result"]["method"], "codex-summary")
		self.assertNotIn("print(", good["data"]["result"]["body"])


class BackfillRepoSourcesTest(BackfillCase):
	def docs(self):
		(self.repo / "docs").mkdir()
		(self.repo / "docs/a.md").write_text("# 합성 문서\n\n본문 password=sample-value", encoding="utf-8")
		self.sources(doc_dirs=("docs",))

	def test_documents_are_indexed_without_ai_or_today_daily_note(self):
		self.docs()
		(self.repo / "docs/b.txt").write_text("제목 없는 문서", encoding="utf-8")
		before = self.snapshot(self.repo)
		with mock.patch.object(backfill.sessions, "inventory", side_effect=AssertionError("대화 읽기")):
			self.assertEqual(self.prepare("repos", "--since", "2026-10-22"), 0)
		batch, items = self.batch()
		self.ai.assert_not_called()
		self.assertEqual(self.snapshot(self.repo), before)
		self.assertEqual(self.snapshot(self.vault), {})
		self.command("--apply", batch["id"])
		paths = list((self.vault / "📥 Inbox/AI").glob("*.md"))
		self.assertEqual(len(paths), 1)
		text = paths[0].read_text()
		self.assertIn("type: reference", text)
		self.assertIn("합성 문서", text)
		self.assertIn("Git 없는 원천", text)
		self.assertIn("내용 SHA-256", text)
		self.assertNotIn("sample-value", text)
		self.assertNotIn("본문", text)
		self.assertTrue(all(t["data"]["result"]["target"] == str(paths[0].relative_to(self.vault)) for t in items))
		written = self.snapshot(self.vault)
		self.command("--apply", batch["id"])
		self.assertEqual(self.snapshot(self.vault), written)

	def test_git_full_local_history_uses_full_sha_and_current_author(self):
		self.docs()
		self.init_git()
		self.git("add", "docs/a.md")
		self.git("commit", "-qm", "합성 첫 기록", day="2026-01-01T10:00:00+09:00")
		first = self.git("rev-parse", "HEAD")
		(self.repo / "docs/a.md").write_text("# 합성 문서 변경\n", encoding="utf-8")
		self.git("add", "docs/a.md")
		self.git("commit", "-qm", "다른 저자의 기록", day="2026-02-01T10:00:00+09:00", email="xyz@example.test")
		other = self.git("rev-parse", "HEAD")
		self.git("branch", "xyz")
		(self.repo / "docs/b.md").write_text("# 별도 문서\n", encoding="utf-8")
		self.git("add", "docs/b.md")
		self.git("commit", "-qm", "합성 다음 기록", day="2026-03-01T10:00:00+09:00")
		second = self.git("rev-parse", "HEAD")
		before = self.snapshot(self.repo)
		self.prepare("repos")
		self.assertEqual(before, self.snapshot(self.repo))
		batch, items = self.batch()
		commits = [t for t in items if t["data"]["type"] == "commits"]
		self.assertEqual(len(commits), 2)
		body = "\n".join(t["data"]["result"]["body"] for t in commits)
		self.assertIn(first, body)
		self.assertIn(second, body)
		self.assertNotIn(other, body)
		self.assertTrue(all(t["data"]["result"]["block"] == "backfill:repo:p01:commits" for t in commits))
		document = next(t for t in items if t["data"].get("relative") == "docs/a.md")
		self.assertIn("2026-01-01", document["data"]["body"])
		self.assertIn("2026-02-01", document["data"]["body"])
		self.command("--apply", batch["id"])
		self.ai.assert_not_called()

	def test_protected_paths_and_links_are_excluded_before_open(self):
		self.docs()
		(self.repo / "docs/🔐private").mkdir()
		secret = self.repo / "docs/🔐private/hidden.md"
		secret.write_text("읽지 않을 내용", encoding="utf-8")
		linked = self.repo / "docs/linked.md"
		linked.symlink_to(secret)
		opening = Path.open

		def checked(path, *args, **kwargs):
			if "🔐" in str(path) or path == linked:
				raise AssertionError("보호 경로 열기")
			return opening(path, *args, **kwargs)

		with mock.patch.object(Path, "open", new=checked):
			self.prepare("repos")
		self.assertEqual(len(self.batch()[1]), 1)

	def test_explicit_html_unsupported_formats_and_document_filters(self):
		self.docs()
		(self.repo / "docs/a.html").write_text("<title>합성 설계</title><p>본문</p>", encoding="utf-8")
		(self.repo / "docs/b.pdf").write_bytes(b"unsupported")
		(self.repo / "docs/skip.md").write_text("제외 문서", encoding="utf-8")
		self.sources(doc_dirs=("docs",), extra="doc_include=['docs/*.md','docs/*.html','docs/*.pdf']\ndoc_exclude=['docs/skip.md']\n")
		self.prepare("repos")
		_, items = self.batch()
		self.assertEqual(len(items), 3)
		self.assertEqual(sum(t["state"] == "held" for t in items), 1)
		self.assertIn("합성 설계", "\n".join(t["data"].get("body", "") for t in items))

	def test_c4_repo_blocks_survive_c1_today_and_yesterday_collection(self):
		self.docs()
		self.init_git()
		for day in ("2026-10-22", "2026-10-23"):
			(self.repo / "docs/a.md").write_text("# " + day, encoding="utf-8")
			self.git("add", "docs/a.md")
			self.git("commit", "-qm", "합성 기록", day=day + "T01:00:00+09:00")
		self.prepare("repos")
		batch, items = self.batch()
		self.command("--apply", batch["id"])
		before = {i["key"]: vault.block_body(self.vault / i["data"]["result"]["target"],
			i["data"]["result"]["block"]) for i in items}
		ledger = Ledger(self.hub / "ledger.db")
		runner = pipeline.Pipeline(config.load_sources(), config.load_plans(), ledger, False)
		runner.today = date(2026, 10, 23)
		runner.handle_repos(date(2026, 10, 22))
		ledger.close()
		for item in items:
			prepared = item["data"]["result"]
			self.assertEqual(vault.block_body(self.vault / prepared["target"], prepared["block"]), before[item["key"]])
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual(backfill_output.audit_batch(store, batch["id"], config.load_sources()), [])
		store.close()
		self.assertIn("repo:acme:commits", vault.daily_path(self.vault, "personal", date(2026, 10, 23)).read_text())

	def test_repos_daily_files_are_batched_by_ten_not_documents(self):
		self.docs()
		for i in range(15):
			(self.repo / f"docs/{i:02d}.md").write_text("# 문서", encoding="utf-8")
		self.init_git()
		for i in range(11):
			(self.repo / "counter").write_text(str(i), encoding="utf-8")
			self.git("add", "counter")
			self.git("commit", "-qm", "합성 기록", day=f"2026-01-{i + 1:02d}T10:00:00+09:00")
		self.prepare("repos")
		batch, items = self.batch()
		self.assertEqual(sum(t["data"]["type"] == "document" for t in items), 16)
		self.assertEqual(sum(t["data"]["type"] == "commits" for t in items), 10)
		self.command("--apply", batch["id"])
		self.prepare("repos")
		self.assertEqual(len(self.batch()[1]), 1)

	def test_sessions_mode_does_not_scan_documents_or_git(self):
		self.codex()
		with mock.patch.object(backfill_repos, "inventory", side_effect=AssertionError("저장소 읽기")):
			self.assertEqual(self.prepare(), 0)

	def test_git_failure_is_held_not_reported_as_empty_history(self):
		self.docs()
		with mock.patch.object(backfill_repos, "resolve_roots", side_effect=config.ConfigError("합성 Git 실패")):
			self.prepare("repos")
		self.assertTrue(any(t["state"] == "held" for t in self.batch()[1]))

	def test_nested_real_git_root_is_used_for_all_local_commits(self):
		self.docs()
		nested = self.repo / "xyz"
		nested.mkdir()
		self.init_git(nested)
		(nested / "README.md").write_text("# 합성 중첩 저장소", encoding="utf-8")
		self.git("add", "README.md", cwd=nested)
		self.git("commit", "-qm", "합성 로컬 이력", cwd=nested, day="2026-01-01T10:00:00+09:00")
		full = self.git("rev-parse", "HEAD", cwd=nested)
		self.prepare("repos")
		self.assertIn(full, "\n".join(t["data"].get("result", {}).get("body", "") for t in self.batch()[1]))
		self.ai.assert_not_called()

	def test_registered_subdirectory_does_not_expand_to_unapproved_git_parent(self):
		self.docs()
		self.init_git()
		self.git("add", "docs/a.md")
		self.git("commit", "-qm", "합성 기록", day="2026-01-01T10:00:00+09:00")
		with self.assertRaises(config.ConfigError):
			backfill_repos.resolve_roots(self.repo / "docs")

	def test_existing_c1_short_sha_is_referenced_instead_of_duplicated(self):
		self.docs()
		self.init_git()
		self.git("add", "docs/a.md")
		self.git("commit", "-qm", "합성 기록", day="2026-01-01T10:00:00+09:00")
		full = self.git("rev-parse", "HEAD")
		path = vault.daily_path(self.vault, "personal", date(2026, 1, 1))
		vault.upsert_block(path, "repo:acme:commits", f"- 커밋 `{full[:7]}` 합성 기록", "acme", date(2026, 1, 1), "personal")
		self.prepare("repos")
		batch, tasks = self.batch()
		commit = next(t for t in tasks if t["data"]["type"] == "commits")
		self.assertEqual(commit["state"], "existing")
		self.command("--apply", batch["id"])
		self.assertNotIn("backfill:repo:p01:commits", path.read_text())

	def test_document_scope_traversal_is_rejected(self):
		self.sources(doc_dirs=("../xyz",))
		self.assertEqual(self.prepare("repos"), 2)
		self.ai.assert_not_called()
