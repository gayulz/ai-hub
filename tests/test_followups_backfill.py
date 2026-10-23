"""소급은 승인된 대화가 반영될 때만 후속 일을 원장에 넣는다."""

from collector.backfill_store import Store
from test_backfill_support import BackfillCase
from test_followups import record


class FollowupsBackfillTest(BackfillCase):
	def test_prepare_does_not_schedule_and_apply_records_company_class(self):
		self.sources(cls="company")
		self.codex(answer=record("월요일 재확인", decision="회사 합성 결정"))
		self.assertEqual(self.prepare(), 0, self.stderr.getvalue())
		bid = self.batch()[0]["id"]
		store = Store(self.hub / "ledger.db", readonly=True)
		self.assertEqual(store.conn.execute("select count(*) from followups").fetchone()[0], 0)
		store.close()
		self.assertEqual(self.command("--apply", bid), 0, self.stderr.getvalue())
		store = Store(self.hub / "ledger.db", readonly=True)
		try:
			row = store.conn.execute("select * from followups").fetchone()
			self.assertEqual((row["project"], row["cls"], row["due"]), ("p01", "company", "2026-08-03"))
		finally:
			store.close()
