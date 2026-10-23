import unittest
from types import SimpleNamespace

from collector import backfill


class FakeStore:
	def __init__(self, used, rows):
		self.rows_ = rows
		self.conn = SimpleNamespace(execute=lambda *args: [(key,) for key in used])

	def get(self, key):
		return self.rows_.get(key)


class ChooseTasksTest(unittest.TestCase):
	def test_recorded_sessions_do_not_count_toward_batch_size(self):
		tasks = [{"key": f"codex:{i}", "hash": f"h{i}", "type": "session"} for i in range(15)]
		rows = {f"codex:{i}": {"hash": f"h{i}", "status": "recorded"} for i in range(10)}
		rows["codex:3"] = {"hash": "old", "status": "recorded"}
		rows["codex:4"] = {"hash": "h4", "status": "minimal"}
		store = FakeStore({"codex:0"}, rows)
		scope = {"id": "acme", "kind": "sessions", "data": {"tasks": tasks}}
		# 새로 처리할 대화는 3·4·10 세 건, 그 사이의 기록된 대화는 함께 담기지만 세지 않는다.
		chosen = [t["key"] for t in backfill.choose_tasks(store, scope, 3)]
		self.assertEqual(chosen, [f"codex:{i}" for i in range(1, 11)])


if __name__ == "__main__":
	unittest.main()
