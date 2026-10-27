"""임시 홈·저장소와 가짜 판정으로 검수와 납품 묶음의 경계를 검사한다."""

import copy
import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

from agency import acceptance, ai, project, review_cli
from agency.__main__ import main
from agency.departments import load_departments
from collector.config import ConfigError
from orchestrator import checks, targets
from orchestrator.record import Run, new_run_id
from tests.test_orchestrator_targets import init_repo, write_repos


class ReviewTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.repo = init_repo(self.root / "repo")
		(self.repo / "src").mkdir()
		(self.repo / "src/value.txt").write_text("start\n", encoding="utf-8")
		self.git("add", "--", "src/value.txt")
		self.git("commit", "-q", "-m", "합성 시작")
		self.head = self.git("rev-parse", "HEAD")
		self.git("branch", "fixture", self.head)
		self.tip = self.commit("fixture", "src/value.txt", "done\n")
		self.repo_row = {"id": "sample", "path": str(self.repo), "test_command": self.command(
			"from pathlib import Path; assert Path('src/value.txt').read_text() == 'done\\n'; print('1 passed')"),
			"restart_paths": ["src/"], "restart_notice": "승인 뒤 합성 서비스를 재시작합니다"}
		write_repos(self.hub, self.repo_row)
		self.write_plans()
		self.write_department()
		self.department = load_departments()["sample"]
		self.criteria = ["프로젝트 가지의 값이 바뀐다", "합성 시험이 통과한다"]
		self.data = {"criteria": [{"criterion": criterion, "verdict": "pass", "evidence": "diff와 1 passed로 확인"}
			for criterion in self.criteria], "summary": "모든 필수 기준을 확인했습니다"}
		for patch in (mock.patch.object(ai, "ask", side_effect=self.answer),
				mock.patch("orchestrator.providers.call", side_effect=AssertionError("실제 AI 호출 금지"))):
			started = patch.start()
			if patch.attribute == "ask":
				self.ask = started
			self.addCleanup(patch.stop)

	def git(self, *args):
		return checks.git(self.repo, *args).strip()

	def command(self, code):
		return shlex.join([sys.executable, "-c", code])

	def commit(self, branch, relative, text):
		with tempfile.TemporaryDirectory(dir=self.root) as temporary:
			worktree = Path(temporary) / "build"
			checks.git(self.repo, "worktree", "add", "--detach", str(worktree), branch)
			try:
				path = worktree / relative
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(text, encoding="utf-8")
				checks.git(worktree, "add", "--", relative)
				checks.git(worktree, "commit", "-q", "-m", "합성 변경")
				tip = checks.git(worktree, "rev-parse", "HEAD").strip()
				self.git("update-ref", "refs/heads/" + branch, tip)
			finally:
				checks.git(self.repo, "worktree", "remove", "--force", str(worktree))
		return tip

	def write_plans(self, reviewers=("writer", "other", "judge")):
		text = ""
		for identity in ("writer", "other", "judge"):
			text += f'[[plans]]\nid = "{identity}"\ntool = "codex"\nowner = "personal"\nallow = ["personal"]\n'
		text += '[orchestration]\nimplementer = ["writer"]\nreviewer = ' + json.dumps(reviewers) + "\n"
		(self.hub / "plans.toml").write_text(text, encoding="utf-8")

	def write_department(self, outward=("merge", "push", "restart", "publish"), review_skill=""):
		(self.hub / "departments.toml").write_text('''[[department]]
id = "sample"
name = "합성 부서"
scope = "로컬 시험"
repos = ["sample"]
allow_plans = ["writer", "other", "judge"]
playbook = "playbook"
skills = ["implementation"]
budget = { calls = 20, minutes = 10 }
''' + "outward = " + json.dumps(outward) + "\nreview_skill = " + json.dumps(review_skill) + "\n", encoding="utf-8")

	def new(self, changed=True):
		row = project.new(self.department, "합성 프로젝트", "요청 본문 비공개 표식")
		identity = row["id"]
		project.update_spec(identity, goal="합성 목표", target="로컬 결과", out_of_scope="외부 작업",
			done_criteria=self.criteria, budget=self.department.budget)
		project.confirm_spec(identity, self.department)
		for status in ("계획", "실행 중", "검수"):
			project.transition(identity, status)
		self.git("branch", row["branch"], self.tip if changed else self.head)
		for number, plan in enumerate(("writer", "other"), 1):
			path = project._folder(identity) / f"t{number}.toml"
			path.write_text("작업 요청서 비공개 표식", encoding="utf-8")
			execution = Run(new_run_id(), {"goal": "구현 요청 비공개 표식"}, str(self.repo))
			execution.document = "구현 응답 비공개 표식"
			execution.review_summary = "구현 자기평가 비공개 표식"
			execution.calls = [{"role": "implement", "ok": True, "plan": plan},
				{"role": "implement", "ok": False, "plan": "judge"},
				{"role": "implement", "ok": None, "plan": "judge"},
				{"role": "implement", "ok": 1, "plan": "judge"},
				{"role": "review", "ok": True, "plan": "judge"}]
			execution.save()
			project.add_task(identity, f"합성 작업 {number}", str(path))
			project.update_task(identity, f"t{number}", status="완료", run_id=execution.run_id,
				commit=self.tip if changed else self.head)
		return project.load(identity)

	def answer(self, department, prompt, schema, repo, plans):
		self.assertIsNone(repo)
		self.assertEqual([plan.id for plan in plans], ["judge"])
		self.assertEqual(schema, acceptance.SCHEMA)
		self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
		return ai.Answer(copy.deepcopy(self.data), "judge", 2.5)

	def blocked(self, result, reason):
		self.assertEqual((result["status"], result["passed"], result["delivery"]), ("막힘", False, ""))
		row = project.load(Path(result["report"]).parent.name)
		self.assertEqual(row["status"], "막힘")
		self.assertIn(reason, row["blocked_reason"])
		self.assertFalse((Path(result["report"]).parent / "delivery.json").exists())
		self.assertEqual(stat.S_IMODE(Path(result["report"]).stat().st_mode), 0o600)
		return row

	def cli(self, argv, entry=review_cli.main):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = entry(argv)
		return code, out.getvalue(), err.getvalue()

	def test_pass_bundle_hash_permissions_report_usage_and_main_preservation(self):
		row = self.new()
		(self.repo / "src/value.txt").write_text("staged\n", encoding="utf-8")
		self.git("add", "--", "src/value.txt")
		(self.repo / "src/value.txt").write_text("dirty\n", encoding="utf-8")
		(self.repo / "local.txt").write_text("untracked", encoding="utf-8")
		before = [(self.repo / path).read_bytes() for path in (".git/HEAD", ".git/index", "src/value.txt", "local.txt")]
		for name in ("report.md", "delivery.json"):
			path = project._folder(row["id"]) / name
			path.write_text("기존 합성 결과", encoding="utf-8")
			path.chmod(0o644)
		result = acceptance.review(row["id"])
		self.assertEqual(set(result), {"status", "passed", "report", "delivery", "failed"})
		self.assertEqual((result["status"], result["passed"], result["failed"]), ("납품 대기", True, []))
		bundle = json.loads(Path(result["delivery"]).read_text(encoding="utf-8"))
		self.assertEqual(set(bundle), {"version", "project", "target", "repo", "branch", "branch_tip", "base",
			"actions", "restart_notice", "changed", "criteria", "tests", "reviewer", "made", "hash"})
		digest = bundle.pop("hash")
		self.assertEqual(digest, hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
		self.assertEqual((bundle["version"], bundle["project"], bundle["target"], bundle["repo"]),
			(1, row["id"], "sample", str(self.repo)))
		self.assertEqual((bundle["branch"], bundle["branch_tip"], bundle["base"]), (row["branch"], self.tip, self.head))
		self.assertEqual(bundle["actions"], ["merge", "push", "restart"])
		self.assertEqual(bundle["restart_notice"], self.repo_row["restart_notice"])
		self.assertEqual(bundle["changed"], ["src/value.txt"])
		self.assertEqual(bundle["criteria"], self.data["criteria"])
		self.assertEqual(bundle["tests"], {"command": self.repo_row["test_command"], "ok": True, "summary": "1 passed"})
		self.assertEqual(bundle["reviewer"], "judge")
		self.assertIsNotNone(datetime.fromisoformat(bundle["made"]).tzinfo)
		for key in ("report", "delivery"):
			self.assertEqual(stat.S_IMODE(Path(result[key]).stat().st_mode), 0o600)
		report = Path(result["report"]).read_text(encoding="utf-8")
		for text in (row["title"], "확정 명세", *self.criteria, "합성 목표", "로컬 결과", "외부 작업",
				"합성 작업 1", "합성 작업 2", self.tip, "판정 요금제", "judge", "1 passed", "src/value.txt",
				"승인하면 실행될 바깥 작업", "merge", "push", "restart", self.data["summary"], self.data["criteria"][0]["evidence"]):
			self.assertIn(text, report)
		self.assertEqual(project.load(row["id"])["used"], {"calls": 1, "seconds": 2.5})
		self.assertEqual(project.load(row["id"])["status"], "납품 대기")
		self.assertEqual(before, [(self.repo / path).read_bytes() for path in (".git/HEAD", ".git/index", "src/value.txt", "local.txt")])
		self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
		self.ask.assert_called_once()

	def test_actions_follow_outward_permissions_and_restart_prefixes(self):
		cases = [((), ["src/"], ["merge"]), (("push",), ["src/"], ["merge", "push"]),
			(("restart",), ["src/value"], ["merge", "restart"]),
			(("push", "restart"), ["other/"], ["merge", "push"]), (("restart",), [], ["merge"])]
		for outward, paths, expected in cases:
			with self.subTest(outward=outward, paths=paths):
				self.write_department(outward=outward)
				write_repos(self.hub, {**self.repo_row, "restart_paths": paths})
				result = acceptance.review(self.new()["id"])
				bundle = json.loads(Path(result["delivery"]).read_text(encoding="utf-8"))
				self.assertEqual(bundle["actions"], expected)

	def test_nonzero_test_exit_cannot_be_waived_by_ai(self):
		write_repos(self.hub, {**self.repo_row, "test_command": self.command("import sys; print('FAILED synthetic'); sys.exit(1)")})
		result = acceptance.review(self.new()["id"])
		self.blocked(result, "검수 불합격: 실패 0개·미검증 0개, 시험 실패")
		self.assertEqual(result["failed"], [])
		self.assertIn("FAILED synthetic", Path(result["report"]).read_text(encoding="utf-8"))
		self.ask.assert_called_once()

	def test_one_failed_or_unverified_criterion_blocks_delivery(self):
		for verdict in ("fail", "unverified"):
			with self.subTest(verdict=verdict):
				self.data["criteria"][1]["verdict"] = verdict
				result = acceptance.review(self.new()["id"])
				self.blocked(result, "검수 불합격:")
				self.assertEqual(result["failed"], [self.criteria[1]])
				self.assertIn(verdict, Path(result["report"]).read_text(encoding="utf-8"))

	def test_reordered_criteria_block_even_if_all_pass(self):
		self.data["criteria"].reverse()
		self.blocked(acceptance.review(self.new()["id"]), "검수 판정 형식이 잘못됐습니다")

	def test_invalid_response_shapes_and_bounds_block_delivery(self):
		valid = copy.deepcopy(self.data)
		cases = [None, [], {}, {"criteria": [], "summary": "빈 기준"}, {**valid, "summary": ""},
			{**valid, "summary": "가" * 1001}, {**valid, "summary": 1}, {**valid, "extra": True}]
		for field, value in (("verdict", "PASS"), ("verdict", []), ("evidence", ""), ("evidence", "가" * 501),
				("evidence", 1), ("criterion", "변형된 기준"), ("criterion", 1)):
			data = copy.deepcopy(valid)
			data["criteria"][0][field] = value
			cases.append(data)
		cases += [{**valid, "criteria": [valid["criteria"][0]] * 2},
			{**valid, "criteria": [None, valid["criteria"][1]]},
			{**valid, "criteria": [{**valid["criteria"][0], "extra": 1}, valid["criteria"][1]]}]
		for data in cases:
			with self.subTest(data=data):
				self.data = data
				self.blocked(acceptance.review(self.new()["id"]), "검수 판정 형식이 잘못됐습니다")

	def test_prompt_uses_only_spec_diff_tests_and_review_skill_body(self):
		self.write_department(review_skill="rubric")
		folder = self.hub / "skills/rubric"
		folder.mkdir(parents=True)
		(folder / "SKILL.md").write_text('---\nname: rubric\ndescription: 머리말 비공개 표식\n---\n합성 검수 루브릭 본문', encoding="utf-8")
		row = self.new()
		with mock.patch.object(ai, "review_plans", wraps=ai.review_plans) as plans:
			acceptance.review(row["id"])
		self.assertEqual(plans.call_args.kwargs["exclude"], ("other", "writer"))
		prompt = self.ask.call_args.args[1]
		for text in (*self.criteria, "합성 목표", "로컬 결과", "외부 작업", "diff --git", "+done", "-start", "1 passed",
				"합성 검수 루브릭 본문", "근거가 없으면 unverified", "추측으로 pass", "변경 통계"):
			self.assertIn(text, prompt)
		tests = json.loads(prompt.split("\n시험 결과:\n", 1)[1].split("\n검수 스킬:", 1)[0])
		self.assertEqual(tests, {"command": self.repo_row["test_command"], "ok": True, "summary": "1 passed"})
		for text in (row["request_text"], "작업 요청서 비공개 표식", "구현 요청 비공개 표식", "구현 응답 비공개 표식",
				"구현 자기평가 비공개 표식", "머리말 비공개 표식", '"budget"', str(self.repo)):
			self.assertNotIn(text, prompt)
		self.assertEqual(self.ask.call_args.kwargs["repo"], None)

	def test_no_independent_plan_blocks_after_machine_test(self):
		self.write_plans(reviewers=("writer", "other"))
		result = acceptance.review(self.new()["id"])
		self.blocked(result, "구현에 쓰지 않은 검수 요금제가 없습니다")
		self.assertIn("1 passed", Path(result["report"]).read_text(encoding="utf-8"))
		self.ask.assert_not_called()

	def test_call_failure_records_usage_and_only_cause_labels(self):
		for cause in ("한도 소진", "로그인 확인 필요", "입력 길이 초과", "네트워크·서버 오류", "시간 초과", "분류 불가"):
			with self.subTest(cause=cause):
				self.ask.side_effect = RuntimeError("비공개 오류 원문 " + cause)
				row = self.new()
				result = acceptance.review(row["id"])
				loaded = self.blocked(result, "검수 판정 호출 실패: " + (cause if cause != "분류 불가" else "원인 미분류"))
				self.assertNotIn("비공개 오류 원문", loaded["blocked_reason"])
				self.assertNotIn("비공개 오류 원문", Path(result["report"]).read_text(encoding="utf-8"))
				self.assertEqual(loaded["used"]["calls"], 1)
				self.assertGreaterEqual(loaded["used"]["seconds"], 0)

	def test_no_changes_block_without_ai_or_machine_test(self):
		row = self.new(changed=False)
		with mock.patch.object(acceptance, "_test") as test:
			self.blocked(acceptance.review(row["id"]), "프로젝트 가지에 변경이 없습니다")
		test.assert_not_called()
		self.ask.assert_not_called()

	def test_large_diff_is_cut_at_sixty_thousand_characters(self):
		row = self.new()
		tip = self.commit(row["branch"], "huge.txt", "가" * 70000 + "잘린 뒤 표식\n")
		raw = checks.git(self.repo, "diff", self.head, tip)
		self.assertGreater(len(raw), 60000)
		acceptance.review(row["id"])
		prompt = self.ask.call_args.args[1]
		self.assertIn(raw[:60000] + "\n...(잘림)\n시험 결과:", prompt)
		self.assertNotIn("잘린 뒤 표식", prompt)

	def test_merge_base_uses_current_target_head_and_pinned_branch_tip(self):
		row = self.new()
		self.commit("main", "independent.txt", "별도 변경\n")
		result = acceptance.review(row["id"])
		bundle = json.loads(Path(result["delivery"]).read_text(encoding="utf-8"))
		self.assertEqual((bundle["base"], bundle["branch_tip"]), (self.head, self.tip))
		self.assertNotIn("independent.txt", self.ask.call_args.args[1])

	def test_worktree_is_removed_on_timeout_oserror_and_unexpected_exception(self):
		run = subprocess.run
		for error in (subprocess.TimeoutExpired("합성 시험", 1200), OSError("합성 실행 오류"), RuntimeError("합성 예외")):
			with self.subTest(error=type(error).__name__):
				row, seen = self.new(), []

				def fake(command, **kwargs):
					if command[0] == "git":
						return run(command, **kwargs)
					seen.append(kwargs["cwd"])
					self.assertEqual(command, shlex.split(self.repo_row["test_command"]))
					self.assertEqual(kwargs["timeout"], 1200)
					self.assertFalse(kwargs["shell"])
					self.assertNotIn("env", kwargs)
					raise error

				with mock.patch.object(acceptance.subprocess, "run", side_effect=fake):
					if isinstance(error, RuntimeError):
						with self.assertRaisesRegex(RuntimeError, "합성 예외"):
							acceptance.review(row["id"])
					else:
						result = acceptance.review(row["id"])
						self.blocked(result, "시험 실패")
						if isinstance(error, subprocess.TimeoutExpired):
							self.assertIn("시간 초과", Path(result["report"]).read_text(encoding="utf-8"))
				self.assertEqual(len(seen), 1)
				self.assertFalse(seen[0].exists())
				self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

	def test_machine_summary_filters_last_twenty_lines_and_bounds_fallback(self):
		cases = [("import sys; print('noise'); print('Ran 2 tests', file=sys.stderr); print('OK', file=sys.stderr)", "Ran 2 tests\nOK"),
			("print('OK ancient'); print('noise\\n' * 20, end=''); print('first\\nsecond\\nthird')", "first\nsecond\nthird"),
			("print('head'); print('a' * 250); print('b' * 250); print('c' * 250)", "\n".join(letter * 200 for letter in "abc"))]
		for code, expected in cases:
			with self.subTest(code=code):
				selected = replace(targets.target("sample"), test_command=self.command(code))
				result = acceptance._test(selected, self.tip)
				self.assertEqual(result, {"command": selected.test_command, "ok": True, "summary": expected})

	def test_review_rejects_wrong_state_incomplete_tasks_and_external_ai(self):
		for status in ("실행 중", "납품 대기", "막힘", "취소"):
			with self.subTest(status=status):
				row = self.new()
				project.transition(row["id"], status, reason="합성 중단")
				with self.assertRaisesRegex(ValueError, "검수 상태"):
					acceptance.review(row["id"])
		for status in ("대기", "실행 중", "실패", "건너뜀"):
			row = self.new()
			project.update_task(row["id"], "t2", status=status)
			with self.assertRaisesRegex(ValueError, "모든 작업"):
				acceptance.review(row["id"])
		row = self.new()
		with mock.patch.object(acceptance, "load_departments", return_value={"sample": replace(self.department, external_ai=False)}):
			with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
				acceptance.review(row["id"])
		self.ask.assert_not_called()

	def test_cli_argument_exception_pass_and_block_exit_codes(self):
		for argv in ([], ["review"], ["review", "id", "extra"], ["show", "id"], ["review", "--bad"]):
			code, out, err = self.cli(argv)
			self.assertEqual((code, out), (2, ""))
			self.assertIn("사용법:", err)
		for error in (ValueError, ConfigError, RuntimeError):
			with mock.patch.object(review_cli, "review", side_effect=error("합성 오류")):
				self.assertEqual(self.cli(["review", "id"]), (1, "", "오류: 합성 오류\n"))
		row = self.new()
		code, out, err = self.cli(["review", row["id"]], entry=main)
		self.assertEqual((code, err), (0, ""))
		self.assertIn("검수: 통과", out)
		self.assertIn(str(project._folder(row["id"]) / "report.md"), out)
		self.assertIn("python3 -m agency deliver " + row["id"], out)
		self.data["criteria"][0]["verdict"] = "fail"
		code, out, err = self.cli(["review", self.new()["id"]], entry=main)
		self.assertEqual((code, err), (3, ""))
		self.assertIn("검수: 불합격", out)
		self.assertNotIn("python3 -m agency deliver", out)
		self.assertEqual(self.cli(["review", "invalid"])[0], 1)


if __name__ == "__main__":
	unittest.main()
