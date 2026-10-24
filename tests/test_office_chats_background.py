"""가짜 자식 프로세스로 대화별 잠금·비동기 실행·임시 파일 수명을 검사한다."""

import copy
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

from office import actions, chats
from test_office_chats import CARD, CHAT, ChatCase, chat_state, write_chat


class OfficeChatsBackgroundTest(ChatCase):
	def setUp(self):
		super().setUp()
		self.jobs = []
		def thread(**options):
			self.jobs.append(options)
			return mock.Mock()
		patch = mock.patch.object(chats.threading, "Thread", side_effect=thread)
		patch.start()
		self.addCleanup(patch.stop)
		self.addCleanup(self.drain)

	def drain(self):
		while self.jobs:
			job = self.jobs.pop(0)
			job["target"](*job["args"])

	def process(self, identity=CHAT, code=0):
		state = chat_state(identity=identity)
		state.update(dev_draft="합성 로그 금지 초안", needs_user="합성 로그 금지 질문")
		return mock.Mock(returncode=code, communicate=mock.Mock(return_value=(json.dumps(state), "합성 로그 금지 오류")))

	def test_send_detaches_with_private_file_and_holds_only_office_lock_until_done(self):
		path = write_chat(self.hub, chat_state())
		before, process = path.read_bytes(), self.process()
		with mock.patch.object(chats.subprocess, "Popen", return_value=process) as popen:
			status, value = actions.apply(self.send)
			self.assertEqual(status, 200, value)
			self.assertEqual(value, actions.result(True, "답을 쓰는 중입니다. 진행은 패널에서 봅니다."))
			process.communicate.assert_not_called()
			argv, options = popen.call_args.args[0], popen.call_args.kwargs
			text_file = Path(argv[-3])
			self.assertEqual(argv, [sys.executable, "-m", "orchestrator", "chat", "send", CHAT,
				"--text-file", str(text_file), "--source", "office"])
			self.assertNotIn(self.send["text"], argv)
			self.assertEqual(text_file.read_text(), self.send["text"])
			self.assertEqual(text_file.parent, self.hub / "run/office-chats")
			self.assertEqual(stat.S_IMODE(text_file.stat().st_mode), 0o600)
			self.assertEqual(stat.S_IMODE(text_file.parent.stat().st_mode), 0o700)
			self.assertIs(options["shell"], False)
			self.assertTrue(options["start_new_session"])
			self.assertEqual(options["stdin"], subprocess.DEVNULL)
			self.assertEqual(options["stdout"], subprocess.PIPE)
			self.assertEqual(options["stderr"], subprocess.PIPE)
			self.assertEqual(options["cwd"], actions.REPO)
			self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
			self.assertEqual(options["env"]["PYTHONPATH"], str(actions.REPO))
			self.assertEqual(len(options["pass_fds"]), 1)
			lock_fd = options["pass_fds"][0]
			os.fstat(lock_fd)
			self.assertTrue((self.hub / "run/office-chats" / (CHAT + ".lock")).is_file())
			self.assertFalse((path.parent / ".lock").exists())
			for payload in (self.send, self.answer, self.confirm, self.close):
				status, value = actions.apply(payload)
				self.assertEqual((status, value["message"]), (409, "이미 답을 쓰는 중입니다."))
			popen.assert_called_once()
			self.assertEqual(path.read_bytes(), before)
		self.drain()
		self.assertFalse(text_file.exists())
		with self.assertRaises(OSError):
			os.fstat(lock_fd)
		log = self.hub / "logs/office-chats" / (CHAT + ".log")
		self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
		self.assertEqual([json.loads(line) for line in log.read_text().splitlines()], [
			{"exit_code": 0, "status": "idle", "action": "chat_send"}])
		for file in (log, self.hub / "logs/office-actions.jsonl"):
			for raw in (self.send["text"], "합성 로그 금지", "hidden-session-01"):
				self.assertNotIn(raw, file.read_text())

	def test_other_chat_runs_concurrently_and_text_length_boundary_is_accepted(self):
		other = "c-" + "d" * 32
		write_chat(self.hub, chat_state())
		write_chat(self.hub, chat_state(identity=other))
		with mock.patch.object(chats.subprocess, "Popen", side_effect=[self.process(), self.process(other)]) as popen:
			self.assertEqual(actions.apply({**self.send, "text": "가" * 2000})[0], 200)
			self.assertEqual(actions.apply({**self.send, "id": other})[0], 200)
			self.assertEqual(popen.call_count, 2)

	def test_missing_closed_busy_and_unconfirmed_cards_refuse_before_engine(self):
		with mock.patch.object(chats.subprocess, "Popen") as popen, mock.patch.object(chats.subprocess, "run") as run:
			self.assertEqual(actions.apply(self.send)[0], 409)
			for status in ("busy", "closed"):
				write_chat(self.hub, chat_state(status, card="waiting"))
				for payload in (self.send, self.answer, self.confirm, self.close):
					self.assertEqual(actions.apply(payload)[0], 409)
			for card in ("waiting", "answered"):
				write_chat(self.hub, chat_state(card=card))
				status, result = actions.apply(self.send)
				self.assertEqual((status, result["message"]), (409, "질문 카드에 답하고 요약을 먼저 확인하세요"))
			popen.assert_not_called()
			run.assert_not_called()
		write_chat(self.hub, chat_state(card="confirmed"))
		with mock.patch.object(chats.subprocess, "Popen", return_value=self.process()) as popen:
			self.assertEqual(actions.apply(self.send)[0], 200)
			popen.assert_called_once()

	def test_answer_and_confirm_require_current_card_and_confirm_uses_fixed_arguments(self):
		with mock.patch.object(chats.subprocess, "Popen", return_value=self.process()) as popen:
			for card in (None, "waiting", "confirmed"):
				write_chat(self.hub, chat_state(card=card))
				self.assertEqual(actions.apply(self.confirm)[0], 409)
			state = chat_state(card="answered")
			state["cards"].append({**copy.deepcopy(state["cards"][0]), "id": "q-" + "c" * 32, "turn": 2})
			write_chat(self.hub, state)
			for payload in (self.answer, self.confirm):
				self.assertEqual(actions.apply(payload)[0], 409)
			popen.assert_not_called()
			state["cards"].pop()
			path = write_chat(self.hub, state)
			before = path.read_bytes()
			self.assertEqual(actions.apply(self.answer)[0], 409)
			self.assertEqual(actions.apply(self.confirm)[0], 200)
			self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "orchestrator", "chat", "confirm", CHAT,
				"--card", CARD, "--source", "office"])
			self.assertEqual(path.read_bytes(), before)
			self.assertEqual(actions.apply(self.confirm)[0], 409)

	def test_failed_background_output_is_discarded_temp_deleted_and_lock_released(self):
		write_chat(self.hub, chat_state())
		for output, status in ((json.dumps(chat_state()), "idle"), ("합성 원문", "확인 불가"),
				(json.dumps({"id": CHAT, "status": "합성 로그 금지"}), "확인 불가"),
				(json.dumps({"id": "c-" + "f" * 32, "status": "idle"}), "확인 불가")):
			process = mock.Mock(returncode=2, communicate=mock.Mock(return_value=(output, "합성 로그 금지 오류")))
			with mock.patch.object(chats.subprocess, "Popen", return_value=process) as popen:
				self.assertEqual(actions.apply(self.send)[0], 200)
				file = Path(popen.call_args.args[0][-3])
				self.drain()
				self.assertFalse(file.exists())
			log = (self.hub / "logs/office-chats" / (CHAT + ".log")).read_text()
			self.assertEqual(json.loads(log.splitlines()[-1]), {"action": "chat_send", "exit_code": 2, "status": status})
			self.assertNotIn("합성 로그 금지", log)

	def test_launch_or_thread_failure_removes_input_and_releases_lock(self):
		write_chat(self.hub, chat_state())
		with mock.patch.object(chats.subprocess, "Popen", side_effect=OSError("합성 숨길 오류")):
			status, value = actions.apply(self.send)
			self.assertEqual(status, 409)
			self.assertNotIn("합성 숨길 오류", value["message"])
		self.assertEqual([p.suffix for p in (self.hub / "run/office-chats").iterdir()], [".lock"])
		process = self.process()
		with mock.patch.object(chats.subprocess, "Popen", return_value=process), \
				mock.patch.object(chats.threading, "Thread", side_effect=RuntimeError("합성 숨길 오류")):
			self.assertEqual(actions.apply(self.send)[0], 409)
		process.terminate.assert_called_once()
		process.communicate.assert_called_once()
		self.assertEqual([p.suffix for p in (self.hub / "run/office-chats").iterdir()], [".lock"])
		with mock.patch.object(chats.subprocess, "Popen", return_value=self.process()):
			self.assertEqual(actions.apply(self.send)[0], 200)

	def test_unsafe_background_log_or_lock_never_launches_or_changes_link_target(self):
		write_chat(self.hub, chat_state())
		folder = self.hub / "logs/office-chats"
		folder.mkdir(parents=True)
		log = folder / (CHAT + ".log")
		outside = self.root / "outside"
		outside.write_text("합성 보존 대상")
		outside.chmod(0o644)
		with mock.patch.object(chats.subprocess, "Popen") as popen:
			log.symlink_to(outside)
			self.assertEqual(actions.apply(self.send)[0], 409)
			log.unlink()
			os.link(outside, log)
			self.assertEqual(actions.apply(self.send)[0], 409)
			self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
			log.unlink()
			os.mkfifo(log)
			self.assertEqual(actions.apply(self.send)[0], 409)
			log.unlink()
			lock = self.hub / "run/office-chats" / (CHAT + ".lock")
			lock.unlink()
			lock.symlink_to(outside)
			self.assertEqual(actions.apply(self.send)[0], 409)
			popen.assert_not_called()
		self.assertEqual(outside.read_text(), "합성 보존 대상")
