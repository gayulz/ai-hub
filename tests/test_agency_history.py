"""실제 원천·검색 엔진 없이 합성 저장소로 로컬 히스토리 검색을 검사한다."""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agency import history, history_cli


class HistoryTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		(self.hub / "bin").mkdir(parents=True)
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.company, self.personal = self.root / "xyz", self.root / "abc"
		for repo in (self.company, self.personal):
			(repo / ".git" / "refs" / "heads").mkdir(parents=True)
			(repo / ".git" / "objects").mkdir()
			(repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
		self.exact = self.commit(self.company, "로그인 조회 개선", 4)
		self.words = self.commit(self.company, "로그인 기본 조회", 3)
		self.commit(self.company, "로그인 설정 변경", 2)
		self.commit(self.personal, "로그인 조회 개인 기록", 1)
		self.write_sources()
		self.jar = self.hub / "bin" / "ai-hub-search.jar"
		self.jar.touch()
		self.java = self.root / "jdk" / "bin" / "java"
		self.java.parent.mkdir(parents=True)
		self.java.touch()
		self.note_output = "WARN 합성 로그\n1 · 0.032 · 노트/261027-01-xyz.md · 합성 제목 > 조회 · 합성 발췌\n"
		self.java_home_error = self.java_error = None
		self.git_errors = {}
		self.fail_all_match = False
		self.real_run = subprocess.run
		patch = mock.patch.object(history.subprocess, "run", side_effect=self.run_local)
		self.run_mock = patch.start()
		self.addCleanup(patch.stop)
		for name in ("socket.socket", "socket.create_connection"):
			patch = mock.patch(name, side_effect=AssertionError("네트워크 호출 금지"))
			patch.start()
			self.addCleanup(patch.stop)

	def object(self, repo, kind, body):
		data = f"{kind} {len(body)}\0".encode() + body
		identity = hashlib.sha1(data).hexdigest()
		path = repo / ".git" / "objects" / identity[:2] / identity[2:]
		path.parent.mkdir(exist_ok=True)
		path.write_bytes(zlib.compress(data))
		return identity

	def commit(self, repo, subject, days, branch="main"):
		# 합성 Git 객체를 직접 써서 시험 준비에도 log 이외의 Git 명령을 쓰지 않는다.
		tree = self.object(repo, "tree", b"")
		ref = repo / ".git" / "refs" / "heads" / branch
		parent = "parent " + ref.read_text().strip() + "\n" if ref.exists() else ""
		when = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days)
		identity = f"xyz <xyz> {int(when.timestamp())} +0000"
		body = f"tree {tree}\n{parent}author {identity}\ncommitter {identity}\n\n{subject}\n"
		digest = self.object(repo, "commit", body.encode("utf-8"))
		ref.write_text(digest + "\n", encoding="utf-8")
		return digest[:7]

	def write_sources(self, company_name="합성 팀", personal_class="personal"):
		text = f"vault = {json.dumps(str(self.root / 'vault'))}\n"
		for path, cls, name, identity in ((self.company, "company", company_name, "xyz"),
			(self.personal, personal_class, "합성 개인", "abc")):
			text += (f"[[repo]]\npath = {json.dumps(str(path))}\nclass = {json.dumps(cls)}\n"
				f"name = {json.dumps(name)}\nid = {json.dumps(identity)}\n")
		(self.hub / "sources.toml").write_text(text, encoding="utf-8")

	def run_local(self, args, **kwargs):
		self.assertIsInstance(args, list)
		self.assertFalse(kwargs.get("shell", False))
		self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
		self.assertTrue(kwargs["check"])
		if args[0] == "/usr/libexec/java_home":
			self.assertEqual(args, ["/usr/libexec/java_home", "-v", "21"])
			if self.java_home_error:
				raise self.java_home_error
			return subprocess.CompletedProcess(args, 0, str(self.java.parent.parent) + "\n")
		if args[0] == str(self.java):
			self.assertEqual(kwargs["timeout"], 120)
			if self.java_error:
				raise self.java_error
			return subprocess.CompletedProcess(args, 0, self.note_output)
		self.assertEqual(args[0], "git", "허용되지 않은 프로그램 호출")
		self.assertIn(args[2], (str(self.company), str(self.personal)))
		self.assertEqual(args[:2], ["git", "-C"])
		self.assertEqual(args[3], "log")
		if args[2] in self.git_errors:
			raise self.git_errors[args[2]]
		if self.fail_all_match and "--all-match" in args:
			raise subprocess.TimeoutExpired(args, 30, stderr=str(self.company))
		env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
		env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
		return self.real_run(args, env=env, **kwargs)

	def git_calls(self):
		return [call.args[0] for call in self.run_mock.call_args_list if call.args[0][0] == "git"]

	def cli(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = history_cli.main(list(args))
		return code, out.getvalue(), err.getvalue()

	def test_company_only_phrase_and_all_words_merge_without_duplicates(self):
		result = history.search("로그인 조회")
		self.assertEqual([row["hash"] for row in result["commits"]], [self.words, self.exact])
		self.assertEqual({row["repo"] for row in result["commits"]}, {"합성 팀"})
		self.assertEqual([row["date"] for row in result["commits"]],
			sorted((row["date"] for row in result["commits"]), reverse=True))
		calls = self.git_calls()
		self.assertEqual(len(calls), 2)
		self.assertIn("--grep=로그인 조회", calls[0])
		self.assertIn("--grep=로그인", calls[1])
		self.assertIn("--grep=조회", calls[1])
		self.assertIn("--all-match", calls[1])
		self.assertTrue(all(str(self.personal) not in args for args in calls))

	def test_all_branches_and_global_limit(self):
		newest = self.commit(self.company, "로그인 분리 조회", 1, branch="side")
		result = history.search("로그인 조회", limit=1)
		self.assertEqual([row["hash"] for row in result["commits"]], [newest])
		for args in self.git_calls():
			for flag in ("--all", "-i", "--since=3 years ago", "--max-count=1", "--date=short",
				"--format=%h%x1f%ad%x1f%s"):
				self.assertIn(flag, args)

	def test_global_merge_from_multiple_company_repos(self):
		self.write_sources(personal_class="company")
		result = history.search("로그인 조회", limit=2)
		self.assertEqual(len(result["commits"]), 2)
		self.assertEqual(result["commits"][0]["repo"], "합성 개인")
		self.assertEqual(len(self.git_calls()), 4)

	def test_repo_name_falls_back_to_id(self):
		self.write_sources(company_name="")
		self.assertEqual({row["repo"] for row in history.search("로그인 조회")["commits"]}, {"xyz"})

	def test_repo_paths_are_absent_from_results_and_cli(self):
		result = history.search("로그인 조회")
		code, out, err = self.cli("history", "로그인 조회")
		self.assertEqual(code, 0)
		for path in (str(self.company), str(self.personal)):
			self.assertNotIn(path, json.dumps(result, ensure_ascii=False) + out + err)

	def test_git_failures_skip_whole_repo_and_only_report_count(self):
		self.fail_all_match = True
		result = history.search("로그인 조회")
		self.assertEqual(result["commits"], [])
		self.assertEqual(result["commits_error"], "저장소 1곳 읽기 실패")
		self.write_sources(personal_class="company")
		self.git_errors = {str(repo): FileNotFoundError(str(repo)) for repo in (self.company, self.personal)}
		result = history.search("로그인 조회")
		self.assertEqual(result["commits_error"], "저장소 2곳 읽기 실패")
		self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))

	def test_missing_source_config_does_not_expose_path_or_hide_notes(self):
		(self.hub / "sources.toml").unlink()
		result = history.search("로그인 조회")
		self.assertEqual(len(result["notes"]), 1)
		self.assertEqual(result["commits"], [])
		self.assertEqual(result["commits_error"], "원천 설정을 읽을 수 없습니다")
		self.assertNotIn(str(self.hub), json.dumps(result, ensure_ascii=False))

	def test_word_search_uses_first_three_words_of_at_least_two_characters(self):
		history.search("로그인 가 조회 나 수정 기록")
		greps = [arg for arg in self.git_calls()[1] if arg.startswith("--grep=")]
		self.assertEqual(greps, ["--grep=로그인", "--grep=조회", "--grep=수정"])

	def test_short_query_never_becomes_an_unfiltered_log(self):
		found = self.commit(self.company, "가 합성", 1)
		result = history.search("가")
		self.assertEqual([row["hash"] for row in result["commits"]], [found])
		self.assertEqual(len(self.git_calls()), 2)
		self.assertTrue(all("--grep=가" in args for args in self.git_calls()))

	def test_query_is_literal_and_case_insensitive_for_git(self):
		found = self.commit(self.company, "XYZ [합성].* 처리", 1)
		self.commit(self.company, "xyz 합성 다른 처리", 0)
		result = history.search("xyz [합성].*")
		self.assertEqual([row["hash"] for row in result["commits"]], [found])

	def test_subject_is_limited_to_120_characters(self):
		self.commit(self.company, "조회 " + "가" * 150, 1)
		self.assertEqual(len(history.search("조회")["commits"][0]["subject"]), 120)

	def test_note_parser_ignores_logs_and_malformed_rows(self):
		self.note_output = ("WARN 합성 로그\n1 · 0.032 · 노트/xyz.md · 합성 제목 > 조회 · " + "가" * 130 + " · 끝\n"
			"틀린 줄\n2 · 점수 · 노트/xyz.md · 제목 · 발췌\n0 · 0.1 · 노트/xyz.md · 제목 · 발췌\n"
			"3 · nan · 노트/xyz.md · 제목 · 발췌\n4 · inf · 노트/xyz.md · 제목 · 발췌\n"
			"5 · 0.1 ·  · 제목 · 발췌\n6 · 0.1 · 노트/xyz.md ·  · 발췌\n")
		self.assertEqual(history.search("조회")["notes"], [{"rank": 1, "score": 0.032,
			"path": "노트/xyz.md", "heading": "합성 제목 > 조회", "excerpt": "가" * 120}])

	def test_note_excerpt_can_contain_separator(self):
		self.note_output = "1 · 0.032 · 노트/xyz.md · 제목 · 첫 부분 · 뒷부분\n"
		self.assertEqual(history.search("조회")["notes"][0]["excerpt"], "첫 부분 · 뒷부분")

	def test_java_arguments_and_note_limit(self):
		query = "$(xyz); 조회"
		self.note_output += "2 · 0.02 · 노트/abc.md · 둘째 제목 · 둘째 발췌\n"
		self.assertEqual(len(history.search(query, limit=1)["notes"]), 1)
		args = self.run_mock.call_args_list[1].args[0]
		self.assertEqual(args, [str(self.java), "--enable-native-access=ALL-UNNAMED", "-jar", str(self.jar),
			"query", query, "--k", "1", "--mode", "hybrid", "--include-company"])

	def test_missing_jar(self):
		self.jar.unlink()
		result = history.search("조회")
		self.assertEqual(result["notes"], [])
		self.assertEqual(result["notes_error"], "검색 엔진 파일이 없습니다")
		self.assertTrue(all(call.args[0][0] == "git" for call in self.run_mock.call_args_list))

	def test_missing_java(self):
		self.java.unlink()
		result = history.search("조회")
		self.assertEqual(result["notes"], [])
		self.assertEqual(result["notes_error"], "Java 21을 찾을 수 없습니다")
		self.assertTrue(result["commits"])

	def test_java_home_failures_are_short_and_private(self):
		for error in (FileNotFoundError(str(self.root)),
			subprocess.CalledProcessError(1, "합성", stderr=str(self.root)),
			subprocess.TimeoutExpired("합성", 10, output=str(self.root))):
			with self.subTest(error=type(error).__name__):
				self.java_home_error = error
				result = history.search("조회")
				self.assertEqual(result["notes"], [])
				self.assertEqual(result["notes_error"], "Java 21을 찾을 수 없습니다")
				self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))

	def test_search_engine_failures_are_short_and_private(self):
		for error, message in ((FileNotFoundError(str(self.jar)), "검색 엔진을 실행할 수 없습니다"),
			(subprocess.CalledProcessError(1, "합성", stderr=str(self.jar)), "검색 엔진을 실행할 수 없습니다"),
			(subprocess.TimeoutExpired("합성", 120, output=str(self.jar)), "검색 엔진 응답 시간이 초과됐습니다")):
			with self.subTest(error=type(error).__name__):
				self.java_error = error
				result = history.search("조회")
				self.assertEqual(result["notes"], [])
				self.assertEqual(result["notes_error"], message)
				self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))
				self.assertTrue(result["commits"])

	def test_query_validation_precedes_any_subprocess(self):
		for query in (None, 1, [], "", "   ", "가" * 201, "가\n나", "가\r나", "가\t나", "가\x00나",
			"가\x1b나", "가\x7f나", "가\x85나", "가\u200b나", "가\u2028나", "가\u2029나", "가\ud800나"):
			with self.subTest(query=repr(query)), self.assertRaises(ValueError):
				history.search(query)
		self.run_mock.assert_not_called()

	def test_query_and_limit_boundaries(self):
		for query, limit in (("가", 1), ("가" * 200, 30)):
			with self.subTest(length=len(query), limit=limit):
				history.search(query, limit)
				self.assertIn(f"--max-count={limit}", self.git_calls()[-1])

	def test_limit_validation(self):
		for limit in (0, -1, 31, True, 1.5, "2", None):
			with self.subTest(limit=limit), self.assertRaises(ValueError):
				history.search("조회", limit)
		self.run_mock.assert_not_called()

	def test_cli_output_and_warning(self):
		code, out, err = self.cli("history", "로그인 조회", "--limit", "2")
		self.assertEqual((code, err), (0, ""))
		self.assertIn("■ 관련 노트\n1. 2026-10-27 합성 제목 > 조회 — 노트/261027-01-xyz.md\n   합성 발췌\n", out)
		self.assertIn("■ 관련 커밋\n", out)
		self.assertRegex(out, rf"\d{{4}}-\d{{2}}-\d{{2}} 합성 팀 {self.words} 로그인 기본 조회")
		self.assertEqual(out.splitlines()[-1], "이 결과에는 회사 자료가 들어 있습니다. AI 대화창에 붙여 넣지 마세요.")

	def test_cli_note_dates_and_missing_dates(self):
		self.note_output = "\n".join(f"{rank} · 0.03 · 노트/{name}.md · 제목 · 발췌"
			for rank, name in enumerate(("2026-10-26-xyz", "261025-xyz", "20261024-xyz", "261332-xyz", "xyz"), 1))
		code, out, err = self.cli("history", "조회")
		self.assertEqual((code, err), (0, ""))
		for rank, prefix in ((1, "2026-10-26 "), (2, "2026-10-25 "), (3, "2026-10-24 "), (4, ""), (5, "")):
			self.assertIn(f"{rank}. {prefix}제목 — 노트/", out)

	def test_cli_no_results_and_sanitized_errors(self):
		self.note_output = "WARN 합성 로그\n"
		code, out, err = self.cli("history", "없는 질의")
		self.assertEqual((code, err), (0, ""))
		self.assertEqual(out.count("찾은 것이 없습니다"), 2)
		self.java_error = subprocess.TimeoutExpired("합성", 120, output=str(self.root))
		self.git_errors[str(self.company)] = OSError(str(self.company))
		code, out, err = self.cli("history", "조회")
		self.assertEqual((code, err), (0, ""))
		self.assertIn("검색 엔진 응답 시간이 초과됐습니다", out)
		self.assertIn("저장소 1곳 읽기 실패", out)
		self.assertNotIn(str(self.root), out)
		self.assertTrue(out.endswith("AI 대화창에 붙여 넣지 마세요.\n"))

	def test_cli_invalid_arguments_and_query_exit_codes(self):
		for args in ((), ("other", "조회"), ("history",), ("history", "조회", "추가"),
			("history", "조회", "--limit"), ("history", "조회", "--limit", "abc"),
			("history", "조회", "--limit", "0"), ("history", "조회", "--limit", "31")):
			with self.subTest(args=args):
				code, out, err = self.cli(*args)
				self.assertEqual(code, 2)
				self.assertTrue(err)
		for query in ("", "가\n나"):
			code, out, err = self.cli("history", query)
			self.assertEqual(code, 1)
			self.assertIn("오류:", err)
		self.run_mock.assert_not_called()

	def test_agency_entry_point_delegates_to_local_history(self):
		from agency.__main__ import main
		with redirect_stdout(io.StringIO()) as out:
			self.assertEqual(main(["history", "로그인 조회"]), 0)
		self.assertIn("■ 관련 커밋", out.getvalue())
		self.assertEqual(len(self.git_calls()), 2)

	def test_module_descriptions_limit_execution_to_user_surfaces(self):
		warning = ("이 명령의 출력은 회사 자료를 담으므로 AI 에이전트(Claude·Codex·AGY 등)가 대신 실행하거나 "
			"출력을 읽지 않는다. 사용자 터미널과 사무실 화면 전용")
		for module in (history, history_cli):
			self.assertIn(warning, module.__doc__)
