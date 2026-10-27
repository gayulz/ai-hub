"""프로젝트 원장과 CLI를 합성 임시 홈에서 검사한다."""

import copy
import hashlib
import io
import json
import os
import runpy
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from agency import project, skills
from agency.__main__ import main
from agency.departments import load_departments


class ProjectTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		patch = mock.patch.object(skills, "REPO", self.root / "repo")
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "xyz"
tool = "codex"
owner = "personal"
allow = ["personal"]
''', encoding="utf-8")
		(self.hub / "departments.toml").write_text('''[[department]]
id = "xyz"
name = "합성 부서"
scope = "합성 자료"
repos = ["ai-hub"]
allow_plans = ["xyz"]
playbook = "xyz"
required_questions = ["어디에 납품하나요?"]
budget = { calls = 3, minutes = 2 }
''', encoding="utf-8")
		self.department = load_departments()["xyz"]
		self.spec = {"goal": "합성 기능", "target": "합성 자료", "out_of_scope": "외부 실행",
			"done_criteria": ["합성 시험 통과"], "budget": {"calls": 3, "minutes": 2}}

	def new(self):
		return project.new(self.department, "합성 프로젝트", "합성 요청")

	def confirmed(self):
		row = self.new()
		project.update_spec(row["id"], **self.spec)
		project.add_answer(row["id"], "어디에 납품하나요?", "로컬 결과")
		return project.confirm_spec(row["id"], self.department)

	def reach(self, status):
		row = self.new() if status == "접수 중" else self.confirmed()
		sequence = ["명세 확정", "계획", "실행 중", "검수", "납품 대기", "납품 중", "완료"]
		if status in sequence:
			for target in sequence[1:sequence.index(status) + 1]:
				row = project.transition(row["id"], target)
		elif status == "막힘":
			row = project.transition(row["id"], "막힘", reason="합성 중단")
		elif status == "취소":
			row = project.cancel(row["id"])
		return row

	def files(self, identity):
		folder = self.hub / "projects" / identity
		return tuple((folder / name).read_bytes() for name in ("project.json", "events.jsonl"))

	def cli(self, *arguments, stdin=""):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
			try:
				code = main(list(arguments))
			except SystemExit as exc:
				code = exc.code
		return code, out.getvalue(), err.getvalue()

	def test_initial_fields_permissions_events_and_round_trip(self):
		row = self.new()
		self.assertEqual(row["version"], 1)
		self.assertEqual(row["status"], "접수 중")
		self.assertEqual(row["branch"], "agency/" + row["id"])
		self.assertEqual(row["budget"], {"calls": 3, "minutes": 2})
		self.assertEqual(row["used"], {"calls": 0, "seconds": 0})
		for key in ("answers", "tasks", "decisions"):
			self.assertEqual(row[key], [])
		self.assertEqual(row["checkpoint"], {"step": "", "detail": ""})
		self.assertEqual(row["blocked_reason"], "")
		self.assertEqual(row["blocked_from"], "")
		self.assertEqual(datetime.fromisoformat(row["created"]).utcoffset().total_seconds(), 9 * 3600)
		self.assertEqual(project.load(row["id"]), row)
		folder = self.hub / "projects" / row["id"]
		for path in (folder, folder.parent):
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
		for name in ("project.json", "events.jsonl"):
			self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
		events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["kind"], "new")
		self.assertEqual(events[0]["at"], row["updated"])
		project.set_checkpoint(row["id"], "계획", "합성 체크포인트")
		self.assertEqual(project.load(row["id"])["checkpoint"], {"step": "계획", "detail": "합성 체크포인트"})
		self.assertEqual(len((folder / "events.jsonl").read_text().splitlines()), 2)

	def test_ids_use_kst_date_and_reserve_existing_directories(self):
		stamp = datetime(2030, 1, 2, 0, 30, tzinfo=ZoneInfo("Asia/Seoul"))
		with mock.patch.object(project, "now", return_value=stamp):
			first = self.new()
			(self.hub / "projects" / "p-20300102-002").mkdir()
			second = self.new()
		self.assertEqual(first["id"], "p-20300102-001")
		self.assertEqual(second["id"], "p-20300102-003")
		self.assertEqual([row["id"] for row in project.list_projects()], [first["id"], second["id"]])

	def test_new_rejects_invalid_text_before_creating_records(self):
		for title, request in (("", "요청"), (" " * 3, "요청"), ("가" * 121, "요청"),
				("제목", ""), ("제목", "가" * 4001), (True, "요청")):
			with self.subTest(title_length=len(str(title)), request_length=len(request)), self.assertRaises(ValueError):
				project.new(self.department, title, request)
		self.assertEqual(project.list_projects(), [])

	def test_all_generic_state_transitions_and_file_invariance(self):
		states = ("접수 중", "명세 확정", "계획", "실행 중", "검수", "납품 대기", "납품 중", "완료", "막힘", "취소")
		self.assertEqual(project.STATUSES, states)
		allowed = {("명세 확정", "계획"), ("계획", "실행 중"), ("실행 중", "검수"),
			("검수", "실행 중"), ("검수", "납품 대기"), ("납품 대기", "납품 중"),
			("납품 대기", "실행 중"), ("납품 중", "완료")}
		for before in states:
			for after in (*states, "잘못된 상태"):
				with self.subTest(before=before, after=after):
					row = self.reach(before)
					snapshot = self.files(row["id"])
					permitted = ((before, after) in allowed or
						before not in ("완료", "취소") and after in ("막힘", "취소"))
					if permitted:
						result = project.transition(row["id"], after, reason="합성 이유")
						self.assertEqual(result["status"], after)
					else:
						with self.assertRaisesRegex(ValueError, "전이|명세|재개"):
							project.transition(row["id"], after, reason="합성 이유")
						self.assertEqual(self.files(row["id"]), snapshot)

	def test_block_requires_reason_and_resume_restores_every_active_state(self):
		for state in project.STATUSES:
			if state in ("완료", "취소", "막힘"):
				continue
			with self.subTest(state=state):
				row = self.reach(state)
				snapshot = self.files(row["id"])
				with self.assertRaises(ValueError):
					project.transition(row["id"], "막힘")
				self.assertEqual(self.files(row["id"]), snapshot)
				blocked = project.transition(row["id"], "막힘", reason="합성 이유")
				self.assertEqual(blocked["blocked_from"], state)
				self.assertEqual(blocked["blocked_reason"], "합성 이유")
				project.transition(row["id"], "막힘", reason="추가 이유")
				resumed = project.resume(row["id"])
				self.assertEqual(resumed["status"], state)
				self.assertEqual((resumed["blocked_from"], resumed["blocked_reason"]), ("", ""))
				with self.assertRaises(ValueError):
					project.resume(row["id"])

	def test_missing_spec_questions_and_frozen_hash(self):
		row = self.new()
		identity = row["id"]
		self.assertEqual(project.spec_missing(row, self.department),
			["goal", "target", "out_of_scope", "done_criteria", "어디에 납품하나요?"])
		snapshot = self.files(identity)
		with self.assertRaises(ValueError):
			project.confirm_spec(identity, self.department)
		self.assertEqual(self.files(identity), snapshot)
		project.update_spec(identity, **self.spec)
		project.add_answer(identity, "다른 질문", "합성 답")
		with self.assertRaises(ValueError):
			project.confirm_spec(identity, self.department)
		project.add_answer(identity, "어디에 납품하나요?", "로컬 결과")
		confirmed = project.confirm_spec(identity, self.department)
		expected = hashlib.sha256(json.dumps(self.spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
		self.assertEqual(confirmed["spec_hash"], expected)
		self.assertEqual(confirmed["status"], "명세 확정")
		self.assertEqual(set(confirmed["answers"][0]), {"question", "answer", "at"})
		for status in ("명세 확정", "막힘", "취소"):
			if status != "명세 확정":
				project.transition(identity, status, reason="합성 중단")
			snapshot = self.files(identity)
			with self.assertRaises(ValueError):
				project.update_spec(identity, goal="다른 목표")
			self.assertEqual(self.files(identity), snapshot)
			self.assertEqual(project.load(identity)["spec_hash"], expected)

	def test_spec_missing_checks_types_ranges_and_unanswered_questions(self):
		base = self.new()
		base["spec"] = copy.deepcopy(self.spec)
		base["answers"] = [{"question": "어디에 납품하나요?", "answer": "", "at": ""}]
		self.assertIn("어디에 납품하나요?", project.spec_missing(base, self.department))
		base["answers"][0]["answer"] = "로컬"
		for key, values in {"goal": [None, "", " " * 3, "가" * 1001], "target": [False, "", "가" * 1001],
				"out_of_scope": [[], "", "가" * 1001],
				"done_criteria": [[], "문자열", [""], ["가" * 301], ["가"] * 21]}.items():
			for value in values:
				with self.subTest(key=key, value_type=type(value).__name__):
					row = copy.deepcopy(base)
					row["spec"][key] = value
					self.assertIn(key, project.spec_missing(row, self.department))
		for key, invalids in (("calls", (0, 201, True, 1.5)), ("minutes", (0, 1441, False, "2"))):
			for invalid in invalids:
				row = copy.deepcopy(base)
				row["spec"]["budget"][key] = invalid
				self.assertIn("budget." + key, project.spec_missing(row, self.department))
		self.assertEqual(project.spec_missing(base, self.department), [])

	def test_confirmed_budget_is_copied_and_department_must_match(self):
		row = self.new()
		project.update_spec(row["id"], **{**self.spec, "budget": {"calls": 5, "minutes": 4}})
		project.add_answer(row["id"], "어디에 납품하나요?", "로컬")
		confirmed = project.confirm_spec(row["id"], self.department)
		self.assertEqual(confirmed["budget"], {"calls": 5, "minutes": 4})
		self.assertEqual(self.department.budget, {"calls": 3, "minutes": 2})
		with self.assertRaises(ValueError):
			project.confirm_spec(row["id"], self.department)
		fresh = self.new()
		with self.assertRaises(ValueError):
			project.update_spec(fresh["id"], unknown="값")
		project.update_spec(fresh["id"], **{**self.spec, "budget": {"calls": 201, "minutes": 1}})
		project.add_answer(fresh["id"], "어디에 납품하나요?", "로컬")
		snapshot = self.files(fresh["id"])
		with self.assertRaises(ValueError):
			project.confirm_spec(fresh["id"], self.department)
		self.assertEqual(self.files(fresh["id"]), snapshot)
		from dataclasses import replace
		with self.assertRaises(ValueError):
			project.confirm_spec(fresh["id"], replace(self.department, id="other"))

	def test_tasks_validate_paths_dependencies_and_keep_ready_order(self):
		row = self.new()
		identity = row["id"]
		folder = self.hub / "projects" / identity
		for path in (self.root / "outside.md", folder.parent / "other.md", folder, Path("relative.md")):
			with self.subTest(path=path.name), self.assertRaises(ValueError):
				project.add_task(identity, "작업", str(path))
		(folder / "escape").symlink_to(self.root, target_is_directory=True)
		with self.assertRaises(ValueError):
			project.add_task(identity, "작업", str(folder / "escape" / "request.md"))
		for dependencies in (["t1"], ["t99"], "t1"):
			with self.assertRaises(ValueError):
				project.add_task(identity, "작업", str(folder / "request.md"), dependencies)
		self.assertEqual(project.load(identity)["tasks"], [])
		project.add_task(identity, "첫 작업", str(folder / "one.md"))
		project.add_task(identity, "둘째 작업", str(folder / "two.md"), ["t1"])
		project.add_task(identity, "셋째 작업", str(folder / "three.md"))
		ready = project.next_ready_tasks(project.load(identity))
		self.assertEqual([task["id"] for task in ready], ["t1", "t3"])
		project.update_task(identity, "t1", status="실패")
		self.assertEqual([task["id"] for task in project.next_ready_tasks(project.load(identity))], ["t3"])
		for status in ("건너뜀", "실행 중", "완료"):
			project.update_task(identity, "t1", status=status, run_id="xyz", commit="abc")
		self.assertEqual([task["id"] for task in project.next_ready_tasks(project.load(identity))], ["t2", "t3"])
		with self.assertRaises(ValueError):
			project.update_task(identity, "t2", status="알 수 없음")
		with self.assertRaises(ValueError):
			project.update_task(identity, "t99", status="완료")
		cyclic = project.load(identity)
		cyclic["tasks"][0]["depends_on"] = ["t2"]
		with self.assertRaises(ValueError):
			project.next_ready_tasks(cyclic)

	def test_usage_budget_decisions_and_invalid_updates(self):
		identity = self.new()["id"]
		row = project.add_usage(identity, 3, 120)
		self.assertFalse(project.over_budget(row))
		row = project.add_usage(identity, 0, 0.5)
		self.assertTrue(project.over_budget(row))
		self.assertTrue(project.over_budget(project.add_usage(self.new()["id"], 4, 0)))
		self.assertTrue(project.over_budget(project.add_usage(self.new()["id"], 0, 10 ** 400)))
		for calls, seconds in ((True, 0), (-1, 0), (1.5, 0), (0, -1), (0, float("nan")), (0, float("inf"))):
			snapshot = self.files(identity)
			with self.assertRaises(ValueError):
				project.add_usage(identity, calls, seconds)
			self.assertEqual(self.files(identity), snapshot)
		row = project.add_decision(identity, "합성 주제", "채택", "기각", "가정", "합성 시험")
		self.assertEqual(row["decisions"][0]["chosen"], "채택")
		for index in range(5):
			for invalid in ("", "가" * 1001):
				values = ["가"] * 5
				values[index] = invalid
				with self.assertRaises(ValueError):
					project.add_decision(identity, *values)

	def test_mutations_use_the_shared_lock_and_reject_linked_storage(self):
		with mock.patch.object(project, "locked", wraps=project.locked) as lock:
			identity = self.new()["id"]
			project.add_usage(identity, 1, 1)
			project.cancel(identity)
		self.assertEqual([call.args for call in lock.call_args_list], [("agency:" + identity,)] * 3)
		folder = self.hub / "projects" / identity
		events = folder / "events.jsonl"
		outside = self.root / "outside.jsonl"
		outside.write_text("합성 원본", encoding="utf-8")
		events.unlink()
		events.symlink_to(outside)
		before = (folder / "project.json").read_bytes()
		with self.assertRaises(ValueError):
			project.add_usage(identity, 1, 1)
		self.assertEqual(outside.read_text(), "합성 원본")
		self.assertEqual((folder / "project.json").read_bytes(), before)
		for invalid in ("../xyz", "xyz", "/xyz", "p-20300101-000"):
			with self.assertRaises(ValueError):
				project.load(invalid)

	def test_cli_new_list_show_cancel_and_error_codes(self):
		code, out, err = self.cli("new", "--dept", "xyz", "--title", "CLI 합성", "합성 요청")
		self.assertEqual((code, err), (0, ""))
		identity = out.strip()
		self.assertRegex(identity, r"^p-[0-9]{8}-[0-9]{3}$")
		code, out, err = self.cli("list")
		self.assertEqual(code, 0)
		self.assertIn(identity + "\txyz\t접수 중\tCLI 합성", out)
		snapshot = self.files(identity)
		code, out, err = self.cli("show", identity)
		self.assertEqual((code, err), (0, ""))
		lines = out.splitlines()
		self.assertEqual([line for line in lines if line.startswith("가지:")], ["가지: agency/" + identity])
		self.assertEqual([line for line in lines if line.startswith("체크포인트:")], ["체크포인트: 없음"])
		for value in ("프로젝트: " + identity, "부서: xyz", "상태: 접수 중", "제목: CLI 합성",
				"명세 빈칸: goal, target, out_of_scope, done_criteria, 어디에 납품하나요?",
				"작업 목록:", "- 없음", "예산 사용: 호출 0/3회, 시간 0/120초"):
			self.assertIn(value, lines)
		self.assertEqual(self.files(identity), snapshot)
		code, out, err = self.cli("cancel", identity)
		self.assertEqual(code, 0)
		self.assertIn("취소", out)
		self.assertEqual(project.load(identity)["status"], "취소")
		self.assertEqual(self.cli("cancel", identity)[0], 1)
		for arguments in ((), ("unknown",), ("new",), ("show",), ("list", "extra")):
			code, out, err = self.cli(*arguments)
			self.assertEqual(code, 2)
			self.assertIn("usage:", err)
		self.assertEqual(self.cli("new", "--dept", "missing", "--title", "제목", "요청")[0], 1)
		for invalid, message in (("p-20300101-001", "프로젝트가 없습니다"),
				("../xyz", "프로젝트 id 형식이 잘못됐습니다"), ("p-20300101-000", "프로젝트 id 형식이 잘못됐습니다")):
			with self.subTest(identity=invalid):
				self.assertEqual(self.cli("show", invalid), (1, "", "오류: " + message + "\n"))

	def test_cli_show_only_latest_checkpoint_is_read_only(self):
		identity = self.new()["id"]
		project.set_checkpoint(identity, "계획", "합성 이전 확인")
		project.set_checkpoint(identity, "검수", "합성 최종 확인")
		snapshot = self.files(identity)
		code, out, err = self.cli("show", identity)
		self.assertEqual((code, err), (0, ""))
		lines = out.splitlines()
		self.assertEqual([line for line in lines if line.startswith("가지:")], ["가지: agency/" + identity])
		self.assertEqual([line for line in lines if line.startswith("체크포인트:")],
			["체크포인트: 검수 · 합성 최종 확인"])
		self.assertNotIn("체크포인트: 계획 · 합성 이전 확인", out)
		self.assertEqual(self.files(identity), snapshot)

	def test_cli_show_preserves_partial_checkpoint_and_whitespace(self):
		identity = self.new()["id"]
		for step, detail in (("", "설명 원문"), ("단계 원문", ""), (" 단계 원문 ", " 설명 원문 ")):
			with self.subTest(step=step, detail=detail):
				project.set_checkpoint(identity, step, detail)
				snapshot = self.files(identity)
				code, out, err = self.cli("show", identity)
				self.assertEqual((code, err), (0, ""))
				self.assertEqual([line for line in out.splitlines() if line.startswith("체크포인트:")],
					[f"체크포인트: {step} · {detail}"])
				self.assertEqual(self.files(identity), snapshot)

	def test_cli_show_confirmed_spec_tasks_and_used_budget(self):
		identity = self.confirmed()["id"]
		folder = self.hub / "projects" / identity
		project.add_task(identity, "첫 작업", str(folder / "one.md"))
		project.add_task(identity, "둘째 작업", str(folder / "two.md"), ["t1"])
		project.update_task(identity, "t1", status="완료")
		project.add_usage(identity, 1, 1.5)
		snapshot = self.files(identity)
		code, out, err = self.cli("show", identity)
		self.assertEqual((code, err), (0, ""))
		for value in ("상태: 명세 확정", "제목: 합성 프로젝트", "명세 빈칸: 없음", "작업 목록:",
				"- t1 [완료] 첫 작업", "- t2 [대기] 둘째 작업", "예산 사용: 호출 1/3회, 시간 1.5/120초"):
			self.assertIn(value, out.splitlines())
		self.assertEqual(self.files(identity), snapshot)

	def test_cli_stdin_and_check_collect_all_skill_errors(self):
		for request in (("-",), ()):
			code, out, err = self.cli("new", "--dept", "xyz", "--title", "표준입력", *request, stdin="표준입력 요청")
			self.assertEqual(code, 0)
			self.assertEqual(project.load(out.strip())["request_text"], "표준입력 요청")
		code, out, err = self.cli("check")
		self.assertEqual(code, 1)
		self.assertIn("xyz", out + err)
		folder = self.hub / "skills" / "xyz"
		folder.mkdir(parents=True)
		(folder / "SKILL.md").write_text("---\nname: xyz\ndescription: 합성 스킬\n---\n합성 본문", encoding="utf-8")
		self.assertEqual(self.cli("check")[0], 0)
		with (self.hub / "departments.toml").open("a") as stream:
			stream.write('skills = ["first", "second"]\nreview_skill = "third"\n')
		code, out, err = self.cli("check")
		self.assertEqual(code, 1)
		for name in ("first", "second", "third"):
			self.assertIn(name, out + err)

	def test_cli_module_entry_point_and_multiline_title(self):
		project.new(self.department, "첫 줄\n둘째 줄\t끝", "합성 요청")
		code, out, err = self.cli("list")
		self.assertEqual(code, 0)
		self.assertEqual(len(out.splitlines()), 1)
		self.assertIn("첫 줄\\n둘째 줄\\t끝", out)
		# 모듈 진입점도 같은 CLI를 실행하며 별도 프로세스는 만들지 않는다.
		with mock.patch("sys.argv", ["agency", "list"]), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
			with mock.patch.dict("sys.modules"):
				import sys
				sys.modules.pop("agency.__main__", None)
				runpy.run_module("agency", run_name="__main__")
		self.assertEqual(caught.exception.code, 0)
