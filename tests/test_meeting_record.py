"""회의 저장의 권한·원자 교체·경로·링크 경계."""

import json
import os
import stat
from unittest import mock

from test_meeting import MeetingCase, question


class MeetingRecordTest(MeetingCase):
	def test_readonly_listing_skips_bad_ids_links_and_shapes_without_chmod_or_locking(self):
		self.ai.analysis_questions = [question()]
		state = self.analyze()
		root = self.hub / "meetings"
		folder = root / state["id"]
		(folder / "meeting.json").chmod(0o400)
		folder.chmod(0o500)
		before = {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
		with self.engine.store.locked(state["id"]) as record:
			# 목록 조회는 이미 잡힌 회의 잠금을 다시 요청하지 않는다.
			self.assertEqual(self.engine.store.list_states(), [state])
		folder.chmod(0o500)
		(folder / "meeting.json").chmod(0o400)
		self.assertEqual(self.engine.store.read(state["id"]), state)
		after = {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
		self.assertEqual(before, after)
		folder.chmod(0o700)
		(root / "invalid-id").mkdir()
		bad = root / ("m-" + "e" * 32)
		bad.mkdir()
		(bad / "meeting.json").write_text("[]")
		linked = root / ("m-" + "f" * 32)
		linked.symlink_to(folder, target_is_directory=True)
		self.assertEqual(self.engine.store.list_states(), [state])
		for data in ("[" * 2000, "{}", json.dumps({**state, "cards": "bad"}),
				json.dumps({**state, "summary": "bad"})):
			(bad / "meeting.json").write_text(data)
			self.assertEqual(self.engine.store.list_states(), [state])
		(bad / "meeting.json").unlink()
		(bad / "meeting.json").symlink_to(folder / "meeting.json")
		self.assertEqual(self.engine.store.list_states(), [state])

	def test_private_modes_and_self_contained_minutes(self):
		state = self.start(self.analyze())
		folder = self.hub / "meetings" / state["id"]
		for path in (folder.parent, folder):
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
		for path in folder.iterdir():
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		text = (folder / "minutes.md").read_text()
		for value in (state["agenda"], "backend", "qa", "round1", "round2", "conclusion", "completed", "서로 다른 관점"):
			self.assertIn(value, text)
		self.assertEqual(json.loads((folder / "meeting.json").read_text()), state)

	def test_path_ids_and_symlinked_root_folder_state_or_lock_are_refused(self):
		for identity in ("../x", "/tmp/x", "a/b", "a\\b", "", ".", "m-" + "x" * 200):
			with self.assertRaises((ValueError, OSError)):
				self.engine.status(identity)
		outside = self.root / "outside"
		outside.mkdir()
		(self.hub / "meetings").symlink_to(outside)
		with self.assertRaises((ValueError, OSError)):
			self.analyze()
		(self.hub / "meetings").unlink()
		state = self.analyze()
		folder = self.hub / "meetings" / state["id"]
		for name in ("meeting.json", ".lock", "minutes.md"):
			path = folder / name
			original = path.read_bytes()
			path.unlink()
			path.symlink_to(outside / name)
			with self.assertRaises((ValueError, OSError)):
				self.start(state)
			path.unlink()
			path.write_bytes(original)
		self.assertEqual(list(outside.iterdir()), [])

	def test_failed_atomic_replace_preserves_previous_json_and_removes_temporary_file(self):
		state = self.analyze()
		folder = self.hub / "meetings" / state["id"]
		original = (folder / "meeting.json").read_bytes()
		with self.engine.store.locked(state["id"]) as record:
			with mock.patch("orchestrator.meeting_record.os.replace", side_effect=OSError("합성 오류")):
				with self.assertRaises(OSError):
					record.save({**state, "failure": "변경"})
		self.assertEqual((folder / "meeting.json").read_bytes(), original)
		self.assertEqual({p.name for p in folder.iterdir()}, {"meeting.json", "minutes.md", ".lock"})

	def test_same_meeting_lock_and_hardlinks_are_refused(self):
		state = self.analyze()
		with self.engine.store.locked(state["id"]):
			with self.assertRaises(RuntimeError):
				self.engine.resume(state["id"])
		path = self.hub / "meetings" / state["id"] / "meeting.json"
		os.link(path, self.root / "linked.json")
		with self.assertRaises((ValueError, OSError)):
			self.engine.status(state["id"])

	def test_meeting_directory_link_and_parent_home_link_are_refused(self):
		state = self.analyze()
		folder = self.hub / "meetings" / state["id"]
		relocated = folder.with_name("relocated")
		folder.rename(relocated)
		folder.symlink_to(relocated, target_is_directory=True)
		with self.assertRaises((ValueError, OSError)):
			self.engine.status(state["id"])
		alias = self.root / "alias"
		alias.symlink_to(self.hub, target_is_directory=True)
		with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(alias)}), self.assertRaises(ValueError):
			self.engine.status(state["id"])
