"""공개 실행의 알림용 제목·결과와 대행사 실행 구분을 확인한다."""

import json

from office import agents
from office.common import text
from test_office_roles import RoleCase


class OfficeAgentsTest(RoleCase):
	def test_public_run_title_reason_are_sanitized_and_private_run_does_not_export_them(self):
		path = self.hub / "runs/2026-10-23-006/run.json"
		data = json.loads(path.read_text())
		secret = "ghp_" + "a" * 36
		title = "합성 변경 /w/private/material " + secret
		reason = "독립 검토\n 통과 /w/private/result " + secret
		for repo, public in ((self.user, True), (self.company_paths[0], False), (self.root / "unknown", False)):
			with self.subTest(public=public):
				data.update(repo=str(repo), request={"goal": "합성 목표", "commit_message": title}, end_reason=reason)
				path.write_text(json.dumps(data))
				before = self.snapshot()
				item = agents.read_runs(self.hub)["items"][0]
				self.assertEqual(item["public"], public)
				if public:
					self.assertEqual((item["title"], item["reason"]), (text(title), text(reason)))
					self.assertNotIn(secret, json.dumps(item))
				else:
					self.assertNotIn("title", item)
					self.assertNotIn("reason", item)
				self.assertEqual(before, self.snapshot())

	def test_title_keeps_only_first_line_and_falls_back_to_goal(self):
		path = self.hub / "runs/2026-10-23-006/run.json"
		data = json.loads(path.read_text())
		for message, expected in (("합성 제목\n자세한 본문", "합성 제목"), ("", "합성 목표"), (None, "합성 목표")):
			request = {"goal": "합성 목표\n자세한 요청"}
			if message is not None:
				request["commit_message"] = message
			data.update(repo=str(self.user), request=request)
			path.write_text(json.dumps(data))
			item = agents.read_runs(self.hub)["items"][0]
			self.assertEqual(item["title"], expected)
			self.assertEqual(item["reason"], "")

	def test_agency_flag_uses_only_base_branch_prefix_including_non_public_runs(self):
		path = self.hub / "runs/2026-10-23-006/run.json"
		data = json.loads(path.read_text())
		for repo in (self.user, self.company_paths[0]):
			for branch, expected in (("agency/p-20261027-001", True), ("main", False), ("feature/agency/a", False), ("", False)):
				data.update(repo=str(repo), request={"goal": "합성 목표", "base_branch": branch})
				path.write_text(json.dumps(data))
				item = agents.read_runs(self.hub)["items"][0]
				self.assertEqual(item.get("agency", False), expected)
