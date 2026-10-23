"""예약 계산·작업별 조회 실패·로그 비공개 처리를 확인한다."""

import os
from datetime import datetime, timedelta
from unittest import mock

from office.jobs import next_run
from test_office import OfficeCase


class OfficeJobsTest(OfficeCase):
	def test_calendar_intervals_and_today_remaining_jobs(self):
		self.assertEqual(next_run({"Minute": 10}, self.now), (self.now+timedelta(minutes=10)).isoformat(timespec="seconds"))
		self.assertEqual(next_run([{"Hour": 12, "Minute": 30}, {"Hour": 23, "Minute": 30}], self.now),
			self.now.replace(hour=23, minute=30).isoformat(timespec="seconds"))
		sunday = datetime(2026, 10, 25, 12).astimezone()
		for weekday in (0, 7):
			self.assertEqual(next_run({"Weekday": weekday, "Hour": 13, "Minute": 0}, sunday), sunday.replace(hour=13).isoformat(timespec="seconds"))
		for bad in ({}, [], {"Minute": 99}, {"Hour": True}, {"xyz": 1}, {"Month": 2, "Day": 30}):
			with self.subTest(bad=bad), self.assertRaises(ValueError):
				next_run(bad, self.now)
		self.assertEqual(len(self.state()["board"]["schedule"]["jobs"]), 3)

	def test_launch_failure_preserves_schedule_and_log(self):
		with mock.patch.dict(os.environ, {"ACME_LAUNCH_EXIT": "1", "ACME_BLOG_EXIT": "1"}):
			state = self.state()
		self.assertEqual(self.people(state)["blog"]["status"], "꺼짐")
		for job in state["jobs"]:
			self.assertEqual(job["status"], "확인 불가")
			self.assertEqual(job["schedule"]["status"], "정상")
			self.assertEqual(job["log"]["last_line"], "기록 4건 · 실패 0건")
		with mock.patch.dict(os.environ, {"ACME_BLOG_EXIT": "2"}):
			self.assertEqual(self.people(self.state())["blog"]["status"], "확인 불가")

	def test_missing_plist_and_log_do_not_break_other_jobs(self):
		(self.user / "Library/LaunchAgents/com.acme.ai-hub-collector.plist").write_bytes(b"<plist><dict>")
		(self.hub / "logs/collector.out.log").write_bytes(b"\xff")
		jobs = self.state()["jobs"]
		self.assertEqual(jobs[0]["schedule"]["status"], "확인 불가")
		self.assertEqual(jobs[0]["log"]["status"], "확인 불가")
		self.assertEqual(jobs[1]["schedule"]["status"], "정상")
		self.assertEqual(jobs[1]["log"]["status"], "정상")
