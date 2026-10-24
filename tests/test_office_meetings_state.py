"""회의 기록의 읽기 전용 상태·필드 제한·부분 오류 격리를 검사한다."""

import copy
import json
import stat
from unittest import mock

from office import meetings
from orchestrator import meeting_prompts, meeting_record
from test_office_meetings import CID, MID, MeetingCase, meeting_state, write_meeting


class OfficeMeetingsStateTest(MeetingCase):
	def snapshot(self):
		return {str(path.relative_to(self.hub)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
			for path in self.hub.rglob("*") if path.is_file() and not path.is_symlink()}

	def test_empty_state_and_roles_match_engine_configured_candidates(self):
		before = self.snapshot()
		state = meetings.read_state(self.hub)
		self.assertEqual(state["meetings"], {"status": "정상", "items": []})
		self.assertEqual(state["questions"], {"status": "정상", "items": []})
		configured = meeting_prompts.configuration(meetings.Rules())
		self.assertEqual(state["meeting_roles"], [{"key": k, "name": v["name"]} for k, v in meeting_prompts.ROLES.items() if k in configured])
		self.assertEqual({r["key"] for r in state["meeting_roles"]}, {"backend", "qa", "plan", "design"})
		self.assertEqual(before, self.snapshot())

	def test_latest_twenty_meetings_but_all_pending_cards_and_only_public_columns(self):
		for n in range(25):
			state = meeting_state(identity="m-" + format(n, "032x"))
			state.update(created=f"2026-10-24T10:{n:02d}:00+09:00", active_seconds=90.5, calls=[{"prompt": "합성 숨길 발언"}],
				absent=[{"role": "qa", "reason": "합성 숨길 부재 이유"}])
			write_meeting(self.hub, state)
		before = self.snapshot()
		with mock.patch.object(meeting_record.Store, "list_states", autospec=True, wraps=None) as listing:
			listing.side_effect = lambda store: [store.read("m-" + format(n, "032x")) for n in range(25)]
			value = meetings.read_state(self.hub)
			listing.assert_called_once()
		self.assertEqual(before, self.snapshot())
		self.assertEqual(value["meetings"]["status"], "정상")
		items = value["meetings"]["items"]
		self.assertEqual(len(items), 20)
		self.assertEqual([item["id"] for item in items], ["m-" + format(n, "032x") for n in range(24, 4, -1)])
		self.assertEqual(set(items[0]), {"id", "status", "agenda", "target", "participants", "absent", "created", "active_seconds", "calls"})
		self.assertEqual(items[0]["participants"], ["backend", "qa"])
		self.assertEqual(items[0]["absent"], ["qa"])
		self.assertEqual((items[0]["calls"], items[0]["active_seconds"]), (1, 90.5))
		self.assertEqual(len(value["questions"]["items"]), 25)
		card = value["questions"]["items"][0]
		self.assertEqual(card, {"meeting": items[0]["id"], "card": CID, "turn": 1, "questions": state["cards"][0]["questions"]})
		self.assertNotIn("합성 숨길", json.dumps(value, ensure_ascii=False))

	def test_masking_covers_full_agenda_before_sixty_chars_and_question_fields(self):
		state = meeting_state()
		sensitive = "acme " + "ghp_" + "a" * 36 + " " + "p01" + "@" + "acme.invalid /" + "home/p01/private"
		state["agenda"] = sensitive + " 나머지" * 100
		state["target"] = "xyz"
		q = state["cards"][0]["questions"][0]
		q.update(text=sensitive, reason=sensitive)
		for option in q["options"]:
			for key in ("label", "difference", "reply"):
				option[key] = sensitive
		write_meeting(self.hub, state)
		value = meetings.read_state(self.hub)
		self.assertEqual(value["meetings"]["status"], "정상")
		self.assertEqual(len(value["meetings"]["items"][0]["agenda"]), 60)
		encoded = json.dumps(value, ensure_ascii=False)
		for raw in ("acme", "xyz", "ghp_", "@", "/home/", "private"):
			self.assertNotIn(raw, encoded)
		self.assertIn("[보안값]", encoded)

	def test_only_current_waiting_cards_are_exported(self):
		for n, (status, card_status) in enumerate((("participants_pending", "waiting"), ("questions_pending", "waiting"),
				("answers_pending", "answered"), ("running", "confirmed"), ("completed", "waiting"))):
			state = meeting_state(status, "m-" + format(n, "032x"))
			state["cards"][0]["status"] = card_status
			if n == 1:
				state["cards"].insert(0, {**copy.deepcopy(state["cards"][0]), "id": "q-" + "c" * 32})
			write_meeting(self.hub, state)
		value = meetings.read_state(self.hub)
		self.assertEqual(len(value["meetings"]["items"]), 5)
		self.assertEqual([q["card"] for q in value["questions"]["items"]], [CID])

	def test_corrupt_records_preserve_healthy_meetings_and_never_rewrite_permissions(self):
		path = write_meeting(self.hub, meeting_state())
		path.chmod(0o644)
		bad = write_meeting(self.hub, meeting_state(identity="m-" + "c" * 32))
		for content in ("{", "[" * 1500, "{}"):
			bad.write_text(content)
			before = self.snapshot()
			value = meetings.read_state(self.hub)
			self.assertEqual(value["meetings"]["status"], "확인 불가")
			self.assertEqual(value["questions"]["status"], "확인 불가")
			self.assertEqual([item["id"] for item in value["meetings"]["items"]], [MID])
			self.assertEqual(len(value["questions"]["items"]), 1)
			self.assertEqual(len(value["meeting_roles"]), 4)
			self.assertEqual(before, self.snapshot())
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

	def test_unsafe_paths_and_read_failures_only_disable_meeting_groups(self):
		folder = self.hub / "meetings"
		folder.symlink_to(self.root, target_is_directory=True)
		value = meetings.read_state(self.hub)
		self.assertEqual(value["meetings"]["status"], "확인 불가")
		self.assertEqual(value["meetings"]["items"], [])
		folder.unlink()
		folder.mkdir()
		with mock.patch.object(meeting_record, "directory", side_effect=PermissionError):
			self.assertEqual(meetings.read_state(self.hub)["questions"]["status"], "확인 불가")
		with mock.patch.object(meeting_record.Store, "list_states", side_effect=RecursionError):
			self.assertEqual(meetings.read_state(self.hub)["meetings"]["status"], "확인 불가")

	def test_malformed_projection_fields_are_isolated_and_do_not_echo_values(self):
		for field, value in (("turn", "합성 숨길 본문"), ("turn", True), ("turn", 6), ("question_id", "acme")):
			state = meeting_state()
			if field == "question_id":
				state["cards"][0]["questions"][0]["id"] = value
			else:
				state["cards"][0][field] = value
			write_meeting(self.hub, state)
			result = meetings.read_state(self.hub)
			self.assertEqual(result["meetings"]["status"], "확인 불가")
			self.assertNotIn("합성 숨길 본문", json.dumps(result, ensure_ascii=False))
			self.assertNotIn("acme", json.dumps(result, ensure_ascii=False))

	def test_broken_role_config_does_not_hide_readable_meetings(self):
		write_meeting(self.hub, meeting_state())
		(self.hub / "plans.toml").write_text("{")
		result = meetings.read_state(self.hub)
		self.assertEqual(result["meeting_roles"], [])
		self.assertEqual(result["meetings"]["status"], "정상")
		self.assertEqual(len(result["meetings"]["items"]), 1)

	def test_latest_order_compares_instants_across_timezones(self):
		one = meeting_state(identity="m-" + "c" * 32)
		one["created"] = "2026-10-24T09:00:00+09:00"
		two = meeting_state()
		two["created"] = "2026-10-24T01:00:00+00:00"
		write_meeting(self.hub, one)
		write_meeting(self.hub, two)
		self.assertEqual(meetings.read_state(self.hub)["meetings"]["items"][0]["id"], MID)
