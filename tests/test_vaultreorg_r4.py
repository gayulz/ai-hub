"""R4의 입력 제외·가짜 벡터/AI·덧붙이기·Git 복구를 합성 볼트로만 시험한다."""

import io
import json
import math
import os
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path
from unittest import mock

from collector import summarize, vault as vault_io
from collector.backfill_common import locked
from collector.config import ConfigError, load_plans
from collector.ledger import Ledger
from vaultreorg import frontmatter, r4, r4_judge, r4_links, r4_vectors
from vaultsync import sync

if __package__:
	from . import test_vaultreorg_support as support
	sys.modules.setdefault("test_vaultreorg_support", support)
else:
	import test_vaultreorg_support as support

BODY = "# 합성 주제\n기록의 근거를 확인했다.\n관련 내용을 정리했다."
JUDGMENT = {"relation": "related", "action": "link", "keep": "", "reason": "같은 주제를 다룬다."}


class R4Test(support.ReorgCase):
	def setUp(self):
		super().setUp()
		self.dictionary([])
		self.plans()
		self.calls, self.embedded = [], []
		self.real_embed = r4_vectors.embed
		for patch in (mock.patch.object(summarize, "call", side_effect=self.fake),
			mock.patch.object(r4_vectors, "embed", side_effect=self.embedding),
			mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("실제 네트워크 금지"))):
			patch.start()
			self.addCleanup(patch.stop)

	def plans(self, limits=(None, None, None), argon=True):
		lines = []
		ids = [("agy-opus", "agy"), ("agy-sonnet", "agy"), ("codex-personal", "codex"),
			("agy-personal", "agy"), ("claude-personal", "claude")]
		if argon:
			ids.append(("agy-argon", "agy"))
		for index, (pid, tool) in enumerate(ids):
			lines.extend(["[[plans]]", f"id='{pid}'", f"tool='{tool}'", "owner='personal'", "model='xyz'", "allow=['personal']"])
			if index < 3 and limits[index] is not None:
				lines.append(f"daily_limit_calls={limits[index]}")
		lines.extend(["[routing]", "personal=['claude-personal','agy-personal','codex-personal']"])
		(self.hub / "plans.toml").write_text("\n".join(lines), encoding="utf-8")

	def set_judges(self, value):
		path = self.hub / "plans.toml"
		base = path.read_text(encoding="utf-8").split("\n[vaultreorg]\n", 1)[0]
		path.write_text(base + "\n[vaultreorg]\n" + value + "\n", encoding="utf-8")

	def note(self, name="Study/p01.md", kind="study", body=BODY, sensitivity="personal", nl="\n", bom=b""):
		classification = f"sensitivity: {sensitivity}{nl}" if sensitivity is not None else ""
		return self.write(name, bom + f"---{nl}type: {kind}{nl}{classification}---{nl}".encode() + body.encode())

	def pair(self):
		return [self.note("Study/p01.md"), self.note("Study/p02.md")]

	def embedding(self, text):
		self.embedded.append(text)
		return [1.0, 0.0]

	def fake(self, plan, prompt, *, cwd):
		self.assertEqual(list(cwd.iterdir()), [])
		self.assertFalse(cwd.is_relative_to(self.vault))
		self.calls.append((plan.id, prompt, cwd))
		return json.dumps(JUDGMENT, ensure_ascii=False)

	def plan(self):
		return json.loads((self.base / "r4-plan.json").read_text(encoding="utf-8"))

	def all_notes(self):
		return {note["path"]: note for group in self.plan()["groups"] for note in group["notes"]}

	def preview(self, *args):
		self.assertEqual(self.command("r4-report", *args), 0, self.stderr.getvalue())
		return self.plan()

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

	def test_selection_guards_headers_and_report_does_not_write_vault(self):
		for kind in sorted(r4_vectors.TYPES):
			self.note(f"Notes/{kind}.md", kind)
		excluded = [self.note(f"Notes/{kind}.md", kind, body="본문" + "\ufffd") for kind in ("daily", "moc", "blog", "meeting")]
		excluded += [self.note("Notes/company.md", sensitivity="company"), self.note("Notes/missing.md", sensitivity=None)]
		for path in excluded:
			path.write_bytes(path.read_bytes() + b"\xff")
		forbidden = [self.note(name) for name in ("🔐 private.md", "🔐 folder/p.md", ".hidden/p.md", "🏢 Company/p.md", "Notes/.hidden.md")]
		link = self.vault / "Notes/link.md"
		link.symlink_to(excluded[0])
		before = self.snapshot()
		with self.guard(forbidden, excluded):
			plan = self.preview()
		self.assertEqual(self.snapshot(), before)
		self.assertEqual(plan["target_count"], 5)
		self.assertEqual(len(plan["groups"][0]["notes"]), 5)
		self.assertEqual(sum(row["pairs"] for row in plan["distribution"]), 10)
		self.assertEqual(len(plan["distribution"]), 40)
		self.assertEqual(plan["skipped"]["제외 종류"], 4)
		self.assertEqual(plan["skipped"]["회사 노트"], 1)
		text = (self.base / "r4-report.md").read_text(encoding="utf-8")
		for value in ("대상 5개", "0.95 ~ 1.00 | 10", "Notes/study.md", "related", "link", "같은 주제", "병합 제안"):
			self.assertIn(value, text)

	def test_ambiguous_headers_are_rejected_before_body_read(self):
		paths = [self.write("Study/p01.md", "---\ntype: study\nsensitivity: personal\nsensitivity: company\n---\n본문"),
			self.write("Study/p02.md", "---\ntype: study\nsensitivity: *xyz\n---\n본문"),
			self.write("Study/p03.md", "---\ntype: daily\nsensitivity: personal\n---\n본문")]
		with self.guard(header_only=paths):
			self.assertEqual(self.preview()["target_count"], 0)
		self.assertEqual(self.calls, [])
		self.assertEqual(self.embedded, [])

	def test_threshold_size_strongest_edges_and_distribution(self):
		notes = [{"path": f"p{i}.md"} for i in range(8)]
		vectors = [[math.cos(i * .01), math.sin(i * .01)] for i in range(8)]
		groups, bins = r4_vectors.group(notes, vectors, .8)
		self.assertTrue(all(2 <= len(group["notes"]) <= 6 for group in groups))
		self.assertEqual(sum(row["pairs"] for row in bins), 28)
		self.assertEqual(r4_vectors.group(notes, vectors, 1.0)[0], [])
		groups, _ = r4_vectors.group(notes[:3], [[1, 0], [.8, .6], [0, 1]], .8)
		self.assertEqual({n["path"] for n in groups[0]["notes"]}, {"p0.md", "p1.md"})
		groups, _ = r4_vectors.group(notes[:3], [[1, 0], [.8, .6], [0, 1]], .81)
		self.assertEqual(groups, [])

	def test_max_groups_limits_ai_and_orders_by_similarity(self):
		for i in range(14):
			self.note(f"Study/p{i:02d}.md")
		plan = self.preview("--max-groups", "1")
		self.assertGreater(len(plan["groups"]), 1)
		self.assertEqual(len(self.calls), 1)
		self.assertEqual(sum(g["status"] == "judged" for g in plan["groups"]), 1)
		self.assertTrue(all(len(g["notes"]) <= 6 for g in plan["groups"]))
		values = [g["similarity"] for g in plan["groups"]]
		self.assertEqual(values, sorted(values, reverse=True))

	def test_response_validation_and_failed_group_is_not_applied(self):
		self.pair()
		for changes in ({"relation": "xyz"}, {"action": "xyz"}, {"keep": "outside.md"},
			{"action": "merge", "keep": ""}, {"relation": "distinct", "action": "link"},
			{"reason": "가" * 81}, {"reason": "English"}, {"reason": "이유\n추가"}, {"keep": []}, {"extra": "xyz"}):
			with self.subTest(changes=changes):
				self.assertIsNone(r4_judge.validate({**JUDGMENT, **changes}, [{"path": "Study/p01.md"}]))
		with mock.patch.object(summarize, "call", return_value=json.dumps({**JUDGMENT, "keep": "outside.md"})):
			plan = self.preview()
		self.assertEqual(plan["groups"][0]["status"], "failed")
		self.init_git("Study")
		with mock.patch.object(vault_io, "atomic_write") as write:
			self.assertEqual(self.command("r4-apply-links"), 0)
			write.assert_not_called()

	def test_only_three_plans_order_daily_ledger_and_cache_reuse(self):
		self.plans((1, 1, 1))
		for i in range(24):
			self.note(f"Study/p{i:02d}.md")
		plan = self.preview()
		self.assertEqual([item[0] for item in self.calls], ["agy-opus", "agy-sonnet", "codex-personal"])
		self.assertEqual(sum(g["reason"] == "한도" for g in plan["groups"]), 1)
		self.preview()
		self.assertEqual(len(self.calls), 3)
		self.assertEqual(len(self.embedded), 1)
		self.assertTrue(all(not item[2].exists() for item in self.calls))
		ledger = Ledger(self.hub / "ledger.db")
		try:
			self.assertEqual(ledger.conn.execute("select sum(n) from calls").fetchone()[0], 3)
		finally:
			ledger.close()

	def test_call_failure_falls_through_to_next_plan_and_dead_plans_are_tried_once(self):
		for i in range(18):
			self.note(f"Study/p{i:02d}.md")
		def failing(plan, prompt, *, cwd):
			result = self.fake(plan, prompt, cwd=cwd)
			if plan.id != "codex-personal":
				raise summarize.SummarizeError("합성 실패")
			return result
		with mock.patch.object(summarize, "call", side_effect=failing):
			plan = self.preview()
		used = [item[0] for item in self.calls]
		self.assertEqual(used[:3], ["agy-opus", "agy-sonnet", "codex-personal"])
		self.assertEqual((used.count("agy-opus"), used.count("agy-sonnet")), (1, 1))
		self.assertEqual(used.count("codex-personal"), len(plan["groups"]))
		self.assertEqual(sum(g["status"] == "failed" for g in plan["groups"]), 0)
		plans = load_plans()
		with self.assertRaises(ConfigError):
			r4_judge.routing(replace(plans, plans={"agy-opus": replace(plans.plans["agy-opus"], tool="claude")}))

	def test_judge_order_is_read_again_for_each_report(self):
		_, second = self.pair()
		last = "codex-personal"
		def failing(plan, prompt, *, cwd):
			result = self.fake(plan, prompt, cwd=cwd)
			if plan.id != last:
				raise summarize.SummarizeError("합성 실패")
			return result
		with mock.patch.object(summarize, "call", side_effect=failing):
			for index, (setting, expected) in enumerate((
				("['agy-argon','agy-sonnet','codex-personal']", ["agy-argon", "agy-sonnet", "codex-personal"]),
				("['codex-personal','agy-argon','agy-sonnet']", ["codex-personal", "agy-argon", "agy-sonnet"]))):
				last = expected[-1]
				self.set_judges("r4_judges=" + setting)
				second.write_bytes(second.read_bytes() + f"\n합성 변경 {index}".encode())
				self.calls.clear()
				self.assertEqual(self.preview()["groups"][0]["plan"], last)
				self.assertEqual([item[0] for item in self.calls], expected)

	def test_missing_table_or_key_uses_default_and_skips_unregistered(self):
		_, second = self.pair()
		def failing(plan, prompt, *, cwd):
			result = self.fake(plan, prompt, cwd=cwd)
			if plan.id != "codex-personal":
				raise summarize.SummarizeError("합성 실패")
			return result
		with mock.patch.object(summarize, "call", side_effect=failing):
			for index, table in enumerate((False, True)):
				self.plans()
				if table:
					self.set_judges("")
				second.write_bytes(second.read_bytes() + f"\n합성 기본 {index}".encode())
				self.calls.clear()
				self.preview()
				self.assertEqual([item[0] for item in self.calls], ["agy-opus", "agy-sonnet", "codex-personal"])
		path = self.hub / "plans.toml"
		remaining = path.read_text(encoding="utf-8").split("[[plans]]", 2)[2]
		path.write_text("[[plans]]" + remaining, encoding="utf-8")
		plans = load_plans()
		routed = r4_judge.routing(plans)
		self.assertEqual(routed.routing["personal"], ("agy-sonnet", "codex-personal"))
		ledger = mock.Mock()
		ledger.reserve_call.return_value = True
		notes = [{"path": "p.md", "before": "a", "input_hash": "b", "title": "합성", "body": "합성"}]
		self.calls.clear()
		with mock.patch.object(summarize, "call", side_effect=failing):
			entry, reason = r4_judge.judge(notes, routed, ledger, "2026-01-01", {}, set())
		self.assertEqual((entry["plan"], reason), ("codex-personal", ""))
		self.assertEqual([item[0] for item in self.calls], ["agy-sonnet", "codex-personal"])

	def test_invalid_judge_settings_and_plan_permissions_fail_before_call(self):
		for value in ("'x'", "1", "false", "[]", "['agy-opus','agy-opus']", "[1]", "[true]",
			"['']", "['agy-personal']", "['unknown']", "{x=1}", "["):
			with self.subTest(value=value):
				self.plans()
				self.set_judges("r4_judges=" + value)
				with self.assertRaises(ConfigError):
					r4.report()
				self.assertEqual(self.calls, [])
		for value in ("'x'", "[]", "false"):
			self.plans()
			path = self.hub / "plans.toml"
			path.write_text("vaultreorg=" + value + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
			with self.subTest(table=value), self.assertRaises(ConfigError):
				r4.report()
		self.plans(argon=False)
		self.set_judges("r4_judges=['agy-argon']")
		with self.assertRaises(ConfigError):
			r4.report()
		self.plans()
		for pid in ("agy-opus", "agy-sonnet", "agy-argon", "codex-personal"):
			self.set_judges("r4_judges=['" + pid + "']")
			plans = load_plans()
			for changes in ({"id": "other"}, {"owner": "company"}, {"tool": "claude"}, {"allow": ("study",)}):
				with self.subTest(pid=pid, changes=changes), self.assertRaises(ConfigError):
					r4_judge.routing(replace(plans, plans={pid: replace(plans.plans[pid], **changes)}))
		self.plans()
		plans = load_plans()
		with self.assertRaises(ConfigError):
			r4_judge.routing(replace(plans, plans={"agy-opus": replace(plans.plans["agy-opus"], allow=())}))
		self.assertEqual(self.calls, [])

	def test_legacy_cache_is_reused_independently_of_current_order(self):
		notes = [{"path": "Study/p01.md", "before": "a", "input_hash": "b", "title": "합성", "body": "합성"}]
		key = "90753261405706fa2803661e7bc2c8d1af5a510ee497eafb064e181469835b52"
		cache_path = self.base / "r4-cache.json"
		for pid in ("agy-opus", "agy-sonnet", "codex-personal", "agy-argon"):
			with self.subTest(pid=pid):
				current = "agy-opus" if pid == "codex-personal" else "codex-personal"
				self.set_judges("r4_judges=['" + current + "']")
				entry = {"judgment": JUDGMENT, "plan": pid}
				cache_path.write_text(json.dumps({"version": 4, "entries": {key: entry}}, ensure_ascii=False) + "\n", encoding="utf-8")
				before = cache_path.read_bytes()
				cache, ledger = r4_vectors.load_cache("r4-cache.json"), mock.Mock()
				result, reason = r4_judge.judge(notes, r4_judge.routing(load_plans()), ledger, "2026-01-01", cache, set())
				self.assertEqual((result, reason, cache[key]), (entry, "", entry))
				r4_vectors.save_cache("r4-cache.json", cache)
				self.assertEqual(cache_path.read_bytes(), before)
				ledger.reserve_call.assert_not_called()
		self.assertEqual(self.calls, [])

	def test_prompt_clip_body_cache_and_title_invalidation(self):
		body = BODY + "가" * 3100 + "보내지않을끝부분"
		self.note("Study/p01.md", body=body)
		second = self.note("Study/p02.md", body=body)
		self.preview()
		self.preview()
		self.assertEqual((len(self.calls), len(self.embedded)), (1, 1))
		self.assertNotIn("보내지않을끝부분", self.calls[0][1])
		self.assertNotIn("보내지않을끝부분", self.embedded[0])
		data = json.loads(self.calls[0][1].split("--- 노트 자료 JSON ---\n")[1].split("\n--- 자료 끝 ---")[0])
		self.assertEqual(len(data[0]["body"]), 3000)
		self.assertEqual(data[0]["title"], "합성 주제")
		second.write_bytes(second.read_bytes().replace("합성 주제".encode(), "다른 제목".encode()))
		self.preview()
		self.assertEqual((len(self.calls), len(self.embedded)), (2, 2))

	def test_embedding_is_loopback_proxy_free_no_redirect_and_validates_vectors(self):
		self.pair()
		opener = mock.MagicMock()
		opener.open.return_value.__enter__.return_value = io.StringIO('{"embeddings":[[1,0]]}')
		with mock.patch.object(r4_vectors, "build_opener", return_value=opener) as build:
			self.assertEqual(self.real_embed("합성"), [1, 0])
			self.assertEqual(opener.open.call_args.args[0].full_url, "http://127.0.0.1:11434/api/embed")
			self.assertEqual(build.call_args.args[0].proxies, {})
			with self.assertRaises(ConfigError):
				build.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://xyz.test")
		for vector in ([], [0, 0], [True, 1], [float("nan")], [float("inf")], "xyz"):
			with self.subTest(vector=vector), self.assertRaises(ConfigError):
				r4_vectors.unit(vector)

	def test_insert_remove_preserves_bytes_bom_crlf_existing_sections_and_aliases(self):
		for i in range(8):
			nl = "\r\n" if i & 1 else "\n"
			body = BODY + "\r\n혼합\n끝" + (nl if i & 4 else "")
			path = self.note(f"Study/p{i}.md", body=body, nl=nl, bom=frontmatter.BOM if i & 2 else b"")
			before = path.read_bytes()
			offset, addition = r4_links.insert(before, ["other"])
			after = before[:offset] + addition + before[offset:]
			self.assertEqual(after[:offset] + after[offset + len(addition):], before)
			self.assertEqual(r4_links.insert(after, ["other"])[1], b"")
		before = (BODY + "\n```md\n## 관련 노트\n```\n\n## 관련 노트\n- [[p02|표시]]\n### 세부\n원문\n## 다음 절\n유지\n").encode()
		offset, addition = r4_links.insert(before, ["p02", "p03"])
		after = before[:offset] + addition + before[offset:]
		self.assertEqual(after.count("## 관련 노트".encode()), 2)
		self.assertEqual(after.count(b"[[p02"), 1)
		self.assertLess(after.index(b"[[p03]]"), after.index("## 다음 절".encode()))
		self.assertEqual(after[:offset] + after[offset + len(addition):], before)

	def test_apply_mutual_links_times_duplicate_names_and_nfd(self):
		name = unicodedata.normalize("NFD", "Study/한글.md")
		paths = [self.note(name), self.note("Study/p02.md"), self.note("Other/p02.md")]
		self.init_git("Study", "Other")
		self.preview()
		before = {path: path.read_bytes() for path in paths}
		old = 1_700_000_000_000_000_000
		for path in paths:
			os.utime(path, ns=(old, old + 7))
		self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())
		for path in paths:
			self.assertEqual((path.stat().st_atime_ns, path.stat().st_mtime_ns), (old, old + 7))
			note = self.all_notes()[path.relative_to(self.vault).as_posix()]
			after, addition = path.read_bytes(), note["addition"].encode()
			self.assertEqual(after[:note["offset"]] + after[note["offset"] + len(addition):], before[path])
		self.assertIn("[[Study/p02]]", paths[0].read_text())
		self.assertIn("[[Other/p02]]", paths[0].read_text())
		self.assertIn("[[한글]]", paths[1].read_text())
		with mock.patch.object(vault_io, "atomic_write") as write:
			self.assertEqual(self.command("r4-apply-links"), 0)
			write.assert_not_called()
		self.assertEqual(self.git("status", "--porcelain"), b"")

	def test_apply_existing_section_and_full_path_links_are_not_duplicated(self):
		first = self.note("Study/p01.md", body=BODY + "\n## 관련 노트\n- [[Study/p02|표시]]\n## 다음 절\n원문")
		second = self.note("Study/p02.md")
		self.init_git("Study")
		self.preview()
		before = first.read_bytes()
		self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())
		self.assertEqual(first.read_bytes(), before)
		self.assertIn("[[p01]]", second.read_text())
		self.assertEqual(self.all_notes()["Study/p01.md"]["status"], "unchanged")

	def test_ten_note_commits_limit_resume_and_no_push(self):
		for i in range(23):
			self.note(f"Study/p{i:02d}.md")
		self.init_git("Study")
		self.preview()
		git = sync.git
		def recording(vault, *args):
			self.assertNotIn("push", args)
			return git(vault, *args)
		with mock.patch.object(sync, "git", side_effect=recording):
			self.assertEqual(self.command("r4-apply-links", "--limit", "12"), 0, self.stderr.getvalue())
			self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 3)
			self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())
		self.assertEqual(int(self.git("rev-list", "--count", "HEAD")), 5)
		self.assertEqual(self.git("log", "-4", "--format=%s").decode().splitlines(),
			[f"볼트 R4: 관련 노트 링크 {n}개 덧붙임" for n in (1, 10, 2, 10)])
		self.assertEqual(self.run_module("r4-apply-links").returncode, 0)

	def test_link_unsafe_file_names_are_excluded_and_plan_still_loads(self):
		self.pair()
		self.note("Study/[대괄호] p03.md")
		self.init_git("Study")
		plan = self.preview()
		self.assertNotIn("Study/[대괄호] p03.md", self.all_notes())
		self.assertEqual(plan["skipped"]["링크에 쓸 수 없는 이름"], 1)
		self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())

	def test_merge_duplicate_and_distinct_are_report_only(self):
		self.pair()
		self.init_git("Study")
		for relation, action, keep in (("duplicate", "merge", "Study/p01.md"), ("duplicate", "link", ""), ("distinct", "none", "")):
			cache = self.base / "r4-cache.json"
			cache.unlink(missing_ok=True)
			with mock.patch.object(summarize, "call", return_value=json.dumps({**JUDGMENT, "relation": relation, "action": action, "keep": keep})):
				self.preview()
			with mock.patch.object(vault_io, "atomic_write") as write:
				self.assertEqual(self.command("r4-apply-links"), 0)
				write.assert_not_called()
			if relation == "duplicate":
				text = (self.base / "r4-report.md").read_text(encoding="utf-8").split("## 병합 제안")[1]
				self.assertIn("Study/p02.md", text)
				self.assertIn("기록의 근거", text)
				self.assertNotIn("합성 주제", text)

	def test_stale_body_reclassification_unsafe_paths_and_tampered_hash(self):
		paths = [self.note(f"Study/p{i}.md") for i in range(4)]
		self.init_git("Study")
		self.preview()
		original = self.plan()
		for value in ("../p01.md", "🔐 p.md", ".hidden/p.md", "🏢 Company/p.md", "Study/p[0].md"):
			plan = json.loads(json.dumps(original))
			plan["groups"][0]["notes"][0]["path"] = value
			self.settings("r4-plan.json", json.dumps(plan))
			with mock.patch.object(vault_io, "atomic_write") as write:
				self.assertEqual(self.command("r4-apply-links"), 2)
				write.assert_not_called()
		self.settings("r4-plan.json", json.dumps(original))
		paths[0].write_bytes(paths[0].read_bytes() + "사용자 변경".encode())
		paths[1].write_bytes(paths[1].read_bytes().replace(b"personal", b"company"))
		with self.guard(header_only=(paths[1],)):
			self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())
		self.assertEqual(self.all_notes()["Study/p0.md"]["reason"], "보고서 이후 바뀜")
		self.assertNotIn("관련 노트", paths[0].read_text())
		self.assertNotIn("[[p1]]", paths[2].read_text())

	def test_git_failure_retry_readback_restore_and_collector_lock(self):
		paths = self.pair()
		self.init_git("Study")
		self.preview()
		with locked("vault:" + str(paths[0].absolute())):
			self.assertEqual(self.command("r4-apply-links"), 1)
		self.assertTrue(all(note["status"] == "pending" for note in self.all_notes().values()))
		self.preview()
		before = paths[0].read_bytes()
		atomic, count = vault_io.atomic_write, 0
		def corrupt_once(path, text):
			nonlocal count
			count += 1
			atomic(path, text + "변경" if count == 1 else text)
		with mock.patch.object(vault_io, "atomic_write", side_effect=corrupt_once):
			self.assertEqual(self.command("r4-apply-links", "--limit", "1"), 1)
		self.assertEqual(paths[0].read_bytes(), before)
		self.preview()
		hook = self.vault / ".git/hooks/pre-commit"
		hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
		hook.chmod(0o700)
		self.assertEqual(self.command("r4-apply-links"), 1)
		self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")
		hook.unlink()
		with mock.patch.object(vault_io, "atomic_write") as write:
			self.assertEqual(self.command("r4-apply-links"), 0, self.stderr.getvalue())
			write.assert_not_called()

	def test_bad_options_and_busy_vault_refuse_writes(self):
		for args in (("--threshold", "nan"), ("--threshold", "1.1"), ("--max-groups", "0")):
			self.assertEqual(self.command("r4-report", *args), 2)
		self.pair()
		self.init_git("Study")
		self.preview()
		self.assertEqual(self.command("r4-apply-links", "--limit", "0"), 2)
		with locked("vaultsync", exact=self.hub / "run/vaultsync.lock"):
			self.assertEqual(self.command("r4-apply-links"), 2)
		index_lock = self.vault / ".git/index.lock"
		index_lock.touch()
		with mock.patch.object(vault_io, "atomic_write") as write:
			self.assertEqual(self.command("r4-apply-links"), 1)
			write.assert_not_called()
