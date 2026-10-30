"""가짜 PM 응답과 임시 저장소로 계획·요청서·CLI의 경계를 검사한다."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agency import ai, plan_cli, planner, project
from agency.__main__ import main as agency_main
from agency.departments import load_departments
from collector.config import ConfigError
from orchestrator import checks, draft
from orchestrator.__main__ import read_request


def sample(count=2, size="medium"):
	return {"size": size, "tasks": [{"title": f"합성 작업 {number}",
		"role": "backend" if number == 1 else "frontend", "goal": f"합성 기능 {number}",
		"done": ["python3 -m unittest 명령이 종료 코드 0을 반환한다"],
		"owned_paths": [f"src/task{number}.py"], "forbidden": ["합성 입력을 보존한다"],
		"details": "기존 규칙을 따른다", "commit_message": f"작업: 합성 기능 {number}",
		"budget": {"calls": 4, "minutes": 30, "fixes": 1},
		"depends_on": [number - 1] if number > 1 else []} for number in range(1, count + 1)],
		"decision": {"chosen": "책임별로 나눈다", "rejected": "한 번에 고치지 않는다",
			"assumptions": "합성 자료만 쓴다", "verification": "합성 시험 출력을 확인한다"},
		"meeting": {"needed": False, "topic": ""}}


class AgencyPlannerTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub, self.repo = self.root / "hub", self.root / "repo"
		self.hub.mkdir()
		self.repo.mkdir()
		self.enterContext(mock.patch("orchestrator.test_isolation.requires", side_effect=lambda path: Path(path).resolve() != self.repo.resolve()))
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch("orchestrator.providers.call", side_effect=AssertionError("실제 AI 호출 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		checks.git(self.repo, "init", "-q", "-b", "main")
		(self.repo / "src").mkdir()
		for number in (1, 2):
			(self.repo / f"src/task{number}.py").write_text("", encoding="utf-8")
		checks.git(self.repo, "add", "--", "src/task1.py", "src/task2.py")
		(self.hub / "repos.toml").write_text('[[repo]]\nid="xyz"\npath=' + json.dumps(str(self.repo))
			+ '\ntest_command="python3 -m unittest"\ncommit_pattern="작업: .+"\n'
			'forbidden=["합성 원천을 변경하지 않는다"]\n', encoding="utf-8")
		(self.hub / "plans.toml").write_text('''[[plans]]
id="xyz"
tool="codex"
owner="personal"
allow=["personal"]
[[plans]]
id="local"
tool="ollama"
model="xyz"
owner="personal"
allow=["personal"]
''', encoding="utf-8")
		(self.hub / "departments.toml").write_text('''[[department]]
id="xyz"
name="합성 부서"
scope="합성 자료"
repos=["xyz"]
allow_plans=["xyz"]
playbook="xyz"
required_questions=["어디에 납품하나요?"]
budget={calls=10, minutes=30}
''', encoding="utf-8")
		(self.hub / "redaction").mkdir()
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "xyz-private", "kind": "회사명", "token": "company01"}]}), encoding="utf-8")
		skill = self.hub / "skills/xyz"
		skill.mkdir(parents=True)
		(skill / "SKILL.md").write_text('---\nname: xyz\ndescription: 합성 절차\n---\n'
			'책임마다 시험 출력을 확인한다.', encoding="utf-8")
		self.department = load_departments()["xyz"]
		patch = mock.patch.object(ai, "ask", return_value=ai.Answer(sample(), "xyz", 2.5))
		self.ask = patch.start()
		self.addCleanup(patch.stop)

	def confirmed(self):
		row = project.new(self.department, "합성 프로젝트", "합성 요청")
		identity = row["id"]
		project.update_spec(identity, goal="합성 목표", target="합성 저장소", out_of_scope="외부 실행",
			done_criteria=["합성 시험 통과"], budget={"calls": 10, "minutes": 30})
		project.add_answer(identity, "어디에 납품하나요?", "로컬 결과")
		return project.confirm_spec(identity, self.department)

	def blocked(self, data, reason):
		row = self.confirmed()
		self.ask.return_value = ai.Answer(data, "xyz", 2.5)
		with self.assertRaisesRegex(ValueError, reason):
			planner.plan(row["id"])
		current = project.load(row["id"])
		self.assertEqual((current["status"], current["blocked_from"]), ("막힘", "계획"))
		self.assertEqual(current["blocked_reason"], reason)
		self.assertEqual(current["tasks"], [])
		self.assertEqual(current["used"], {"calls": 1, "seconds": 2.5})
		self.assertFalse((project._folder(row["id"]) / "tasks").exists())

	def test_two_tasks_permissions_readback_dependencies_branch_and_policy(self):
		row = self.confirmed()
		data = sample()
		data["tasks"][0].update(repo="xyz-private", test_command="xyz-private", base_branch="xyz-private",
			budget={"calls": 99, "minutes": 1, "fixes": 99})
		self.ask.return_value = ai.Answer(data, "xyz", 2.5)
		with mock.patch.object(planner, "read_request", wraps=read_request) as read:
			result = planner.plan(row["id"])
		self.assertEqual(result, {"tasks": [
			{"id": "t1", "title": "합성 작업 1", "depends_on": []},
			{"id": "t2", "title": "합성 작업 2", "depends_on": ["t1"]}],
			"size": "medium", "plan": "xyz", "meeting": False})
		self.assertEqual(read.call_count, 2)
		current = project.load(row["id"])
		self.assertEqual(current["status"], "계획")
		self.assertEqual(current["used"], {"calls": 1, "seconds": 2.5})
		folder = project._folder(row["id"]) / "tasks"
		self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
		self.assertEqual(sorted(path.name for path in folder.iterdir()), ["t1.toml", "t2.toml"])
		for number, task in enumerate(current["tasks"], 1):
			path = folder / f"t{number}.toml"
			self.assertEqual(task["request_path"], str(path))
			self.assertEqual(task["depends_on"], [] if number == 1 else ["t1"])
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			request = read_request(str(path))["request"]
			self.assertEqual(request["base_branch"], row["branch"])
			self.assertEqual(request["repo"], str(self.repo))
			self.assertEqual(request["test_command"], "python3 -m unittest")
			self.assertEqual(request["forbidden"], ["합성 입력을 보존한다"] + draft.DEFAULT_FORBIDDEN
				+ ["합성 원천을 변경하지 않는다"])
		self.assertEqual(read_request(str(folder / "t1.toml"))["budget"], {"calls": 8, "minutes": 10, "fixes": 2})
		self.ask.assert_called_once()
		args, kwargs = self.ask.call_args
		self.assertEqual(args[0], self.department)
		self.assertIs(args[2], planner.SCHEMA)
		self.assertEqual(kwargs, {"repo": self.repo})

	def test_prompt_contains_spec_answers_playbook_size_rules_and_only_first_400_files(self):
		row = self.confirmed()
		original = checks.git

		def fake_git(repo, *args, **kwargs):
			if args == ("ls-files",):
				return "\n".join(f"src/file{number:03d}.py" for number in range(1, 402))
			return original(repo, *args, **kwargs)

		def answer(department, prompt, schema, repo):
			self.assertEqual(project.load(row["id"])["status"], "계획")
			return ai.Answer(sample(), "xyz", 2.5)

		self.ask.side_effect = answer
		with mock.patch.object(checks, "git", side_effect=fake_git):
			planner.plan(row["id"])
		prompt = self.ask.call_args.args[1]
		for text in ("합성 목표", "로컬 결과", "책임마다 시험 출력을 확인한다", "xyz", "src/file400.py",
				"작은 일", "중간 일", "large", "1건", "2~4건", "500줄", "상대 경로", "실제 홈",
				"책임 하나", "참·거짓", "depends_on", "읽기 전용"):
			self.assertIn(text, prompt)
		self.assertNotIn("src/file401.py", prompt)
		self.assertNotIn(str(self.repo), prompt)
		self.assertEqual(planner.SCHEMA["properties"]["size"]["enum"], ["small", "medium", "large"])
		task_schema = planner.SCHEMA["properties"]["tasks"]["items"]
		self.assertEqual(set(task_schema["required"]), set(sample()["tasks"][0]))
		self.assertFalse(task_schema["additionalProperties"])

	def test_large_five_and_empty_plans_block_without_files(self):
		for data, reason in ((sample(size="large"), "작업이 4건을 넘어 단계로 나눠 다시 요청해야 합니다"),
				(sample(5), "작업이 4건을 넘어 단계로 나눠 다시 요청해야 합니다"),
				(sample(0, "small"), "나눌 작업이 없습니다")):
			with self.subTest(size=data["size"], count=len(data["tasks"])):
				self.blocked(data, reason)

	def test_four_tasks_and_multiple_dependencies_preserve_order(self):
		row = self.confirmed()
		data = sample(4)
		data["tasks"][3]["depends_on"] = [1, 2, 3]
		self.ask.return_value = ai.Answer(data, "xyz", 2.5)
		result = planner.plan(row["id"])
		self.assertEqual([task["id"] for task in result["tasks"]], ["t1", "t2", "t3", "t4"])
		self.assertEqual(result["tasks"][3]["depends_on"], ["t1", "t2", "t3"])
		self.assertEqual(len(list((project._folder(row["id"]) / "tasks").glob("*.toml"))), 4)

	def test_malformed_plan_or_meeting_blocks_before_writes(self):
		for data, field in ((None, "size"), ({**sample(), "size": "xyz"}, "size"),
				({**sample(), "tasks": None}, "tasks"), ({**sample(), "meeting": None}, "meeting"),
				({**sample(), "meeting": {"needed": "true", "topic": "합성"}}, "meeting"),
				({**sample(), "meeting": {"needed": True, "topic": "\x00"}}, "meeting")):
			with self.subTest(field=field, data=data):
				self.blocked(data, f"{field} 칸이 잘못됐습니다")

	def test_bad_second_task_fields_and_public_text_do_not_leave_first_file(self):
		cases = [("owned_paths", value) for value in (["/tmp/xyz.py"], ["../xyz.py"], ["src/../xyz.py"], [])]
		cases += [("role", "xyz"), ("title", ""), ("title", "xyz-private"), ("goal", "xyz-private"),
			("done", []), ("details", "xyz-private"), ("commit_message", "합성 커밋"),
			("details", "\ud800"),
			("budget", {"calls": True, "minutes": 30, "fixes": 1})]
		for field, value in cases:
			with self.subTest(field=field, value=value):
				data = sample()
				data["tasks"][1][field] = value
				self.blocked(data, f"2번째 작업의 {field} 칸이 잘못됐습니다")

	def test_dependencies_only_allow_integer_indices_of_preceding_tasks(self):
		for position, dependencies in ((0, [1]), (0, [2]), (1, [2]), (1, [3]), (1, [0]),
				(1, [-1]), (1, [True]), (1, ["1"]), (1, "1"), (1, None)):
			with self.subTest(position=position, dependencies=dependencies):
				data = sample()
				data["tasks"][position]["depends_on"] = dependencies
				self.blocked(data, f"{position + 1}번째 작업의 depends_on 칸이 잘못됐습니다")

	def test_existing_tasks_and_unconfirmed_status_are_rejected_without_changes(self):
		row = self.confirmed()
		project.add_task(row["id"], "기존 작업", str(project._folder(row["id"]) / "prior.toml"))
		rows = [row, project.new(self.department, "접수", "합성 요청")]
		for row in rows:
			with self.subTest(identity=row["id"]):
				before = project.load(row["id"])
				with self.assertRaises(ValueError):
					planner.plan(row["id"])
				self.assertEqual(project.load(row["id"]), before)
		self.ask.assert_not_called()

	def test_external_ai_forbidden_department_is_rejected_before_call(self):
		row = self.confirmed()
		path = self.hub / "departments.toml"
		path.write_text(path.read_text().replace('allow_plans=["xyz"]', 'allow_plans=["local"]')
			+ "external_ai=false\n", encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
			planner.plan(row["id"])
		self.ask.assert_not_called()
		self.assertEqual(project.load(row["id"])["status"], "명세 확정")

	def test_decision_record_and_truncated_meeting_checkpoint(self):
		row = self.confirmed()
		data = sample()
		data["decision"].update(chosen="가" * 1000, rejected="나")
		data["meeting"] = {"needed": True, "topic": "가" * 350}
		self.ask.return_value = ai.Answer(data, "xyz", 2.5)
		result = planner.plan(row["id"])
		current = project.load(row["id"])
		self.assertTrue(result["meeting"])
		self.assertEqual(current["decisions"], [{"topic": "작업 나누기", **data["decision"]}])
		self.assertEqual(current["checkpoint"], {"step": "meeting", "detail": "가" * 300})

	def test_invalid_decision_is_skipped_and_single_task_plan_is_supported(self):
		for value in ("", " ", "가" * 1001, None):
			with self.subTest(value_type=type(value).__name__):
				row = self.confirmed()
				data = sample(1, "small")
				data["decision"]["verification"] = value
				self.ask.return_value = ai.Answer(data, "xyz", 2.5)
				result = planner.plan(row["id"])
				self.assertEqual(len(result["tasks"]), 1)
				self.assertEqual(project.load(row["id"])["decisions"], [])
				self.assertEqual(project.load(row["id"])["checkpoint"], {"step": "", "detail": ""})

	def test_ask_failure_is_classified_blocked_and_reraised(self):
		for message, cause in (("합성 실패 (한도 소진)", "한도 소진"), ("xyz-private", "원인 미분류")):
			with self.subTest(cause=cause):
				row = self.confirmed()
				self.ask.reset_mock()
				self.ask.side_effect = RuntimeError(message)
				with self.assertRaises(RuntimeError) as caught:
					planner.plan(row["id"])
				current = project.load(row["id"])
				self.assertEqual(current["status"], "막힘")
				self.assertEqual(current["blocked_reason"], "PM 계획 호출 실패: " + cause)
				self.assertEqual(str(caught.exception), current["blocked_reason"])
				self.ask.assert_called_once()
				self.assertEqual(current["used"]["calls"], 1)
				self.assertFalse((project._folder(row["id"]) / "tasks").exists())

	def test_symlinked_request_directory_is_never_written(self):
		row = self.confirmed()
		folder = project._folder(row["id"]) / "tasks"
		outside = self.root / "outside"
		outside.mkdir()
		folder.symlink_to(outside, target_is_directory=True)
		with self.assertRaises(ValueError):
			planner.plan(row["id"])
		self.assertEqual(project.load(row["id"])["tasks"], [])
		self.assertEqual(project.load(row["id"])["status"], "막힘")
		self.assertEqual(list(outside.iterdir()), [])

	def test_leftover_request_directory_is_moved_aside_not_overwritten(self):
		row = self.confirmed()
		folder = project._folder(row["id"]) / "tasks"
		folder.mkdir()
		(folder / "t1.toml").write_text("합성 원본", encoding="utf-8")
		planner.plan(row["id"])
		kept = [path for path in folder.parent.iterdir() if path.name.startswith("tasks-failed-")]
		self.assertEqual(len(kept), 1)
		self.assertEqual((kept[0] / "t1.toml").read_text(encoding="utf-8"), "합성 원본")
		self.assertEqual(len(project.load(row["id"])["tasks"]), 2)
		self.assertNotEqual((folder / "t1.toml").read_text(encoding="utf-8"), "합성 원본")

	def test_replan_after_blocked_call_is_resumed(self):
		row = self.confirmed()
		self.ask.side_effect = RuntimeError("합성 실패 (한도 소진)")
		with self.assertRaises(RuntimeError):
			planner.plan(row["id"])
		resumed = project.resume(row["id"])
		self.assertEqual((resumed["status"], resumed["tasks"]), ("계획", []))
		self.ask.side_effect = None
		self.ask.return_value = ai.Answer(sample(), "xyz", 2.5)
		result = planner.plan(row["id"])
		current = project.load(row["id"])
		self.assertEqual(len(result["tasks"]), 2)
		self.assertEqual(current["status"], "계획")
		self.assertEqual(current["used"]["calls"], 2)

	def test_readback_failure_prevents_task_registration(self):
		row = self.confirmed()
		with mock.patch.object(planner, "read_request", side_effect=ValueError("합성 되읽기 실패")):
			with self.assertRaises(ValueError):
				planner.plan(row["id"])
		self.assertEqual(project.load(row["id"])["tasks"], [])
		self.assertEqual(project.load(row["id"])["status"], "막힘")

	def test_request_creation_is_exclusive_nofollow_and_mode_0600(self):
		row = self.confirmed()
		with mock.patch.object(planner.os, "open", wraps=os.open) as opened:
			planner.plan(row["id"])
		calls = [call for call in opened.call_args_list if str(call.args[0]).endswith(("t1.toml", "t2.toml"))]
		self.assertEqual(len(calls), 2)
		for call in calls:
			self.assertTrue(call.args[1] & os.O_EXCL)
			self.assertTrue(call.args[1] & os.O_NOFOLLOW)
			self.assertEqual(call.args[2], 0o600)

	def test_cli_delegation_output_argument_errors_and_runtime_errors(self):
		row = self.confirmed()
		with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
			self.assertEqual(agency_main(["plan", row["id"]]), 0)
		self.assertEqual(output.getvalue(), "- t1 합성 작업 1 (의존: 없음)\n- t2 합성 작업 2 (의존: t1)\n")
		self.assertEqual(errors.getvalue(), "")
		for arguments in ([], ["plan"], ["plan", row["id"], "extra"], ["show", row["id"]]):
			with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
				self.assertEqual(plan_cli.main(arguments), 2)
		for exception in (ValueError("합성 값 오류"), ConfigError("합성 설정 오류"), RuntimeError("합성 호출 오류")):
			with self.subTest(kind=type(exception).__name__), mock.patch.object(plan_cli, "plan", side_effect=exception), \
					redirect_stderr(io.StringIO()) as errors:
				self.assertEqual(plan_cli.main(["plan", row["id"]]), 1)
				self.assertEqual(errors.getvalue(), f"오류: {exception}\n")


if __name__ == "__main__":
	unittest.main()
