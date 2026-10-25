"""전이표의 전체 조합과 두 확인 지점을 검사한다."""

import copy
from unittest import mock

from shorts import Conflict, TransitionError, approvals, states
from test_shorts_support import ShortsCase


class StateTest(ShortsCase):
	def test_every_status_pair_against_independent_transition_table(self):
		expected = {
			"waiting": {"running", "blocked", "cancelling"},
			"running": {"succeeded", "awaiting_user", "blocked", "failed", "cancelling", "needs_check"},
			"awaiting_user": {"waiting", "cancelling"},
			"blocked": {"waiting", "cancelling", "needs_check"},
			"failed": {"waiting", "cancelling", "needs_check"},
			"cancelling": {"cancelled", "needs_check"}, "cancelled": set(),
			"succeeded": {"waiting"}, "needs_check": {"waiting", "cancelling"}}
		self.assertEqual(set(states.STATUSES), set(expected))
		for before in (*expected, "unknown"):
			for after in (*expected, "unknown"):
				with self.subTest(before=before, after=after):
					if after in expected.get(before, set()):
						states.check_transition(before, after)
					else:
						with self.assertRaises(TransitionError):
							states.check_transition(before, after)
		for stage in states.STAGES:
			for before in expected:
				for after in expected:
					run = copy.deepcopy(self.run)
					run.update(stage=stage, status=before)
					allowed = (after in expected[before]
						and not (after == "running" and stage in ("voice", "final"))
						and not (after == "awaiting_user" and stage not in ("script", "preview"))
						and not (after == "succeeded" and stage in ("script", "preview")))
					with self.subTest(stage=stage, before=before, after=after):
						if allowed:
							states.transition(run, after, confirmed_end=True)
							self.assertEqual(run["status"], after)
						else:
							with self.assertRaises(TransitionError):
								states.transition(run, after, confirmed_end=True)
							self.assertEqual(run["status"], before)

	def test_stage_order_and_only_checkpoints_can_await_user(self):
		expected = ("intake", "prepare", "script", "voice", "scene_code", "preview", "final", "deliver")
		self.assertEqual(states.STAGES, expected)
		for index, stage in enumerate(expected):
			run = copy.deepcopy(self.run)
			run.update(stage=stage, status="running")
			with self.subTest(stage=stage):
				if stage in ("script", "preview"):
					states.transition(run, "awaiting_user")
					run["status"] = "running"
					with self.assertRaises(TransitionError):
						states.transition(run, "succeeded")
				else:
					with self.assertRaises(TransitionError):
						states.transition(run, "awaiting_user")
					states.transition(run, "succeeded")
					states.advance(run)
					self.assertEqual((run["stage"], run["status"]),
						(expected[index + 1], "waiting") if stage != "deliver" else ("deliver", "succeeded"))

	def test_sequential_round_finishes_only_after_two_explicit_approvals(self):
		self.to_stage("deliver")
		self.call(approvals.start_stage)
		run = self.call(approvals.complete_stage)
		self.assertEqual((run["stage"], run["status"]), ("deliver", "succeeded"))
		self.assertTrue(all(run["approvals"].values()))
		with self.assertRaises(TransitionError):
			self.call(approvals.start_stage)

	def test_no_response_time_or_general_words_do_not_approve(self):
		run = self.pending()
		path = self.round_path() / "run.json"
		before = path.read_bytes()
		with mock.patch("shorts.schema.now", return_value="2099-01-01T12:00:00+09:00"):
			self.assertEqual(self.read(), run)
			for digest in ("", "좋아요", "추천대로", None):
				with self.assertRaises(Conflict):
					self.call(approvals.approve, "script", digest, run["revision"])
			with self.assertRaises(TransitionError):
				self.call(approvals.start_stage)
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual(self.read()["status"], "awaiting_user")

	def test_advance_and_cancel_cannot_skip_preconditions(self):
		for status in ("waiting", "running", "awaiting_user", "failed", "blocked", "needs_check", "cancelled"):
			run = copy.deepcopy(self.run)
			run["status"] = status
			with self.assertRaises(TransitionError):
				states.advance(run)
		run = copy.deepcopy(self.run)
		run["status"] = "cancelling"
		with self.assertRaises(TransitionError):
			states.transition(run, "cancelled")
		for invalid in ([], {}, None, 1, True):
			with self.assertRaises(TransitionError):
				states.check_transition(invalid, "running")
			with self.assertRaises(TransitionError):
				states.check_transition("waiting", invalid)
