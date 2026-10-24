"""합성 대화의 마지막 작업 기록·날짜·반복 기록·실패 경계를 확인한다."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from collector import config, pipeline, vault, work_records
from collector.ledger import Ledger
from collector.sessions import Session, Turn


def record(*lines, decision="합성 결정"):
	return "## 작업 기록\n- 결정: " + decision + "\n" + "\n".join("- 남은 일: " + line for line in lines)


class FollowupsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.hub, self.vault = self.root / "hub", self.root / "vault"
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub), "HOME": str(self.root / "acme")})
		patch.start()
		self.addCleanup(patch.stop)
		self.ledger = Ledger(self.hub / "ledger.db")
		self.addCleanup(self.ledger.close)
		self.now = datetime(2026, 10, 24, 12).astimezone()
		self.sources = config.Sources(self.vault,
			(config.Rule("/w/acme", "company", "", False), config.Rule("/w/xyz", "personal", "", False)),
			(config.Repo("/w/acme", "company", "acme", (), "p01"), config.Repo("/w/xyz", "personal", "xyz", (), "p03")), ())
		self.session = Session("codex", "s01", "/w/xyz", self.now - timedelta(hours=2),
			self.now - timedelta(hours=1), [Turn("user", "합성 질문"), Turn("assistant", record("월요일 확인"), "final")])
		for folder in vault.landing_dirs(self.vault):
			folder.mkdir(parents=True)

	def rows(self):
		return [dict(row) for row in self.ledger.conn.execute("select * from followups order by key")]

	def runner(self, dry_run=False):
		runner = pipeline.Pipeline(self.sources, None, self.ledger, dry_run)
		runner.now, runner.today = self.now, self.now.date()
		return runner

	def test_korean_and_numeric_dates_are_relative_to_the_record(self):
		for sentence, expected in (("오늘 확인", "2026-10-24"), ("내일 확인", "2026-10-25"),
				("월요일 확인", "2026-10-26"), ("10/26 확인", "2026-10-26"),
				("11월 3일 확인", "2026-11-03"), ("1/2 확인", "2027-01-02"),
				("기한 없이 확인", ""), ("2/30 확인", "")):
			with self.subTest(sentence=sentence):
				self.assertEqual(work_records.due_hint(sentence, self.now.date()), expected)
		self.assertEqual(work_records.due_hint("내일", date(2026, 12, 31)), "2027-01-01")
		self.assertEqual(work_records.due_hint("다음 월요일", date(2026, 10, 26)), "2026-11-02")

	def test_iso_dates_preserve_explicit_year_and_validate_calendar_dates(self):
		for sentence, expected in (("2026-11-05 확인", "2026-11-05"),
				("기한은 2026-01-02", "2026-01-02"), ("2027-01-02까지 확인", "2027-01-02"),
				("2028-02-29 확인", "2028-02-29"), ("2026-02-29 확인", ""),
				("2026-11-31 확인", ""), ("2026-00-05 확인", ""),
				("12026-11-05 확인", ""), ("2026-11-051 확인", "")):
			with self.subTest(sentence=sentence):
				self.assertEqual(work_records.due_hint(sentence, self.now.date()), expected)

	def test_iso_due_is_saved_to_the_followup_ledger(self):
		self.session.turns.append(Turn("assistant", record("2026-11-05 확인"), "final"))
		work_records.save(self.ledger, self.session, self.sources, "personal")
		self.assertEqual([(row["sentence"], row["due"]) for row in self.rows()], [("2026-11-05 확인", "2026-11-05")])

	def test_only_last_complete_record_fields_and_no_empty_or_fenced_tasks(self):
		self.session.turns += [Turn("assistant", record("오늘 확인", "(있으면)", "", "없음", "오늘 확인", "기한 없이 확인")
			+ "\n```text\n- 남은 일: 코드 예제\n```", "final"),
			Turn("assistant", record("해석 중"), "analysis"), Turn("assistant", record("미완료"), complete=False),
			Turn("user", record("사용자 붙여넣기"))]
		work_records.save(self.ledger, self.session, self.sources, "personal")
		self.assertEqual([(r["sentence"], r["due"]) for r in self.rows()], [("오늘 확인", "2026-10-24"), ("기한 없이 확인", "")])

	def test_repeat_updates_and_removal_replace_the_conversations_snapshot(self):
		self.runner().handle_session(self.session)
		self.assertEqual((self.rows()[0]["project"], self.rows()[0]["cls"]), ("p03", "personal"))
		self.session.turns.append(Turn("assistant", record("내일 재확인")))
		self.runner().handle_session(self.session)
		self.runner().handle_session(self.session)
		self.assertEqual(len(self.rows()), 1)
		self.assertEqual(self.rows()[0]["sentence"], "내일 재확인")
		self.session.turns.append(Turn("assistant", record("없음")))
		self.runner().handle_session(self.session)
		self.assertEqual(self.rows(), [])
		self.assertEqual(self.ledger.conn.execute("select count(*) from work_records").fetchone()[0], 1)

	def test_existing_record_populates_new_table_without_a_second_ai_call(self):
		self.ledger.put(self.session.key, "codex", "s01", "personal", pipeline.digest(self.session), "recorded")
		with mock.patch("collector.summarize.call") as call:
			self.runner().handle_session(self.session)
		call.assert_not_called()
		self.assertEqual(len(self.rows()), 1)

	def test_dry_run_and_failed_vault_write_do_not_create_followups(self):
		self.runner(dry_run=True).handle_session(self.session)
		self.assertEqual(self.rows(), [])
		with mock.patch.object(vault, "upsert_block", side_effect=OSError("합성 실패")):
			with self.assertRaises(OSError):
				self.runner().handle_session(self.session)
		self.assertEqual(self.rows(), [])
		self.assertFalse((self.hub / "run/collector-active.json").exists())

	def test_company_project_and_processing_metadata_never_contain_body(self):
		self.session.cwd = "/w/acme/src"
		with work_records.processing(self.session, self.sources, "company"):
			data = json.loads((self.hub / "run/collector-active.json").read_text())
			self.assertEqual((data["cls"], data["project_id"]), ("company", "p01"))
			self.assertEqual(set(data), {"pid", "cls", "project_id", "at"})
		self.runner().handle_session(self.session)
		self.assertEqual((self.rows()[0]["project"], self.rows()[0]["cls"]), ("p01", "company"))

	def test_schema_addition_keeps_old_session_rows(self):
		self.ledger.put("codex:old", "codex", "old", "personal", "xyz", "recorded")
		self.ledger.conn.execute("drop table followups")
		self.ledger.conn.execute("drop table work_records")
		other = Ledger(self.hub / "ledger.db")
		self.addCleanup(other.close)
		self.assertEqual(other.get("codex:old")["hash"], "xyz")
		self.assertEqual(other.conn.execute("select count(*) from followups").fetchone()[0], 0)
