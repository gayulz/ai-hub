"""presence의 신선도와 오케스트레이션 우선순위를 확인한다."""

import json
from datetime import timedelta

from collector.hooks import session_hash
from test_office import OfficeCase


class C2OfficeTest(OfficeCase):
	def presence(self, tool, status="일하는 중", age=60):
		path = self.hub / f"run/presence/{tool}.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"status": status, "at": (self.now - timedelta(seconds=age)).isoformat(),
			"session_hash": session_hash(f"{tool}:s01"), "cls": "company", "project_id": "p01",
			"cwd": "/w/acme-private", "text": "외부 출력 금지 합성 자료"}))
		return path

	def test_only_fresh_working_presence_is_active_and_private_fields_are_not_copied(self):
		for tool in ("claude", "codex", "gemini"):
			for status, age, expected in (("일하는 중", 60, "일하는 중"), ("일하는 중", 601, "대기"),
					("일하는 중", -1, "대기"), ("대기", 1, "대기"), ("끝남", 1, "대기")):
				with self.subTest(tool=tool, status=status, age=age):
					self.presence(tool, status, age)
					before = self.snapshot()
					state = self.state()
					self.assertEqual(self.people(state)[tool]["status"], expected)
					self.assertEqual(before, self.snapshot())
					text = json.dumps(state, ensure_ascii=False)
					self.assertNotIn("/w/acme-private", text)
					self.assertNotIn("외부 출력 금지", text)

	def test_orchestration_role_wins_over_presence(self):
		self.presence("codex", "끝남")
		self.hold()
		self.assertEqual(self.people(self.state())["codex"]["status"], "구현 중")
		self.run_record("2026-10-23-006", "작업 중", "review", "acme-claude")
		self.presence("claude")
		self.assertEqual(self.people(self.state())["claude"]["status"], "검토 중")

	def test_broken_and_symlinked_presence_is_idle_without_reading_target(self):
		path = self.presence("gemini")
		for text in ("{", "[]", '{"at":"broken","status":"일하는 중"}'):
			path.write_text(text)
			self.assertEqual(self.people(self.state())["gemini"]["status"], "대기")
		other = self.root / "outside.json"
		other.write_text("외부 원본")
		path.unlink()
		path.symlink_to(other)
		self.assertEqual(self.people(self.state())["gemini"]["status"], "대기")
		self.assertEqual(other.read_text(), "외부 원본")
