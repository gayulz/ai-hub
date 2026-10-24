"""합성 결재함과 가짜 명령으로 반영 범위·잠금·가림·기록을 확인한다."""

import json
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from office import actions
from test_office import OfficeCase


class ActionCase(OfficeCase):
	def setUp(self):
		super().setUp()
		self.repo = self.root / "repo"
		self.repo.mkdir()
		patch = mock.patch.object(actions, "REPO", self.repo)
		patch.start()
		self.addCleanup(patch.stop)
		self.bid = "c4-" + "a" * 16
		self.rid = "2026-10-23-005"
		self.conn.execute("insert into c4_batches values (?,'s01','prepared','xyz')", (self.bid,))
		self.conn.execute("insert into c4_items values (?,'xyz','xyz','prepared','xyz')", (self.bid,))
		self.conn.commit()
		self.run_path = self.hub / "runs" / self.rid / "run.json"
		self.run_path.write_text(json.dumps({"status": "승인 필요", "kind": "code", "repo": str(self.repo)}))
		self.plan_path = self.hub / "vaultreorg/r1-plan.json"
		self.plan_path.parent.mkdir()
		self.plan_path.write_text(json.dumps({"notes": [{"folder": "Career", "status": "pending"},
			{"folder": "root", "status": "pending"}]}))


class OfficeActionsTest(ActionCase):
	def apply(self, action="c4_apply", identity=None):
		return actions.apply({"action": action, "id": self.bid if identity is None else identity})

	def test_only_three_commands_with_exact_argv_repository_environment_and_timeout(self):
		for action, identity, command in (("c4_apply", self.bid, ["collector", "backfill", "--apply"]),
				("run_apply", self.rid, ["orchestrator", "apply"]), ("r1_apply", "Career", ["vaultreorg", "apply", "--folder"]),
				("r1_apply", "root", ["vaultreorg", "apply", "--folder"])):
			with self.subTest(action=action), mock.patch.object(actions.subprocess, "run") as run:
				run.return_value = subprocess.CompletedProcess([], 0, "xyz\n")
				status, payload = self.apply(action, identity)
				self.assertEqual(status, 200)
				self.assertTrue(payload["ok"])
				calls = [call for call in run.call_args_list if call.args[0][0] == sys.executable]
				self.assertEqual(len(calls), 1)
				self.assertEqual(calls[0].args[0], [sys.executable, "-m", *command, identity])
				options = calls[0].kwargs
				self.assertIs(options["shell"], False)
				self.assertEqual(options["cwd"], self.repo)
				self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
				self.assertEqual(options["env"]["PYTHONPATH"], str(self.repo))
				self.assertEqual(options["timeout"], 600)
				self.assertEqual(options["stderr"], subprocess.STDOUT)

	def test_invalid_actions_ids_and_payloads_never_start_commands(self):
		bad = [[], None, {}, {"action": "dev_run", "id": self.rid}, {"action": [], "id": self.rid},
			{"action": "c4_apply", "id": self.bid, "extra": "xyz"}]
		for action, values in (("c4_apply", ("c4-acme", "c4-" + "g" * 16, self.bid + "/", 1, None)),
				("run_apply", ("2026-13-24-001", "2026-02-30-001", "2026-10-24-1", "../xyz")),
				("r1_apply", ("../xyz", "xyz/abc", "xyz..abc", ".", "", "Career\n", "Ca\x00reer", "Ca\x7freer",
					"Career\u2028xyz", "💼 Career", "🔐xyz", "Ca\\reer", " Career", "Career\ud800"))):
			bad += [{"action": action, "id": value} for value in values]
		with mock.patch.object(actions.subprocess, "run") as run:
			for payload in bad:
				with self.subTest(payload=repr(payload)):
					self.assertEqual(actions.apply(payload)[0], 400)
			run.assert_not_called()
		self.assertFalse((self.hub / "logs/office-actions.jsonl").exists())

	def test_absent_completed_or_empty_approvals_and_other_repositories_are_conflicts(self):
		with mock.patch.object(actions.subprocess, "run") as run:
			for action, identity in (("c4_apply", "c4-" + "b" * 16), ("run_apply", "2026-10-24-999"), ("r1_apply", "Study")):
				self.assertEqual(self.apply(action, identity)[0], 409)
			self.conn.execute("update c4_items set state='held' where batch=?", (self.bid,))
			self.conn.commit()
			self.assertEqual(self.apply()[0], 409)
			self.conn.execute("update c4_items set state='prepared' where batch=?", (self.bid,))
			self.conn.execute("update c4_batches set state='applied' where id=?", (self.bid,))
			self.conn.commit()
			self.assertEqual(self.apply()[0], 409)
			for record in ({"status": "완료", "repo": str(self.repo)}, {"status": "승인 필요", "repo": str(self.root / "xyz")}):
				self.run_path.write_text(json.dumps(record))
				self.assertEqual(self.apply("run_apply", self.rid)[0], 409)
			self.plan_path.write_text('{"notes":[{"folder":"Career","status":"applied"}]}')
			self.assertEqual(self.apply("r1_apply", "Career")[0], 409)
			run.assert_not_called()

	def test_output_is_redacted_before_last_twenty_lines_and_audit_is_private_metadata(self):
		secret = "xyz-private-material"
		output = "\n".join([f"줄 {n}" for n in range(30)] + ["password=" + secret,
			"xyz" + "@" + "acme.invalid", "192" + ".0.2.1", "-----BEGIN PRIVATE KEY-----",
			*([secret] * 25), "-----END PRIVATE KEY-----", "xyz 마지막"])
		with mock.patch.object(actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
			status, payload = self.apply()
		self.assertEqual(status, 200)
		self.assertEqual(len(payload["output"].splitlines()), 20)
		self.assertNotIn(secret, payload["output"])
		self.assertNotIn("@", payload["output"])
		self.assertNotIn("192" + ".0.2.1", payload["output"])
		self.assertIn("[보안값]", payload["output"])
		self.assertTrue(payload["output"].endswith("xyz 마지막"))
		path = self.hub / "logs/office-actions.jsonl"
		row = json.loads(path.read_text())
		self.assertEqual(set(row), {"at", "action", "id", "exit_code", "seconds", "ok", "message"})
		self.assertNotIn(secret, path.read_text())
		self.assertNotIn("password", path.read_text())
		self.assertEqual(row["exit_code"], 0)
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
		self.assertEqual(stat.S_IMODE((self.hub / "run").stat().st_mode), 0o700)
		self.assertTrue(any(item["source"] == "office-action" for item in self.state()["activity"]))

	def test_failure_timeout_launch_error_and_redaction_error_never_leak_raw_output(self):
		for failure, expected in ((subprocess.CompletedProcess([], 7, "password=xyz"), 500),
				(subprocess.TimeoutExpired([], 600, output=b"password=xyz"), 504), (OSError("xyz-private"), 500)):
			with self.subTest(failure=type(failure).__name__), mock.patch.object(actions.subprocess, "run") as run:
				if isinstance(failure, Exception):
					run.side_effect = failure
				else:
					run.return_value = failure
				status, payload = self.apply()
				self.assertEqual(status, expected)
				self.assertFalse(payload["ok"])
				self.assertNotIn("xyz", payload["output"] + payload["message"])
		with mock.patch.object(actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "xyz-private")), \
				mock.patch.object(actions, "redact", side_effect=ValueError):
			self.assertNotIn("xyz-private", self.apply()[1]["output"])

	def test_only_commit_changes_under_office_require_restart_even_after_partial_failure(self):
		for changed, exit_code, expected in (("office/server.py\0", 0, True), ("", 0, False), ("office/office.py\0", 1, True)):
			with self.subTest(changed=changed, exit_code=exit_code), mock.patch.object(actions.subprocess, "run") as run:
				run.side_effect = [subprocess.CompletedProcess([], 0, "a" * 40),
					subprocess.CompletedProcess([], exit_code, "xyz"), subprocess.CompletedProcess([], 0, changed)]
				self.assertEqual(self.apply("run_apply", self.rid)[1]["restart_needed"], expected)
				self.assertEqual(run.call_args_list[-1].args[0], ["git", "diff", "--name-only", "-z", "a" * 40, "HEAD", "--", "office/"])

	def test_lock_blocks_second_caller_and_is_released_after_failure(self):
		started, release = threading.Event(), threading.Event()
		def wait(*args, **kwargs):
			started.set()
			if not release.wait(5):
				raise RuntimeError("가짜 명령 종료 대기 실패")
			return subprocess.CompletedProcess([], 1, "xyz")
		with mock.patch.object(actions.subprocess, "run", side_effect=wait) as run, ThreadPoolExecutor() as pool:
			first = pool.submit(self.apply)
			try:
				self.assertTrue(started.wait(5))
				status, payload = self.apply()
				self.assertEqual((status, payload["message"]), (409, "이미 실행 중"))
			finally:
				release.set()
			self.assertEqual(first.result(timeout=5)[0], 500)
			run.assert_called_once()
		with mock.patch.object(actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "xyz")):
			self.assertEqual(self.apply()[0], 200)

	def test_unwritable_or_linked_audit_prevents_execution(self):
		log = self.hub / "logs/office-actions.jsonl"
		log.symlink_to(self.root / "xyz")
		with mock.patch.object(actions.subprocess, "run") as run:
			self.assertEqual(self.apply()[0], 409)
			run.assert_not_called()
		self.assertFalse((self.root / "xyz").exists())
