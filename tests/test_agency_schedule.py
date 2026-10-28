"""임시 원장과 고정 시각으로 정기 접수의 중복과 저장소 연결을 검사한다."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agency import acceptance, planner, project, runner, schedule, tick
from agency.departments import Department


class ScheduleTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve()
		self.stamp = datetime(2030, 1, 1, tzinfo=timezone.utc)
		self.item = {"repo": "second", "every_days": 7, "title": "정기 점검", "request": "합성 점검 요청"}
		self.department = Department("sample", "합성 부서", "합성 범위", ("first", "second"), ("local",),
			True, "sample", (), "", (), ("대상은?", "범위는?"), {"calls": 5, "minutes": 5},
			schedule=(self.item,))
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch.object(project, "now", side_effect=lambda: self.stamp),
				mock.patch.object(tick, "now", side_effect=lambda: self.stamp),
				mock.patch.object(tick, "load_departments", side_effect=lambda: {"sample": self.department}),
				mock.patch("agency.intake.step"), mock.patch("agency.hooks.before_tick", return_value=[])):
			patch.start()
			self.addCleanup(patch.stop)

	def test_tick_records_answers_and_blocks_duplicates_until_period_after_cancel(self):
		first = tick.tick()
		self.assertEqual(len(first["projects"]), 1)
		identity = first["projects"][0]["id"]
		row = project.load(identity)
		self.assertEqual((row["schedule_repo"], row["title"]), ("second", self.item["title"]))
		self.assertEqual([(item["question"], item["answer"]) for item in row["answers"]],
			[(question, self.item["request"]) for question in self.department.required_questions])
		events = (project._folder(identity) / "events.jsonl").read_text(encoding="utf-8").splitlines()
		self.assertEqual(json.loads(events[0])["kind"], "schedule")
		self.assertEqual(len(tick.tick()["projects"]), 1)
		project.cancel(identity)
		self.stamp += timedelta(days=6, hours=23, minutes=59)
		self.assertEqual(len(tick.tick()["projects"]), 1)
		self.stamp += timedelta(minutes=1)
		self.assertEqual(len(tick.tick()["projects"]), 2)
		self.assertEqual(len(tick.tick()["projects"]), 2)

	def test_active_blocked_project_and_one_per_department_each_tick(self):
		other = {**self.item, "title": "다른 점검"}
		self.department = replace(self.department, schedule=(self.item, other))
		first = tick.tick()["projects"]
		self.assertEqual(len(first), 1)
		project.transition(first[0]["id"], "막힘", reason="합성 막힘")
		second = tick.tick()["projects"]
		self.assertEqual(len(second), 2)
		self.assertEqual(len(tick.tick()["projects"]), 2)
		self.assertEqual({row["title"] for row in project.list_projects()}, {self.item["title"], other["title"]})

	def test_manual_project_matches_only_its_actual_target(self):
		project.new(self.department, self.item["title"], "합성 수동 요청")
		rows = project.list_projects()
		self.assertEqual(len(schedule.receive({"sample": self.department}, rows, self.stamp)), 1)
		self.assertEqual(schedule.target(rows[0], self.department), "first")
		self.assertEqual(schedule.target(rows[1], self.department), "second")

	def test_latest_completion_time_and_corrupt_timestamp_fail_closed(self):
		row = project.new(self.department, self.item["title"], self.item["request"], schedule_repo="second")
		project.cancel(row["id"])
		finished = project.load(row["id"])
		finished["updated"] = (self.stamp - timedelta(days=100)).isoformat()
		self.assertEqual(schedule.receive({"sample": self.department}, [finished], self.stamp), [])
		finished["finished_at"] = "broken"
		with self.assertRaisesRegex(ValueError, "완료 시각"):
			schedule.receive({"sample": self.department}, [finished], self.stamp)

	def test_runner_planner_and_review_resolve_the_scheduled_repository(self):
		row = project.new(self.department, self.item["title"], self.item["request"], schedule_repo="second")
		with mock.patch.object(planner, "load_departments", return_value={"sample": self.department}), \
				mock.patch.object(runner, "load_departments", return_value={"sample": self.department}), \
				mock.patch("orchestrator.targets.target", side_effect=RuntimeError("저장소 선택")) as target:
			with self.assertRaisesRegex(RuntimeError, "저장소 선택"):
				runner.run(row["id"])
			target.assert_called_with("second")
			project.update_spec(row["id"], goal="합성", target="합성", out_of_scope="합성", done_criteria=["합성"])
			project.confirm_spec(row["id"], self.department)
			with self.assertRaisesRegex(RuntimeError, "저장소 선택"):
				planner.plan(row["id"])
			self.assertEqual(target.call_count, 2)
			target.assert_called_with("second")
			with mock.patch.object(acceptance, "load_departments", return_value={"sample": self.department}), \
					mock.patch.object(acceptance, "_ready"):
				with self.assertRaisesRegex(RuntimeError, "저장소 선택"):
					acceptance.review(row["id"])
				target.assert_called_with("second")
