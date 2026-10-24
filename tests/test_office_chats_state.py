"""대화 목록은 최근 20개만 가려서 읽고 손상된 기록을 격리한다."""

import json
import os
from unittest import mock

from office import chats
from orchestrator import chat_record
from test_office_chats import CARD, CHAT, ChatCase, chat_state, write_chat


class OfficeChatsStateTest(ChatCase):
	def test_empty_state_and_plan_filter_are_read_only_and_match_engine(self):
		with (self.hub / "plans.toml").open("a") as stream:
			stream.write('''
[[plans]]
id="p03"
tool="agy"
owner="personal"
allow=["personal"]
[[plans]]
id="p04"
tool="codex"
owner="company"
allow=["company"]
[[plans]]
id="p05"
tool="claude"
owner="personal"
allow=["company"]
''')
		before = self.snapshot()
		state = chats.read_state(self.hub)
		self.assertEqual(state, {"chats": {"status": "정상", "items": []},
			"chat_plans": [{"id": "p01", "tool": "codex"}, {"id": "p02", "tool": "claude"}]})
		self.assertEqual(before, self.snapshot())
		self.assertFalse((self.hub / "chats").exists())

	def test_twenty_most_recently_modified_records_only_public_columns_and_last_assistant_preview(self):
		for n in range(25):
			state = chat_state(identity="c-" + format(n, "032x"), card="waiting")
			state.update(calls=4, seconds=1.25, needs_user="합성 확인 필요")
			state["turns"] = [{"role": role, "text": text, "at": state["created"], "source": "office", "kind": "message"}
				for role, text in (("assistant", "옛 답"), ("user", "합성 질문"), ("assistant", "마지막 합성 답 " + "가" * 100), ("user", "숨길 최근 질문"))]
			path = write_chat(self.hub, state)
			os.utime(path, (1_790_000_000 + n,) * 2)
		before = self.snapshot()
		with mock.patch.object(chat_record.Store, "list_states", side_effect=AssertionError("요약 목록 대신 기록을 읽어야 합니다")):
			value = chats.read_state(self.hub)
		self.assertEqual(value["chats"]["status"], "정상")
		items = value["chats"]["items"]
		self.assertEqual([item["id"] for item in items], ["c-" + format(n, "032x") for n in range(24, 4, -1)])
		self.assertEqual(set(items[0]), {"id", "target", "plan", "tool", "status", "needs_user", "calls", "user_turns", "updated", "preview", "card"})
		self.assertEqual((items[0]["calls"], items[0]["user_turns"]), (4, 2))
		self.assertEqual(len(items[0]["preview"]), 60)
		self.assertTrue(items[0]["preview"].startswith("마지막 합성 답"))
		self.assertEqual(items[0]["card"], {"card": CARD, "turn": 1, "questions": state["cards"][0]["questions"]})
		self.assertNotIn("hidden-session", json.dumps(value))
		self.assertNotIn("숨길 최근 질문", json.dumps(value, ensure_ascii=False))
		self.assertEqual(before, self.snapshot())

	def test_mask_before_truncation_and_paths_removed_from_preview_needs_and_card(self):
		secret = "ghp_" + "a" * 36
		sensitive = "acme " + secret + " " + "p01" + "@" + "acme.invalid /tmp/private ./lib/file.py C:\\data\\private"
		state = chat_state(card="waiting")
		state.update(target="xyz", plan="acme", needs_user=sensitive)
		state["turns"] = [{"at": state["created"], "role": "assistant", "text": sensitive + " 뒤" * 200, "source": "office", "kind": "message"}]
		q = state["cards"][0]["questions"][0]
		q.update(text=sensitive, reason=sensitive)
		for option in q["options"]:
			for key in ("label", "difference", "reply"):
				option[key] = sensitive
		write_chat(self.hub, state)
		value = chats.read_state(self.hub)
		self.assertEqual(value["chats"]["status"], "정상")
		encoded = json.dumps(value, ensure_ascii=False)
		for raw in ("acme", "xyz", "@", secret, "private"):
			self.assertNotIn(raw, encoded)
		self.assertIn("[경로 생략]", encoded)
		self.assertIn("[보안값]", encoded)

	def test_plain_words_and_repository_relative_paths_stay_readable_but_absolute_paths_are_hidden(self):
		state = chat_state()
		state["turns"] = [{"at": state["created"], "role": "assistant", "source": "office", "kind": "message",
			"text": "UI/UX ~/.ai-hub/y office/static/office.js /Users/sample/x"}]
		write_chat(self.hub, state)
		preview = chats.read_state(self.hub)["chats"]["items"][0]["preview"]
		for kept in ("UI/UX", "office/static/office.js"):
			self.assertIn(kept, preview)
		for hidden in ("/Users/sample", ".ai-hub"):
			self.assertNotIn(hidden, preview)
		self.assertIn("[경로 생략]", preview)

	def test_only_last_waiting_card_is_projected_and_empty_preview_is_supported(self):
		for status in ("waiting", "answered", "confirmed"):
			state = chat_state(card=status)
			state["cards"].insert(0, {**chat_state(card="waiting")["cards"][0], "id": "q-" + "c" * 32})
			write_chat(self.hub, state)
			row = chats.read_state(self.hub)["chats"]["items"][0]
			self.assertEqual(row["preview"], "")
			self.assertEqual(row["card"]["card"] if row["card"] else None, CARD if status == "waiting" else None)
		write_chat(self.hub, chat_state())
		self.assertIsNone(chats.read_state(self.hub)["chats"]["items"][0]["card"])

	def test_corrupt_records_preserve_healthy_items_without_permission_or_file_changes(self):
		good = write_chat(self.hub, chat_state(card="waiting"))
		good.chmod(0o644)
		bad = write_chat(self.hub, chat_state(identity="c-" + "d" * 32))
		for content in ("{", "{}", "[" * 1500):
			bad.write_text(content)
			before = self.snapshot()
			value = chats.read_state(self.hub)
			self.assertEqual(value["chats"]["status"], "확인 불가")
			self.assertEqual([item["id"] for item in value["chats"]["items"]], [CHAT])
			self.assertIsNotNone(value["chats"]["items"][0]["card"])
			self.assertEqual(before, self.snapshot())
		bad.unlink()
		bad.symlink_to(good)
		self.assertEqual(chats.read_state(self.hub)["chats"]["status"], "확인 불가")

	def test_plan_or_directory_failures_mark_unavailable_without_hiding_healthy_items(self):
		write_chat(self.hub, chat_state())
		(self.hub / "plans.toml").write_text("{")
		value = chats.read_state(self.hub)
		self.assertEqual(value["chat_plans"], [])
		self.assertEqual(value["chats"]["status"], "확인 불가")
		self.assertEqual(len(value["chats"]["items"]), 1)
		with mock.patch.object(chat_record, "directory", side_effect=PermissionError):
			self.assertEqual(chats.read_state(self.hub)["chats"]["status"], "확인 불가")
			self.assertEqual(chats.read_state(self.hub)["chats"]["items"], [])

	def test_unsafe_question_identifiers_are_isolated_and_symlink_root_is_not_followed(self):
		state = chat_state(card="waiting")
		state["cards"][0]["questions"][0]["id"] = "acme"
		write_chat(self.hub, state)
		self.assertEqual(chats.read_state(self.hub)["chats"]["items"], [])
		self.assertEqual(chats.read_state(self.hub)["chats"]["status"], "확인 불가")
		other = self.root / "linked"
		other.mkdir()
		with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(other)}):
			(other / "chats").symlink_to(self.hub / "chats", target_is_directory=True)
			value = chats.read_state(other)
		self.assertEqual(value["chats"]["status"], "확인 불가")
		self.assertEqual(value["chats"]["items"], [])
