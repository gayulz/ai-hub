"""합성 할 일로 저장·완료·복원·결정 연결과 기록 보호를 검사한다."""

import fcntl
import json
import os
import stat
from unittest import mock

from office import actions, tasks
from office.state import read_decisions
from test_office import OfficeCase

TID = "t-" + "a" * 32


def create_payload(**values):
	return {"action": "task_create", "id": TID, "title": "합성 개발 아이디어", "memo": "합성 메모\n다음 줄", **values}


def change_payload(row, action="task_update", **values):
	return {"action": action, **{key: row[key] for key in ("id", "kind", "version", "title", "memo")}, **values}


class OfficeTasksTest(OfficeCase):
	def source(self):
		return read_decisions(self.hub, self.now.date())["decisions"]

	def state(self):
		return tasks.read_state(self.hub, self.source())

	def create(self):
		code, body = actions.apply(create_payload())
		self.assertEqual(code, 200, body)
		return body["task"]

	def test_read_without_store_writes_nothing_and_keeps_decisions(self):
		before = self.snapshot()
		group, personal, decisions = self.state()
		self.assertEqual((group["status"], personal["items"]), ("정상", []))
		self.assertEqual([(r["id"], r["version"]) for r in decisions["items"]], [("d01", 0)])
		self.assertEqual(before, self.snapshot())

	def test_register_edit_complete_reopen_survive_reload_without_commands(self):
		with mock.patch.object(actions.subprocess, "run") as command:
			row = self.create()
			code, body = actions.apply(change_payload(row, memo="합성 처리 메모"))
			self.assertEqual(code, 200)
			row = body["task"]
			code, body = actions.apply(change_payload(row, "task_complete"))
			self.assertEqual(code, 200)
			row = body["task"]
			self.assertEqual((row["version"], row["status"], row["memo"]), (3, "done", "합성 처리 메모"))
			self.assertIsNotNone(row["completed"])
			self.assertEqual(self.state()[1]["items"], [])
			self.assertEqual(tasks.load(self.hub), [row])
			code, body = actions.apply(change_payload(row, "task_reopen"))
			self.assertEqual(code, 200)
			self.assertEqual((body["task"]["version"], body["task"]["completed"]), (4, None))
			self.assertEqual(len(self.state()[1]["items"]), 1)
			command.assert_not_called()
		path = self.hub / "office-tasks" / ("task-" + TID + ".json")
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
		audit = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assertNotIn(row["title"], audit)
		self.assertNotIn(row["memo"], audit)
		self.assertEqual([json.loads(line)["action"] for line in audit.splitlines()], list(tasks.ACTIONS[:3]) + ["task_reopen"])

	def test_decision_completion_preserves_source_and_archives_title_after_removal(self):
		original = (self.hub / "decisions.toml").read_bytes()
		row = self.state()[2]["items"][0]
		code, body = actions.apply(change_payload(row, "task_complete", memo="합성 결정 처리 완료"))
		self.assertEqual(code, 200, body)
		self.assertEqual(self.state()[2]["items"], [])
		self.assertEqual((self.hub / "decisions.toml").read_bytes(), original)
		completed = body["task"]
		(self.hub / "decisions.toml").write_text("item=[]\n")
		self.assertEqual(self.state()[0]["items"], [completed])
		self.assertEqual(actions.apply(change_payload(completed, "task_reopen"))[0], 409)
		(self.hub / "decisions.toml").write_bytes(original)
		self.assertEqual(actions.apply(change_payload(completed, "task_reopen"))[0], 200)
		self.assertEqual(len(self.state()[2]["items"]), 1)

	def test_stale_duplicate_wrong_status_and_global_lock_do_not_overwrite(self):
		row = self.create()
		self.assertEqual(actions.apply(create_payload())[0], 409)
		self.assertEqual(actions.apply(change_payload(row, "task_reopen"))[0], 409)
		self.assertEqual(actions.apply(change_payload(row, memo="다른 화면의 수정"))[0], 200)
		before = tasks.load(self.hub)
		self.assertEqual(actions.apply(change_payload(row, "task_complete"))[0], 409)
		with actions.private_file(self.hub, "run", "office-action.lock") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			self.assertEqual(actions.apply(change_payload(before[0]))[0], 409)
		self.assertEqual(tasks.load(self.hub), before)

	def test_validation_rejects_bad_text_ids_versions_kinds_and_extra_fields(self):
		bad = [create_payload(title="  "), create_payload(title="가" * 201), create_payload(memo="가" * 2001),
			create_payload(title="합성\x00"), create_payload(memo="합성\ud800"), create_payload(id="../abc"),
			create_payload(id="d01"), create_payload(extra="허용 안 됨"), create_payload(memo=[])]
		row = {"id": TID, "kind": "task", "version": 1, "title": "합성 제목", "memo": ""}
		bad += [change_payload(row, version=True), change_payload(row, version=-1), change_payload(row, kind="run"),
			change_payload(row, kind="decision", id="../abc")]
		with mock.patch.object(actions.subprocess, "run") as command:
			for payload in bad:
				with self.subTest(payload=repr(payload)):
					self.assertEqual(actions.apply(payload)[0], 400)
			command.assert_not_called()
		self.assertFalse((self.hub / "office-tasks").exists())

	def test_failed_atomic_replace_keeps_old_data_and_cleans_temporary(self):
		row = self.create()
		path = next((self.hub / "office-tasks").iterdir())
		before = path.read_bytes()
		with mock.patch.object(tasks.os, "replace", side_effect=OSError("합성 저장 실패")):
			self.assertEqual(actions.apply(change_payload(row, memo="실패한 수정"))[0], 409)
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual(list(path.parent.iterdir()), [path])

	def test_bad_record_is_unavailable_and_never_overwritten(self):
		self.create()
		path = next((self.hub / "office-tasks").iterdir())
		for content in ("{", "[]", '{"id":"xyz"}'):
			path.write_text(content)
			before = path.read_bytes()
			self.assertEqual(self.state()[0]["status"], "확인 불가")
			self.assertEqual(actions.apply(create_payload(id="t-" + "b" * 32))[0], 409)
			self.assertEqual(path.read_bytes(), before)

	def test_links_hardlinks_and_wrong_permissions_are_rejected(self):
		row = self.create()
		path = next((self.hub / "office-tasks").iterdir())
		for mode in (0o644, 0o660):
			path.chmod(mode)
			self.assertEqual(self.state()[0]["status"], "확인 불가")
			self.assertEqual(actions.apply(change_payload(row))[0], 409)
		path.chmod(0o600)
		outside = self.root / "outside.json"
		outside.write_bytes(path.read_bytes())
		outside.chmod(0o600)
		path.unlink()
		for link in (lambda: path.symlink_to(outside), lambda: os.link(outside, path)):
			link()
			self.assertEqual(self.state()[0]["status"], "확인 불가")
			self.assertEqual(actions.apply(change_payload(row))[0], 409)
			path.unlink()
		path.parent.rmdir()
		path.parent.symlink_to(self.root)
		self.assertEqual(self.state()[0]["status"], "확인 불가")
		self.assertEqual(actions.apply(change_payload(row))[0], 409)

	def test_dashboard_excludes_completed_decision_but_keeps_approval_groups(self):
		with mock.patch("socket.create_connection", side_effect=OSError("합성 연결 거부")):
			row = self.state()[2]["items"][0]
			self.assertEqual(actions.apply(change_payload(row, "task_complete"))[0], 200)
			self.create()
			before = self.snapshot()
			state = OfficeCase.state(self)
			self.assertEqual(len(state["approvals"]["tasks"]["items"]), 1)
			self.assertEqual(state["approvals"]["decisions"]["items"], [])
			self.assertEqual(len([r for r in state["tasks"]["items"] if r["status"] == "done"]), 1)
			self.assertEqual(before, self.snapshot())
			self.assertIn("runs", state["approvals"])
			self.assertIn("c4", state["approvals"])
