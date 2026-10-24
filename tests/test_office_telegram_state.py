"""재시작 위치·카드 보관 한도와 안전한 파일 교체를 합성 홈에서 확인한다."""

import copy
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest import mock

from office.telegram_state import Store, checked, empty
from test_office_telegram_client import TelegramCase


def local_card(number=1, age=0):
	return {"meeting": "m-" + f"{number:032x}", "questions": {"scope": number},
		"pending": {"scope": {"kind": "choice", "value": "a"}}, "submit_message": None,
		"created": (datetime.now().astimezone() - timedelta(days=age)).isoformat()}


def local_ask(number=1, age=0):
	return {"id": "k-" + f"{number:08x}", "message_id": number, "options": ["승인", "거절"],
		"created": (datetime.now().astimezone() - timedelta(days=age)).isoformat(), "answer": None}


class TelegramStateTest(TelegramCase):
	def test_roundtrip_permissions_and_offset_survive_restart(self):
		store = Store()
		with store.locked() as state:
			self.assertEqual(state, empty())
			state.update(offset=31, baseline_done=True, seen=["run:2026-10-24-001"],
				cards={"q-" + "1" * 32: local_card()})
		with Store().locked() as state:
			self.assertEqual(state["offset"], 31)
			self.assertEqual(len(state["cards"]), 1)
			self.assertTrue(state["baseline_done"])
		folder = self.hub / "run"
		self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
		for path in folder.iterdir():
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual({p.name for p in folder.iterdir()}, {".telegram.lock", "telegram-state.json"})

	def test_corrupt_invalid_deep_or_unreadable_state_starts_empty(self):
		store = Store()
		with store.locked():
			pass
		path = self.hub / "run/telegram-state.json"
		for data in ("[", "[" * 2000, "[]", "{}", json.dumps({**empty(), "offset": "1"}),
				json.dumps({**empty(), "baseline_done": 1}), json.dumps({**empty(), "seen": ["raw text"]}),
				json.dumps({**empty(), "cards": {"q-" + "1" * 32: {}}})):
			path.write_text(data)
			with store.locked() as state:
				self.assertEqual(state, empty())
		path.chmod(0o000)
		with store.locked() as state:
			self.assertEqual(state, empty())
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

	def test_seven_day_hundred_card_and_five_hundred_seen_limits(self):
		state = empty()
		state["seen"] = [f"run:2026-10-24-{i:03d}" for i in range(600)]
		state["cards"] = {"q-" + f"{i:032x}": local_card(i) for i in range(1, 105)}
		state["cards"]["q-" + "f" * 32] = local_card(500, age=8)
		checked(state)
		self.assertEqual(len(state["seen"]), 500)
		self.assertEqual(state["seen"][0], "run:2026-10-24-100")
		self.assertEqual(len(state["cards"]), 100)
		self.assertNotIn("q-" + "f" * 32, state["cards"])

	def test_linked_folder_state_lock_and_hardlinks_never_write_outside(self):
		outside = self.root / "outside"
		outside.mkdir()
		folder = self.hub / "run"
		folder.symlink_to(outside, target_is_directory=True)
		with self.assertRaises((OSError, ValueError)):
			with Store().locked():
				pass
		self.assertEqual(list(outside.iterdir()), [])
		folder.unlink()
		with Store().locked():
			pass
		for name in ("telegram-state.json", ".telegram.lock"):
			path = folder / name
			original = path.read_bytes()
			path.unlink()
			path.symlink_to(outside / name)
			with self.assertRaises((OSError, ValueError)):
				with Store().locked() as state:
					self.assertEqual(state, empty())
			self.assertEqual(list(outside.iterdir()), [])
			path.unlink()
			path.write_bytes(original)
			path.chmod(0o600)
		path = folder / "telegram-state.json"
		alias = outside / "copy"
		os.link(path, alias)
		before = alias.read_bytes()
		with self.assertRaises(ValueError):
			with Store().locked():
				pass
		self.assertEqual(alias.read_bytes(), before)

	def test_failed_replace_preserves_previous_state_and_cleans_temporary_file(self):
		store = Store()
		with store.locked() as state:
			state["offset"] = 7
		path = self.hub / "run/telegram-state.json"
		before = path.read_bytes()
		with mock.patch("office.telegram_state.os.replace", side_effect=OSError("합성 교체 실패")):
			with self.assertRaises(OSError):
				with store.locked() as state:
					state["offset"] = 9
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual({p.name for p in path.parent.iterdir()}, {"telegram-state.json", ".telegram.lock"})

	def test_separate_instances_serialize_updates_without_losing_offsets(self):
		def increment(_):
			with Store().locked() as state:
				state["offset"] += 1
		with ThreadPoolExecutor(4) as pool:
			list(pool.map(increment, range(30)))
		with Store().locked() as state:
			self.assertEqual(state["offset"], 30)

	def test_all_partial_sends_are_saved_even_when_the_transaction_raises(self):
		with self.assertRaises(RuntimeError):
			with Store().locked() as state:
				state["cards"]["q-" + "1" * 32] = copy.deepcopy(local_card())
				raise RuntimeError("합성 후속 전송 실패")
		with Store().locked() as state:
			self.assertEqual(len(state["cards"]), 1)

	def test_old_state_gains_asks_without_losing_offset_seen_or_pending_cards(self):
		old = {"offset": 31, "baseline_done": True, "seen": ["run:2026-10-24-001"],
			"cards": {"q-" + "1" * 32: local_card()}}
		with Store().locked():
			pass
		(self.hub / "run/telegram-state.json").write_text(json.dumps(old))
		with Store().locked() as state:
			self.assertEqual(state, {**old, "asks": {}})

	def test_only_fifty_most_recent_asks_within_seven_days_survive(self):
		state = empty()
		asks = [local_ask(i) for i in range(1, 56)] + [local_ask(100, age=8)]
		state["asks"] = {ask["id"]: ask for ask in asks}
		checked(state)
		self.assertEqual(len(state["asks"]), 50)
		self.assertNotIn(asks[0]["id"], state["asks"])
		self.assertNotIn(asks[-1]["id"], state["asks"])
		self.assertIn(asks[-2]["id"], state["asks"])
		with Store().locked() as saved:
			saved.update(state)
		with Store().locked() as saved:
			self.assertEqual(saved, state)

	def test_invalid_ask_ids_options_answers_and_bot_secret_are_rejected(self):
		row = local_ask()
		changes = ({"id": "k-bad"}, {"message_id": True}, {"options": ["승인"]},
			{"options": ["가" * 21, "거절"]}, {"options": [self.token, "거절"]},
			{"created": "2026-01-01"}, {"answer": {"kind": "command", "value": "apply", "at": row["created"]}},
			{"answer": {"kind": "option", "value": "다른 답", "at": row["created"]}},
			{"answer": {"kind": "text", "value": "가" * 1001, "at": row["created"]}},
			{"answer": {"kind": "text", "value": self.token, "at": row["created"]}})
		for change in changes:
			with self.subTest(fields=list(change)), self.assertRaises(ValueError):
				checked({**empty(), "asks": {row["id"]: {**row, **change}}})
