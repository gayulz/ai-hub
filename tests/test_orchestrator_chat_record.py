"""대화 기록·복구·동시 호출의 파일 시스템 경계."""

import copy
import json
import os
import stat
import time
from contextlib import ExitStack
from unittest import mock

from orchestrator import chat, chat_record
from orchestrator.providers import ChatReply
from test_orchestrator_chat import ChatCase, response


class ChatRecordTest(ChatCase):
	def path(self, state):
		return self.hub / "chats" / state["id"]

	def test_private_modes_and_atomic_replace_preserve_previous_state_on_failure(self):
		state = self.new()
		folder = self.path(state)
		for path in (self.hub / "chats", folder):
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
		for name in ("chat.json", ".lock"):
			self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
		with self.engine.store.locked(state["id"]) as record:
			changed = {**state, "needs_user": "선택이 필요합니다"}
			with mock.patch("orchestrator.chat_record.os.replace", side_effect=OSError("합성 실패")), self.assertRaises(OSError):
				record.save(changed)
			self.assertEqual(record.load(), state)
		self.assertEqual(sorted(p.name for p in folder.iterdir()), [".lock", "chat.json"])

	def test_read_and_list_never_create_locks_repair_permissions_or_recover_busy(self):
		state = self.new()
		folder = self.path(state)
		state["status"] = "busy"
		with self.engine.store.locked(state["id"]) as record:
			record.save(state)
		(folder / ".lock").unlink()
		os.chmod(folder, 0o755)
		os.chmod(folder / "chat.json", 0o644)
		old = time.time() - 601
		os.utime(folder / "chat.json", (old, old))
		self.assertEqual(self.engine.store.read(state["id"]), state)
		self.assertEqual(self.engine.store.list_states()[0]["status"], "busy")
		self.assertFalse((folder / ".lock").exists())
		self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)
		self.assertEqual(stat.S_IMODE((folder / "chat.json").stat().st_mode), 0o644)
		self.assertEqual((folder / "chat.json").stat().st_mtime, old)

	def test_symbolic_and_hard_links_for_state_and_lock_are_refused(self):
		for name in ("chat.json", ".lock"):
			for hard in (False, True):
				with self.subTest(name=name, hard=hard):
					state = self.new()
					path = self.path(state) / name
					outside = self.root / (state["id"] + ".json")
					outside.write_bytes(path.read_bytes())
					os.chmod(outside, 0o644)
					path.unlink()
					if hard:
						os.link(outside, path)
					else:
						path.symlink_to(outside)
					before = outside.read_bytes()
					self.assertIsNone(self.engine.store.read(state["id"]))
					with self.assertRaises((OSError, ValueError)):
						self.engine.send(state["id"], "질문")
					self.assertEqual(outside.read_bytes(), before)
					self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
		self.mock_call.assert_not_called()

	def test_linked_conversation_root_parent_and_global_slots_are_refused(self):
		state = self.new()
		folder = self.path(state)
		outside = self.root / "moved"
		folder.rename(outside)
		folder.symlink_to(outside, target_is_directory=True)
		self.assertIsNone(self.engine.store.read(state["id"]))
		self.assertEqual(self.engine.store.list_states(), [])
		with self.assertRaises(OSError):
			self.engine.send(state["id"], "질문")
		folder.unlink()
		outside.rename(folder)
		root = self.hub / "chats"
		root.rename(self.root / "moved-root")
		root.symlink_to(self.root / "moved-root", target_is_directory=True)
		self.assertEqual(self.engine.store.list_states(), [])
		with self.assertRaises(OSError):
			self.new()
		root.unlink()
		(self.root / "moved-root").rename(root)
		(self.hub / "run").symlink_to(self.repo, target_is_directory=True)
		with self.assertRaises(OSError):
			self.engine.send(state["id"], "질문")
		self.assertFalse((self.repo / "chat-slots").exists())
		self.mock_call.assert_not_called()

	def test_recent_fifty_summaries_skip_corrupt_or_unknown_state(self):
		states = []
		for number in range(55):
			state = self.new()
			state["created"] = f"2026-10-25T00:00:{number:02d}+00:00"
			with self.engine.store.locked(state["id"]) as record:
				record.save(state)
			states.append(state)
		for change in ({"version": True}, {"created": "일시 오류"}, {"turns": None}, {"raw_response": "원문"},
				{"seconds": float("nan")}, {"session": str(self.root / "session.json")}, {"cards": [{}]}):
			state = self.new()
			(self.path(state) / "chat.json").write_text(json.dumps({**state, **change}))
			self.assertIsNone(self.engine.store.read(state["id"]))
		items = self.engine.store.list_states()
		self.assertEqual(len(items), 50)
		self.assertEqual([item["id"] for item in items], [s["id"] for s in reversed(states[5:])])
		self.assertEqual(set(items[0]), {"id", "created", "target", "plan", "tool", "status", "needs_user", "calls", "seconds", "failure", "user_turns"})
		for invalid in ("../bad", "c-xyz", "", None):
			self.assertIsNone(self.engine.store.read(invalid))

	def test_fresh_busy_refuses_send_and_stale_busy_recovers_on_next_access(self):
		state = self.new()
		state.update(status="busy", session="thread-01", calls=1)
		with self.engine.store.locked(state["id"]) as record:
			record.save(state)
		with self.assertRaisesRegex(chat.ChatError, "이미 답을 쓰는 중입니다"):
			self.engine.send(state["id"], "이어서 질문")
		self.assertEqual(self.engine.status(state["id"])["status"], "busy")
		old = time.time() - 601
		os.utime(self.path(state) / "chat.json", (old, old))
		state = self.engine.status(state["id"])
		self.assertEqual(state["status"], "idle")
		self.assertIsNone(state["session"])
		state = self.engine.send(state["id"], "다시 질문")
		self.assertEqual((state["status"], state["calls"], state["failure"]), ("idle", 2, ""))
		self.assertIsNone(self.ai.calls[0][1]["session"])

	def test_live_folder_lock_cannot_be_recovered_even_after_ten_minutes(self):
		state = self.new()
		def live(plan, **kwargs):
			path = self.path(state) / "chat.json"
			old = time.time() - 601
			os.utime(path, (old, old))
			self.assertEqual(chat.Engine().status(state["id"])["status"], "busy")
			with self.assertRaisesRegex(chat.ChatError, "이미 답을 쓰는 중입니다"):
				chat.Engine().send(state["id"], "동시 전송")
			with self.assertRaisesRegex(chat.ChatError, "이미 답을 쓰는 중입니다"):
				chat.Engine().close(state["id"])
			return ChatReply(response(), .25, {}, "thread-01")
		with mock.patch("orchestrator.providers.chat_call", side_effect=live) as call:
			result = self.engine.send(state["id"], "최초 전송")
		self.assertEqual(call.call_count, 1)
		self.assertEqual(result["status"], "idle")
		self.assertEqual(len(result["turns"]), 2)

	def test_three_global_slots_only_and_full_pool_changes_no_conversation(self):
		state = self.new()
		with ExitStack() as stack:
			for _ in range(2):
				stack.enter_context(chat_record.slot())
			state = self.engine.send(state["id"], "세 번째 호출")
			stack.enter_context(chat_record.slot())
			with self.assertRaisesRegex(chat.ChatError, "대화 호출이 가득 찼습니다"):
				self.engine.send(state["id"], "네 번째 호출")
			self.assertEqual(self.engine.store.read(state["id"]), state)
		self.assertEqual(len(self.ai.calls), 1)
		state = self.engine.send(state["id"], "슬롯 반환 뒤 호출")
		self.assertEqual(len(self.ai.calls), 2)
		for path in (self.hub / "run/chat-slots").iterdir():
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual(len(list((self.hub / "run/chat-slots").iterdir())), 3)

	def test_confirm_does_not_consume_card_or_turn_when_slots_or_turn_budget_are_full(self):
		state = self.pending()
		card_id = state["cards"][-1]["id"]
		from test_orchestrator_chat import answer
		state = self.engine.answer(state["id"], card_id, [answer()])
		with ExitStack() as stack:
			for _ in range(3):
				stack.enter_context(chat_record.slot())
			with self.assertRaisesRegex(chat.ChatError, "가득"):
				self.engine.confirm(state["id"], card_id)
		self.assertEqual(self.engine.store.read(state["id"]), state)
		with self.engine.store.locked(state["id"]) as record:
			turn = copy.deepcopy(state["turns"][0])
			state["turns"].extend(copy.deepcopy(turn) for _ in range(49))
			record.save(state)
		with self.assertRaisesRegex(chat.ChatError, "50"):
			self.engine.confirm(state["id"], card_id)
		self.assertEqual(self.engine.store.read(state["id"]), state)
		self.assertEqual(len(self.ai.calls), 1)
