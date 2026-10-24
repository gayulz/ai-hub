"""가짜 자식 프로세스로 회의 시작·재개·중복 잠금과 원문 없는 로그를 검사한다."""

import json
import os
import stat
import subprocess
import sys
from unittest import mock

from office import actions, meetings
from test_office_meetings import CID, MID, MeetingCase, meeting_state, write_meeting


class OfficeMeetingsBackgroundTest(MeetingCase):
	def setUp(self):
		super().setUp()
		self.jobs = []
		def thread(**options):
			self.jobs.append(options)
			return mock.Mock()
		patch = mock.patch.object(meetings.threading, "Thread", side_effect=thread)
		patch.start()
		self.addCleanup(patch.stop)
		self.addCleanup(self.drain)

	def drain(self):
		while self.jobs:
			job = self.jobs.pop(0)
			job["target"](*job["args"])

	def process(self, status="questions_pending", code=0):
		state = meeting_state(status)
		state["agenda"] = "합성 로그 금지 안건"
		state["cards"][0]["summary"] = "합성 로그 금지 답변"
		return mock.Mock(returncode=code, communicate=mock.Mock(return_value=(json.dumps(state), "합성 로그 금지 오류")))

	def test_start_detaches_immediately_holds_lock_and_writes_only_safe_status_log(self):
		state = meeting_state("participants_pending")
		path = write_meeting(self.hub, state)
		before = path.read_bytes()
		process = self.process()
		with mock.patch.object(meetings.subprocess, "Popen", return_value=process) as popen:
			status, value = actions.apply(self.start)
			self.assertEqual(status, 200, value)
			self.assertEqual(value, actions.result(True, "회의를 시작했습니다. 진행은 패널에서 봅니다."))
			process.communicate.assert_not_called()
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "meeting", "start", MID, "--participants", "backend", "qa"])
			options = popen.call_args.kwargs
			self.assertTrue(options["start_new_session"])
			self.assertIs(options["shell"], False)
			self.assertEqual(options["stdin"], subprocess.DEVNULL)
			self.assertEqual(options["stdout"], subprocess.PIPE)
			self.assertEqual(options["stderr"], subprocess.PIPE)
			self.assertEqual(options["cwd"], actions.REPO)
			self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
			self.assertEqual(options["env"]["PYTHONPATH"], str(actions.REPO))
			self.assertEqual(len(options["pass_fds"]), 1)
			os.fstat(options["pass_fds"][0])
			self.assertEqual(actions.apply(self.start)[0], 409)
			self.assertEqual(actions.apply(self.confirm)[0], 409)
			popen.assert_called_once()
			self.assertEqual(path.read_bytes(), before)
		self.drain()
		log = self.hub / "logs/office-meetings" / (MID + ".log")
		self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
		lines = [json.loads(line) for line in log.read_text().splitlines()]
		self.assertEqual(lines[-1], {"exit_code": 0, "status": "questions_pending"})
		for path in (log, self.hub / "logs/office-actions.jsonl"):
			self.assertNotIn("합성 로그 금지", path.read_text())
			self.assertNotIn("합성 질문 본문", path.read_text())

	def test_start_rechecks_status_confirmation_and_current_configured_candidates(self):
		with mock.patch.object(meetings.subprocess, "Popen") as popen:
			for status in ("analyzing", "questions_pending", "answers_pending", "running", "completed", "failed", "budget_exhausted"):
				write_meeting(self.hub, meeting_state(status))
				self.assertEqual(actions.apply(self.start)[0], 409)
			state = meeting_state("participants_pending")
			state["confirmed"] = True
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply(self.start)[0], 409)
			state["confirmed"] = False
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply({**self.start, "participants": ["backend", "frontend"]})[0], 400)
			popen.assert_not_called()

	def test_confirm_requires_last_answered_card_then_resumes_with_office_source(self):
		with mock.patch.object(meetings.subprocess, "Popen", return_value=self.process()) as popen:
			for status in ("questions_pending", "participants_pending", "running", "completed"):
				write_meeting(self.hub, meeting_state(status))
				self.assertEqual(actions.apply(self.confirm)[0], 409)
			state = meeting_state("answers_pending")
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply(self.confirm)[0], 409)
			state["cards"][0].update(status="confirmed", answers=[{"source": "office"}])
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply(self.confirm)[0], 409)
			state["cards"][0]["status"] = "answered"
			state["cards"].append({**state["cards"][0], "id": "q-" + "c" * 32, "turn": 2})
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply(self.confirm)[0], 409)
			popen.assert_not_called()
			state["cards"].pop()
			path = write_meeting(self.hub, state)
			before = path.read_bytes()
			status, value = actions.apply(self.confirm)
			self.assertEqual(status, 200, value)
			self.assertTrue(value["ok"])
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "meeting", "resume", MID,
				"--confirm-answers", CID, "--source", "office"])
			self.assertTrue(popen.call_args.kwargs["start_new_session"])
			self.assertIs(popen.call_args.kwargs["shell"], False)
			self.assertEqual(actions.apply(self.confirm)[0], 409)
			popen.assert_called_once()
			self.assertEqual(path.read_bytes(), before)

	def test_background_failures_never_copy_stdout_or_stderr_to_log_and_release_lock(self):
		write_meeting(self.hub, meeting_state("participants_pending"))
		process = mock.Mock(returncode=2, communicate=mock.Mock(return_value=("합성 로그 금지 출력", "회의 처리 실패: 합성 로그 금지 오류")))
		with mock.patch.object(meetings.subprocess, "Popen", return_value=process) as popen:
			self.assertEqual(actions.apply(self.start)[0], 200)
			self.drain()
			self.assertEqual(actions.apply(self.start)[0], 200)
			self.assertEqual(popen.call_count, 2)
		self.drain()
		log = self.hub / "logs/office-meetings" / (MID + ".log")
		self.assertEqual(json.loads(log.read_text().splitlines()[-1]), {"exit_code": 2})
		self.assertNotIn("합성 로그 금지", log.read_text())

	def test_failed_launch_allows_retry_and_linked_log_prevents_launch(self):
		path = write_meeting(self.hub, meeting_state("participants_pending"))
		before = path.read_bytes()
		with mock.patch.object(meetings.subprocess, "Popen", side_effect=OSError("합성 로그 금지")):
			code, value = actions.apply(self.start)
		self.assertEqual(code, 409)
		self.assertNotIn("합성 로그 금지", value["message"])
		self.assertEqual(path.read_bytes(), before)
		with mock.patch.object(meetings.subprocess, "Popen", return_value=self.process()) as popen:
			self.assertEqual(actions.apply(self.start)[0], 200)
			popen.assert_called_once()
		self.drain()
		log = self.hub / "logs/office-meetings" / (MID + ".log")
		log.unlink()
		outside = self.root / "outside"
		outside.write_text("합성 보존 대상")
		log.symlink_to(outside)
		with mock.patch.object(meetings.subprocess, "Popen") as popen:
			self.assertEqual(actions.apply(self.start)[0], 409)
			popen.assert_not_called()
		self.assertEqual(outside.read_text(), "합성 보존 대상")

	def test_other_meetings_can_start_while_a_meeting_is_running(self):
		other = "m-" + "c" * 32
		write_meeting(self.hub, meeting_state("participants_pending"))
		write_meeting(self.hub, meeting_state("participants_pending", other))
		with mock.patch.object(meetings.subprocess, "Popen", return_value=self.process()) as popen:
			self.assertEqual(actions.apply(self.start)[0], 200)
			self.assertEqual(actions.apply({**self.start, "id": other})[0], 200)
			self.assertEqual(popen.call_count, 2)

	def test_hardlinked_or_nonregular_log_never_changes_target_or_launches(self):
		write_meeting(self.hub, meeting_state("participants_pending"))
		folder = self.hub / "logs/office-meetings"
		folder.mkdir(parents=True)
		log = folder / (MID + ".log")
		outside = self.root / "outside"
		outside.write_text("합성 보존 대상")
		outside.chmod(0o644)
		os.link(outside, log)
		with mock.patch.object(meetings.subprocess, "Popen") as popen:
			self.assertEqual(actions.apply(self.start)[0], 409)
			self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
			self.assertEqual(outside.read_text(), "합성 보존 대상")
			log.unlink()
			os.mkfifo(log)
			self.assertEqual(actions.apply(self.start)[0], 409)
			popen.assert_not_called()
