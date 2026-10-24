"""회사 문장은 SQL 조회와 화면 JSON 양쪽에서 제외한다."""

import json
import sqlite3

from office import agents, ledger
from office.common import text
from test_office_roles import RoleCase


class OfficeFollowupsTest(RoleCase):
	def test_schedule_tags_dates_undated_group_and_company_counts(self):
		created = self.now.isoformat()
		self.conn.executemany("insert into followups values (?,?,?,?,?,?,?)", [
			("a", "a", "personal", "p03", "블로그 후속 확인", "2026-10-26", created),
			("b", "b", "personal", "p04", "기한 없는 후속 확인", "", created),
			("c", "c", "company", "p01", "acme-private-action", "2026-10-25", created),
			("d", "c", "company", "p01", "acme-private-action-2", "2026-10-25", created)])
		self.conn.executemany("insert into work_records values (?,?,?,?,?)", [
			("a", "personal", "p03", "개인 결정 요약", created),
			("c", "company", "p01", "acme-private-decision", created)])
		self.conn.commit()
		before = self.snapshot()
		state = self.state()
		self.assertEqual(before, self.snapshot())
		items = [item for item in state["schedule"]["items"] if item["kind"] == "followup"]
		self.assertEqual([item["due"] for item in items], ["2026-10-25", "2026-10-26", ""])
		self.assertEqual([(item["title"], item["source"]) for item in items], [
			("p01 · 후속 2건", "회사"), ("블로그 후속 확인", "블로그"), ("기한 없는 후속 확인", "개인")])
		payload = json.dumps(state, ensure_ascii=False)
		for private in ("acme-private-action", "acme-private-decision"):
			self.assertNotIn(private, payload)
		self.assertIn("개인 결정 요약", payload)

	def test_company_run_summary_is_excluded_and_personal_run_is_sanitized(self):
		for rid, repo, sentence in (("2026-10-23-005", self.company_paths[0], "acme-private-run"),
				("2026-10-23-006", self.user, "개인 실행 결정")):
			path = self.hub / "runs" / rid / "run.json"
			data = json.loads(path.read_text())
			data.update(repo=str(repo), handoff={"summary": sentence})
			path.write_text(json.dumps(data))
		board = self.state()["board"]["items"]
		self.assertNotIn("acme-private-run", json.dumps(board))
		self.assertEqual([item["text"] for item in board], ["개인 실행 결정"])

	def test_company_query_reads_only_project_date_count(self):
		queries = []
		self.conn.row_factory = sqlite3.Row
		self.conn.set_trace_callback(queries.append)
		ledger.followups(self.conn)
		company_queries = [q for q in queries if "cls='company'" in q]
		self.assertTrue(company_queries)
		self.assertTrue(all("sentence" not in q for q in company_queries))

	def test_run_goal_is_sanitized_and_exported_only_for_public_classification(self):
		path = self.hub / "runs/2026-10-23-006/run.json"
		data = json.loads(path.read_text())
		token = "ghp_" + "a" * 36
		goal = "  합성\n 목표 /w/private/material " + token
		for repo, public in ((self.user, True), (self.company_paths[0], False), (self.root / "unknown", False)):
			data.update(repo=str(repo), request={"goal": goal}, started=self.now.isoformat())
			path.write_text(json.dumps(data))
			before = self.snapshot()
			item = agents.read_runs(self.hub)["items"][0]
			self.assertEqual(item["public"], public)
			if public:
				self.assertEqual(item["goal"], text(goal))
				self.assertNotIn(token, item["goal"])
				self.assertNotIn("/w/private/material", item["goal"])
				self.assertEqual(item["started"], self.now.isoformat())
			else:
				self.assertNotIn("goal", item)
				self.assertNotIn("goal_hash", item)
				self.assertNotIn("repo", item)
				self.assertNotIn("합성 목표", json.dumps(self.state(), ensure_ascii=False))
			self.assertEqual(before, self.snapshot())

	def test_missing_followups_only_marks_schedule_unavailable(self):
		self.conn.execute("drop table followups")
		self.conn.commit()
		state = self.state()
		self.assertEqual(state["schedule"]["status"], "확인 불가")
		self.assertEqual(len(state["people"]), 23)
		self.assertEqual(state["usage"]["plans"]["status"], "정상")
