"""연결 지점이 등록 함수를 부르고, 실패해도 다음 함수와 진행을 막지 않는지 검사한다."""

import unittest

from agency import hooks


class AgencyHooksTest(unittest.TestCase):
	def setUp(self):
		hooks.clear()
		self.addCleanup(hooks.clear)

	def test_before_tick_calls_all_and_reports_only_failed_names(self):
		seen = []

		def broken():
			raise RuntimeError("xyz-secret")

		hooks.on_before_tick(broken)
		hooks.on_before_tick(lambda: seen.append("ok"))
		self.assertEqual(hooks.before_tick(), ["broken"])
		self.assertEqual(seen, ["ok"])

	def test_changed_skips_same_status_and_passes_arguments(self):
		seen = []
		hooks.on_changed(lambda identity, before, after: seen.append((identity, before, after)))
		self.assertEqual(hooks.changed("p-20261027-001", "검수", "검수"), [])
		self.assertEqual(seen, [])
		hooks.changed("p-20261027-001", "검수", "납품 대기")
		self.assertEqual(seen, [("p-20261027-001", "검수", "납품 대기")])


if __name__ == "__main__":
	unittest.main()
