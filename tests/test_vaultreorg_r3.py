"""R3의 개인정보 경계·바이트 보존·호출 한도·묶음 커밋을 합성 자료로 시험한다."""

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from unittest import mock

from collector import summarize, vault as vault_io
from collector.backfill_common import locked
from collector.config import load_plans
from collector.ledger import Ledger
from vaultreorg import frontmatter, r3_select, r3_text
from vaultsync import sync

# 기존 R1 공통 fixture도 모듈명 실행·discover 양쪽에서 같은 객체를 쓴다.
if __package__:
	from . import test_vaultreorg_support as support
	sys.modules.setdefault("test_vaultreorg_support", support)
else:
	import test_vaultreorg_support as support

SUMMARY = {"lines": ["기록을 확인했다.", "판단의 근거를 정리했다.", "남은 작업을 확인했다."]}
# 짧은 study 는 대상에서 빠지므로 숫자·영문 없는 합성 문장으로 500자를 넘긴다
BODY = "\n".join(SUMMARY["lines"]) + "\n" + "길이를 채우는 합성 문장이다. " * 30


class R3Test(support.ReorgCase):
	def setUp(self):
		super().setUp()
		self.dictionary(["가짜회사", "가짜서비스"])
		self.plans()
		self.calls = []
		self.real_call = summarize.call
		self.call = mock.patch.object(summarize, "call", side_effect=self.fake)
		self.call.start()
		self.addCleanup(self.call.stop)

	def plans(self, limits=(None, None, None)):
		lines = []
		ids = (*r3_text.ORDER, "agy-personal", "claude-personal", "xyz-local")
		for index, (pid, tool) in enumerate(zip(ids, ("agy", "agy", "codex", "agy", "claude", "ollama"))):
			owner = "company" if tool == "ollama" else "personal"
			lines.extend(["[[plans]]", f"id='{pid}'", f"tool='{tool}'", f"owner='{owner}'",
				"model='xyz'", "allow=['personal']" if owner == "personal" else "allow=['company']"])
			if index < 3 and limits[index] is not None:
				lines.append(f"daily_limit_calls={limits[index]}")
		lines.extend(["[routing]", "personal=['claude-personal','agy-personal','codex-personal']"])
		(self.hub / "plans.toml").write_text("\n".join(lines), encoding="utf-8")

	def note(self, name="Study/p01.md", kind="study", body=BODY, sensitivity="personal", nl="\n", bom=b""):
		classification = f"sensitivity: {sensitivity}{nl}" if sensitivity is not None else ""
		header = f"---{nl}type: {kind}{nl}{classification}---{nl}"
		return self.write(name, bom + header.encode() + body.encode())

	def fake(self, plan, prompt, *, cwd):
		self.calls.append((plan.id, prompt, cwd))
		self.assertEqual(list(cwd.iterdir()), [])
		self.assertFalse(cwd.is_relative_to(self.vault))
		value = SUMMARY if '출력 형식: {"lines"' in prompt else {"done": SUMMARY["lines"][0]}
		return json.dumps(value, ensure_ascii=False)

	def plan(self):
		return json.loads((self.base / "r3-plan.json").read_text(encoding="utf-8"))

	def preview(self):
		self.assertEqual(self.command("r3-report"), 0, self.stderr.getvalue())
		return self.notes()

	def guard(self, forbidden=(), header_only=()):
		original = Path.open
		def opening(path, *args, **kwargs):
			self.assertNotIn(path, forbidden)
			stream = original(path, *args, **kwargs)
			if path in header_only:
				self.assertEqual(kwargs.get("buffering"), 0)
				proxy = mock.MagicMock(wraps=stream)
				proxy.__enter__.return_value = proxy
				proxy.__exit__.side_effect = stream.__exit__
				proxy.read.side_effect = AssertionError("제외 본문 읽기")
				return proxy
			return stream
		return mock.patch.object(Path, "open", opening)

	def test_selection_only_personal_and_report_never_writes_vault(self):
		for kind in ("study", "decision", "troubleshoot", "meeting"):
			self.note(f"Notes/{kind}.md", kind)
		self.note("Notes/big.md", "project", BODY + "가" * (5000 - len(BODY)))
		self.note("Notes/small.md", "project", BODY + "가" * (4999 - len(BODY)))
		for kind in ("moc", "daily", "career", "blog", "reference"):
			self.note(f"Notes/{kind}.md", kind)
		for index, heading in enumerate(r3_select.HEADINGS):
			self.note(f"Notes/heading{index}.md", body=f"## {heading}\n{BODY}")
		self.note("Notes/marked.md", body=r3_select.MARKER + "\n" + BODY)
		company = self.note("Notes/company.md", sensitivity="company")
		company.write_bytes(company.read_bytes() + b"\xff")
		missing = self.note("Notes/missing.md", sensitivity=None)
		self.write("Notes/no-header.md", BODY)
		forbidden = [self.note(name) for name in ("🔐 private.md", "🔐 folder/p.md", ".hidden/p.md",
			"🏢 Company/p.md", "Notes/.hidden.md")]
		link = self.vault / "Notes/link.md"
		link.symlink_to(company)
		before = self.snapshot()
		with self.guard(forbidden, (company, missing)):
			notes = self.preview()
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(sum(note["status"] == "pending" for note in notes.values()), 5)
		counts = self.plan()["skipped"]
		for reason, count in (("회사 노트", 1), ("personal 분류 없음", 1), ("보호 경로", 2),
			("숨김 경로", 2), ("회사 폴더", 1), ("기존 5-2 제목", 4), ("이미 R3 표식", 1), ("제외 종류", 5)):
			self.assertEqual(counts[reason], count)
		text = (self.base / "r3-report.md").read_text(encoding="utf-8")
		for value in ("대상 5개", "| decision | 1 | 1 |", "Notes/study.md", SUMMARY["lines"][0], "> - **무엇을 했나**"):
			self.assertIn(value, text)

	def test_insert_remove_exact_original_bytes_and_preserve_times_through_commit(self):
		originals = {}
		for index in range(8):
			nl = "\r\n" if index & 1 else "\n"
			body = BODY + "\r\n혼합\n끝" + (nl if index & 4 else "")
			path = self.note(f"Study/p{index}.md", body=body, nl=nl, bom=frontmatter.BOM if index & 2 else b"")
			originals[path] = path.read_bytes()
		self.init_git("Study")
		notes = self.preview()
		old = 1_700_000_000_000_000_000
		for path in originals:
			os.utime(path, ns=(old, old + 7))
		self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
		for path, before in originals.items():
			self.assertEqual((path.stat().st_atime_ns, path.stat().st_mtime_ns), (old, old + 7))
			after = path.read_bytes()
			note = notes[path.relative_to(self.vault).as_posix()]
			addition = r3_text.block(before, note["kind"], note["proposal"])
			offset = len(before) - len(frontmatter.split(before).body)
			self.assertEqual(after[:offset] + after[offset + len(addition):], before)
			self.assertEqual(after.count(r3_select.MARKER.encode()), 1)
		with mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("r3-apply"), 0)
			writing.assert_not_called()
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)
		self.preview()
		self.assertEqual(self.plan()["skipped"]["이미 R3 표식"], 8)

	def test_invalid_generated_number_is_skipped_and_cached(self):
		self.note()
		bad = {"lines": [*SUMMARY["lines"][:2], "실행 99개를 확인했다."]}
		with mock.patch.object(summarize, "call", return_value=json.dumps(bad)) as call:
			self.assertEqual(self.preview()["Study/p01.md"]["reason"], "검증 실패")
			self.assertEqual(self.preview()["Study/p01.md"]["reason"], "검증 실패")
			self.assertEqual(call.call_count, 1)
		self.assertEqual(self.plan()["skipped"]["검증 실패"], 1)

	def test_validation_drops_only_unsupported_outline_items_and_rejects_new_names(self):
		body = "실행 12개 alpha https://xyz.test/a 경로를 확인했다. " + BODY
		for sentence in ("실행 99개를 확인했다.", "bravo를 확인했다.", "https://xyz.test/b 확인", "가" * 121, "가\n나"):
			with self.subTest(sentence=sentence):
				value = {"done": SUMMARY["lines"][0], "todo": sentence}
				self.assertEqual(r3_text.validate("outline", value, body, []), {"done": SUMMARY["lines"][0]})
		good = {"done": "실행 12개 alpha https://xyz.test/a 경로를 확인했다."}
		self.assertEqual(r3_text.validate("outline", good, body, []), good)
		self.assertIsNone(r3_text.validate("outline", {**good, "todo": "가짜회사를 확인했다."}, body, ["가짜회사"]))
		self.assertIsNone(r3_text.validate("study", {"lines": SUMMARY["lines"][:2]}, body, []))
		self.assertIsNone(r3_text.validate("outline", {}, body, []))

	def test_routing_limits_only_allowed_ids_in_order_and_cache_reuse(self):
		self.plans((1, 1, 1))
		for index in range(4):
			self.note(f"Study/p{index}.md", body=BODY + str(index))
		self.preview()
		self.assertEqual([entry[0] for entry in self.calls], list(r3_text.ORDER))
		self.assertEqual(self.plan()["skipped"]["한도"], 1)
		self.preview()
		self.assertEqual(len(self.calls), 3)
		ledger = Ledger(self.hub / "ledger.db")
		try:
			self.assertEqual(ledger.conn.execute("select sum(n) from calls").fetchone()[0], 3)
		finally:
			ledger.close()
		self.assertTrue(all(not entry[2].exists() for entry in self.calls))

	def test_same_body_cache_is_shared_between_notes_and_prompt_is_clipped(self):
		body = BODY + "가" * 13000 + "끝부분확인"
		self.note("Study/p01.md", body=body)
		self.note("Study/p02.md", body=body)
		self.preview()
		self.preview()
		self.assertEqual(len(self.calls), 1)
		prompt = self.calls[0][1]
		self.assertNotIn("끝부분확인", prompt)
		self.assertEqual(len(prompt.split("--- 노트 시작 ---\n")[1].split("\n--- 노트 끝 ---")[0]), 12000)

	def test_failure_moves_to_next_plan_on_next_note_without_retrying_a_note(self):
		for index in range(3):
			self.note(f"Study/p{index}.md", body=BODY + str(index))
		def failing(plan, prompt, *, cwd):
			result = self.fake(plan, prompt, cwd=cwd)
			if plan.id != "codex-personal":
				raise summarize.SummarizeError("합성 실패")
			return result
		with mock.patch.object(summarize, "call", side_effect=failing):
			self.preview()
		self.assertEqual([entry[0] for entry in self.calls], list(r3_text.ORDER))
		self.assertEqual(self.plan()["skipped"]["AI 호출 실패"], 2)

	def test_created_file_in_empty_ai_cwd_is_rejected(self):
		self.note()
		def writing(plan, prompt, *, cwd):
			result = self.fake(plan, prompt, cwd=cwd)
			(cwd / "unexpected").write_text("합성")
			return result
		with mock.patch.object(summarize, "call", side_effect=writing):
			self.assertEqual(self.preview()["Study/p01.md"]["reason"], "AI 호출 실패")
		self.assertFalse(self.calls[0][2].exists())

	def test_ambiguous_headers_and_non_personal_bodies_are_not_loaded(self):
		paths = [
			self.write("Study/duplicate.md", "---\ntype: study\nsensitivity: personal\nsensitivity: company\n---\n" + BODY),
			self.write("Study/quoted.md", "---\ntype: study\nsensitivity: 'company' # 합성\n---\n" + BODY),
			self.write("Study/alias.md", "---\ntype: study\nsensitivity: *xyz\n---\n" + BODY),
		]
		with self.guard(header_only=paths):
			self.preview()
		self.assertEqual(len(self.calls), 0)
		self.assertTrue(all(note["status"] == "skipped" for note in self.notes().values()))

	def test_malformed_plan_proposal_refuses_before_writes(self):
		self.note()
		self.init_git("Study")
		self.preview()
		original = self.plan()
		for proposal in ({}, {"lines": "합성"}, {"lines": [1, 2, 3]}, {"done": "합성"}):
			plan = json.loads(json.dumps(original))
			plan["notes"][0]["proposal"] = proposal
			self.settings("r3-plan.json", json.dumps(plan))
			with mock.patch.object(vault_io, "atomic_write") as writing:
				self.assertEqual(self.command("r3-apply"), 2)
				writing.assert_not_called()

	def test_changed_body_marker_and_company_reclassification_skip_without_writes(self):
		changed = self.note("Study/changed.md")
		marked = self.note("Study/marked.md")
		company = self.note("Study/company.md")
		self.note("Study/valid.md")
		self.init_git("Study")
		self.preview()
		changed.write_bytes(changed.read_bytes() + "사용자 변경".encode())
		marked.write_bytes(marked.read_bytes() + r3_select.MARKER.encode())
		company.write_bytes(company.read_bytes().replace(b"personal", b"company"))
		with self.guard(header_only=(company,)):
			self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
		notes = self.notes()
		self.assertEqual(notes["Study/changed.md"]["reason"], "보고서 이후 바뀜")
		self.assertEqual(notes["Study/marked.md"]["reason"], "이미 R3 표식")
		self.assertEqual(notes["Study/company.md"]["reason"], "회사 노트")
		self.assertEqual(notes["Study/valid.md"]["status"], "applied")

	def test_ten_note_batches_limit_resume_and_no_push(self):
		for index in range(23):
			self.note(f"Study/p{index:02d}.md")
		self.init_git("Study")
		self.preview()
		original_git = sync.git
		def recording(vault, *args):
			self.assertNotIn("push", args)
			return original_git(vault, *args)
		with mock.patch.object(sync, "git", side_effect=recording):
			self.assertEqual(self.command("r3-apply", "--limit", "12"), 0, self.stderr.getvalue())
			self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 3)
			self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 5)
		messages = self.git("log", "-4", "--format=%s").decode().splitlines()
		self.assertEqual(messages, [f"볼트 R3: 본문 요약·정리 {n}개 덧붙임" for n in (1, 10, 2, 10)])
		self.assertEqual(self.git("status", "--porcelain"), b"")
		result = self.run_module("r3-apply")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.command("r3-apply", "--limit", "0"), 2)

	def test_commit_failure_retries_without_rewriting_and_readback_failure_restores(self):
		path = self.note()
		self.init_git("Study")
		self.preview()
		before = path.read_bytes()
		atomic = vault_io.atomic_write
		count = 0
		def corrupt_once(path, text):
			nonlocal count
			count += 1
			atomic(path, text + "변경" if count == 1 else text)
		with mock.patch.object(vault_io, "atomic_write", side_effect=corrupt_once):
			self.assertEqual(self.command("r3-apply"), 1)
		self.assertEqual(path.read_bytes(), before)
		hook = self.vault / ".git/hooks/pre-commit"
		hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
		hook.chmod(0o700)
		self.assertEqual(self.command("r3-apply"), 1)
		self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")
		self.assertIsNone(self.notes()["Study/p01.md"]["commit"])
		hook.unlink()
		with mock.patch.object(vault_io, "atomic_write") as writing:
			self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
			writing.assert_not_called()
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)

	def test_busy_git_and_collector_note_lock_refuse_writes(self):
		path = self.note()
		self.init_git("Study")
		self.preview()
		before = path.read_bytes()
		with locked("vault:" + str(path.absolute())):
			self.assertEqual(self.command("r3-apply"), 1)
		self.assertEqual(path.read_bytes(), before)
		with locked("vaultsync", exact=self.hub / "run/vaultsync.lock"):
			self.assertEqual(self.command("r3-apply"), 2)
		index_lock = self.vault / ".git/index.lock"
		index_lock.touch()
		try:
			with mock.patch.object(vault_io, "atomic_write") as writing:
				self.assertEqual(self.command("r3-apply"), 1)
				writing.assert_not_called()
		finally:
			index_lock.unlink()

	def test_edit_immediately_after_commit_keeps_new_body_and_timestamp(self):
		from vaultreorg import r3_apply
		path = self.note()
		self.init_git("Study")
		self.preview()
		commit = r3_apply.commit
		new_time = 1_800_000_000_000_000_000
		def editing(vault, notes):
			result = commit(vault, notes)
			path.write_bytes(path.read_bytes() + "사용자 변경".encode())
			os.utime(path, ns=(new_time, new_time))
			return result
		with mock.patch.object(r3_apply, "commit", side_effect=editing):
			self.assertEqual(self.command("r3-apply"), 1)
		self.assertEqual(path.stat().st_mtime_ns, new_time)
		self.assertTrue(path.read_bytes().endswith("사용자 변경".encode()))
		self.assertIn("시각 복원 보류", self.plan()["commit_error"])

	def test_unsafe_plan_paths_symlink_and_tampered_hash_refuse(self):
		path = self.note()
		self.init_git("Study")
		self.preview()
		original = self.plan()
		for value in ("../p01.md", "🔐 private.md", ".hidden/p01.md", "🏢 Company/p01.md"):
			plan = json.loads(json.dumps(original))
			plan["notes"][0]["path"] = value
			self.settings("r3-plan.json", json.dumps(plan))
			self.assertEqual(self.command("r3-apply"), 2)
		for key in ("after", "body_hash"):
			plan = json.loads(json.dumps(original))
			plan["notes"][0][key] = "0" * 64
			self.settings("r3-plan.json", json.dumps(plan))
			with mock.patch.object(vault_io, "atomic_write") as writing:
				self.assertIn(self.command("r3-apply"), (0, 1))
				writing.assert_not_called()
		self.settings("r3-plan.json", json.dumps(original))
		outside = self.write("outside.md", "합성")
		path.unlink()
		path.symlink_to(outside)
		with self.guard(forbidden=(path, outside)):
			self.assertEqual(self.command("r3-apply"), 1)

	def test_nfd_git_path_and_dictionary_path_rules_are_reused(self):
		name = unicodedata.normalize("NFD", "Study/한글.md")
		self.note(name)
		self.note("Study/가짜회사.md")
		self.git("init", "-q", "-b", "main")
		self.git("config", "commit.gpgsign", "false")
		self.git("-c", "core.precomposeunicode=false", "add", "--", name)
		self.git("commit", "-q", "-m", "합성 시작")
		self.preview()
		self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)
		self.assertNotIn("가짜회사", self.git("ls-files").decode())
		self.assertTrue(self.notes()[name]["commit"])
		# 경로 규칙으로 커밋에서 빠진 노트는 로컬 반영으로 끝나고 다음 실행이 다시 잡지 않는다
		self.assertTrue(self.notes()["Study/가짜회사.md"]["local_only"])
		self.assertEqual(self.command("r3-apply"), 0, self.stderr.getvalue())
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 2)

	def test_fenced_or_wrapped_json_is_accepted_and_non_json_is_not_cached(self):
		self.note()
		fenced = "설명입니다.\n```json\n" + json.dumps(SUMMARY, ensure_ascii=False) + "\n```\n끝"
		with mock.patch.object(summarize, "call", return_value=fenced):
			self.assertEqual(self.preview()["Study/p01.md"]["status"], "pending")
		self.note("Study/p02.md", body="다른 본문이다. " * 80)
		with mock.patch.object(summarize, "call", return_value="JSON이 아닌 답") as call:
			self.preview()
			self.preview()
			self.assertEqual(call.call_count, 2)
		self.assertEqual(self.notes()["Study/p02.md"]["reason"], "검증 실패")

	def test_short_study_is_skipped_without_ai_call(self):
		self.note(body="짧은 본문이다.")
		with mock.patch.object(summarize, "call") as call:
			note = self.preview()["Study/p01.md"]
		call.assert_not_called()
		self.assertEqual((note["status"], note["reason"]), ("skipped", "짧은 study"))

	def test_agy_argv_model_and_legacy_default_and_explicit_cwd(self):
		plan = load_plans().plans["agy-argon"]
		self.assertEqual(summarize._argv(plan, "합성"), (["agy", "-p", "합성", "--model", "xyz"], False))
		from dataclasses import replace
		self.assertEqual(summarize._argv(replace(plan, model=None), "합성"), (["agy", "-p", "합성"], False))
		response = subprocess.CompletedProcess([], 0, "합성 응답", "")
		with mock.patch.object(summarize.subprocess, "run", return_value=response) as run:
			metadata = {}
			self.assertEqual(self.real_call(plan, "합성", cwd=self.root, metadata=metadata), "합성 응답")
			self.assertEqual(run.call_args.args[0].count("--model"), 1)
			self.assertEqual(run.call_args.kwargs["cwd"], self.root)
			self.assertEqual(metadata["model"], "xyz")
			self.real_call(replace(plan, model=None), "합성")
			self.assertEqual(run.call_args.args[0], ["agy", "-p", "합성"])
			self.assertEqual(run.call_args.kwargs["cwd"], self.hub / "run")
