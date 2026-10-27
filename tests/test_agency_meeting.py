"""합성 원장과 가짜 공급자로 대행사 자문·중단 복구를 검사한다."""

import copy
import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agency import meeting, project
from agency.departments import Department
from orchestrator import providers


class MeetingTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name).resolve()
		self.hub = self.root / "hub"
		(self.hub / "redaction").mkdir(parents=True)
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "synthetic-corp", "kind": "회사명", "token": "company01"}]}))
		self.plans = "".join(
			f'[[plans]]\nid="{identity}"\ntool="{tool}"\nowner="personal"\nallow=["personal"]\n'
			for identity, tool in (("one", "codex"), ("two", "claude"), ("pm", "codex"), ("forbidden", "agy"))
		) + '''[orchestration]
pm=["pm"]
implementer=["one"]
reviewer=["two"]
[orchestration.meeting]
backend=["one"]
qa=["two"]
'''
		(self.hub / "plans.toml").write_text(self.plans)
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="synthetic-user"),
				mock.patch("socket.socket.connect", side_effect=AssertionError("네트워크 금지"))):
			patch.start()
			self.addCleanup(patch.stop)
		self.department = Department("sample", "합성", "합성", ("sample",), ("one", "two", "pm"),
			True, "sample", (), "", (), (), {"calls": 12, "minutes": 10})
		self.calls = []
		self.barrier = threading.Barrier(2)
		self.reply = {"recommendation": "작은 단위로 진행", "reason": "검사가 쉽다",
			"counterexample": "단위 사이에 누락이 있을 수 있다",
			"facts": [{"source": "spec", "text": "합성 기능"}]}
		self.decision = {"chosen": "작은 단위로 진행", "rejected": "한 번에 수행하면 확인이 어렵다",
			"assumptions": "합성 환경을 사용할 수 있다", "verification": "합성 시험을 실행한다"}
		patch = mock.patch.object(providers, "call", side_effect=self.call)
		self.provider = patch.start()
		self.addCleanup(patch.stop)

	def new(self, budget=None, topic="합성 회의 주제"):
		row = project.new(self.department, "합성 프로젝트", "합성 요청")
		identity = row["id"]
		project.update_spec(identity, goal="합성 기능", target="합성 결과", out_of_scope="외부 실행",
			done_criteria=["합성 시험 통과"], budget=budget or self.department.budget)
		project.confirm_spec(identity, self.department)
		project.transition(identity, "계획")
		path = self.hub / "projects" / identity / "task.toml"
		path.write_text("합성 요청서")
		project.add_task(identity, "합성 작업", str(path))
		project.set_checkpoint(identity, "meeting", topic)
		return identity

	def call(self, plan, **kwargs):
		self.calls.append((plan, kwargs))
		self.assertFalse(kwargs["writer"])
		self.assertTrue(kwargs["cwd"].is_dir())
		self.assertEqual(list(kwargs["cwd"].iterdir()), [])
		self.assertEqual(stat.S_IMODE(kwargs["cwd"].stat().st_mode), 0o500)
		if "recommendation" in kwargs["schema"]["properties"]:
			self.barrier.wait(timeout=3)
			return providers.Reply(copy.deepcopy(self.reply), .25, {})
		return providers.Reply(copy.deepcopy(self.decision), .5, {})

	def test_parallel_same_safe_input_decision_and_idempotent_restart(self):
		identity = self.new()
		meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual(row["checkpoint"], {"step": "meeting_done", "detail": "합성 회의 주제"})
		self.assertEqual(row["decisions"], [{"topic": "합성 회의 주제", **self.decision}])
		self.assertEqual(row["used"]["calls"], 3)
		self.assertGreaterEqual(row["used"]["seconds"], 1.0)
		self.assertEqual(len(self.calls), 3)
		first, second, pm = [args for _, args in self.calls]
		self.assertEqual(first["prompt"], second["prompt"])
		self.assertNotEqual(first["cwd"], second["cwd"])
		self.assertNotIn("합성 프로젝트", first["prompt"])
		self.assertNotIn(str(self.root), first["prompt"])
		self.assertIn("작은 단위로 진행", pm["prompt"])
		self.assertNotIn("confirmed", row["meeting"])
		meeting.ensure(identity, self.department)
		self.assertEqual(len(self.calls), 3)
		self.assertEqual(project.load(identity)["decisions"], row["decisions"])

	def fallback_department(self):
		extra = "".join(
			f'[[plans]]\nid="{identity}"\ntool="{tool}"\nowner="personal"\nallow=["personal"]\n'
			for identity, tool in (("alternate", "agy"), ("pm-backup", "claude")))
		config = self.plans.replace('[orchestration]', extra + '[orchestration]')
		config = config.replace('backend=["one"]', 'backend=["one", "forbidden", "alternate"]')
		config = config.replace('pm=["pm"]', 'pm=["pm", "forbidden", "pm-backup"]')
		(self.hub / "plans.toml").write_text(config)
		return Department(**{**self.department.__dict__, "allow_plans":
			("one", "two", "pm", "alternate", "pm-backup")})

	def test_quota_uses_allowed_alternatives_for_advice_and_pm(self):
		department = self.fallback_department()
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace('pm=["pm",', 'pm=["one", "pm",'))
		identity = self.new()
		def call(plan, **kwargs):
			self.calls.append((plan, kwargs))
			reserved = project.load(identity)
			self.assertGreaterEqual(reserved["used"]["seconds"], kwargs["timeout"])
			self.assertGreaterEqual(reserved["used"]["calls"], len(self.calls))
			self.assertFalse(kwargs["writer"])
			self.assertEqual(stat.S_IMODE(kwargs["cwd"].stat().st_mode), 0o500)
			if "recommendation" in kwargs["schema"]["properties"] and plan.id in ("one", "two"):
				self.barrier.wait(timeout=3)
			if plan.id in ("one", "pm"):
				raise providers.ProviderError("exit", "합성 종료 (한도 소진)")
			data = self.reply if "recommendation" in kwargs["schema"]["properties"] else self.decision
			return providers.Reply(copy.deepcopy(data), .25, {})
		self.provider.side_effect = call
		meeting.ensure(identity, department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual({item["plan"] for item in row["meeting"]["members"]}, {"alternate", "two"})
		self.assertEqual(row["meeting"]["pm"], "pm-backup")
		self.assertEqual(row["used"]["calls"], 5)
		self.assertEqual({args["prompt"] for plan, args in self.calls if plan.id in ("one", "two", "alternate")},
			{self.calls[0][1]["prompt"]})
		self.assertNotIn("forbidden", [plan.id for plan, _ in self.calls])
		meeting.ensure(identity, department)
		self.assertEqual(self.provider.call_count, 5)

	def test_quota_exhaustion_and_budget_stop_without_duplicate_calls(self):
		for case in ("advice", "pm", "calls", "seconds"):
			with self.subTest(case=case):
				department = self.fallback_department()
				budget = {"calls": 3, "minutes": 10} if case == "calls" else None
				if case == "seconds":
					budget = {"calls": 12, "minutes": 2}
				identity = self.new(budget)
				self.barrier = threading.Barrier(2)
				self.calls.clear()
				def call(plan, **kwargs):
					self.calls.append(plan.id)
					if plan.id in ("one", "two"):
						self.barrier.wait(timeout=3)
					if plan.id == "one" or case == "advice" and plan.id == "alternate" or case == "pm" and plan.id in ("pm", "pm-backup"):
						raise providers.ProviderError("exit", "합성 종료 (한도 소진)")
					data = self.reply if "recommendation" in kwargs["schema"]["properties"] else self.decision
					return providers.Reply(copy.deepcopy(data), 95 if case == "seconds" else .25, {})
				self.provider.side_effect = call
				before = self.provider.call_count
				with self.assertRaises(ValueError):
					meeting.ensure(identity, department)
				row = project.load(identity)
				self.assertEqual(row["used"]["calls"], len(self.calls))
				self.assertEqual(row["decisions"], [])
				self.assertEqual(row["meeting"]["status"], "retry_advice" if case in ("calls", "seconds") else "failed")
				if case in ("calls", "seconds"):
					self.assertEqual(len(self.calls), 2)
				with self.assertRaises(ValueError):
					meeting.ensure(identity, department)
				self.assertEqual(self.provider.call_count - before, len(self.calls))

	def test_overlap_reassigns_same_input_opinion_without_duplicate_plan_calls(self):
		department = self.fallback_department()
		path = self.hub / "plans.toml"
		path.write_text(path.read_text().replace('backend=["one", "forbidden", "alternate"]', 'backend=["one", "two"]')
			.replace('qa=["two"]', 'qa=["two", "alternate"]'))
		identity = self.new()
		def call(plan, **kwargs):
			self.calls.append((plan.id, kwargs["prompt"]))
			if plan.id in ("one", "two"):
				self.barrier.wait(timeout=3)
			if plan.id == "one":
				raise providers.ProviderError("exit", "합성 종료 (한도 소진)")
			data = self.reply if "recommendation" in kwargs["schema"]["properties"] else self.decision
			return providers.Reply(copy.deepcopy(data), .25, {})
		self.provider.side_effect = call
		meeting.ensure(identity, department)
		row = project.load(identity)
		self.assertEqual([member["plan"] for member in row["meeting"]["members"]], ["two", "alternate"])
		self.assertEqual(sorted(plan for plan, _ in self.calls), ["alternate", "one", "pm", "two"])
		self.assertEqual(len({prompt for plan, prompt in self.calls if plan != "pm"}), 1)

	def test_uncertain_fallback_is_reserved_and_never_recalled(self):
		department = self.fallback_department()
		identity = self.new()
		def call(plan, **kwargs):
			if plan.id in ("one", "two"):
				self.barrier.wait(timeout=3)
			if plan.id == "one":
				raise providers.ProviderError("exit", "합성 종료 (한도 소진)")
			if plan.id == "alternate":
				raise KeyboardInterrupt
			return providers.Reply(copy.deepcopy(self.reply), .25, {})
		self.provider.side_effect = call
		with self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, department)
		row = project.load(identity)
		self.assertEqual((row["meeting"]["status"], row["used"]["calls"]), ("consulting", 3))
		self.assertGreaterEqual(row["meeting"]["reserved_seconds"], 30)
		with self.assertRaisesRegex(ValueError, "중단"):
			meeting.ensure(identity, department)
		self.assertEqual(project.load(identity)["meeting"]["status"], "uncertain")
		self.assertEqual(self.provider.call_count, 3)

	def test_pm_fallback_requires_budget_and_does_not_retry_exhausted_plan(self):
		department = self.fallback_department()
		identity = self.new({"calls": 3, "minutes": 10})
		def call(plan, **kwargs):
			if plan.id in ("one", "two"):
				self.barrier.wait(timeout=3)
			if plan.id == "pm":
				raise providers.ProviderError("exit", "합성 종료 (한도 소진)")
			return providers.Reply(copy.deepcopy(self.reply), .25, {})
		self.provider.side_effect = call
		for _ in range(2):
			with self.assertRaisesRegex(ValueError, "예산"):
				meeting.ensure(identity, department)
		row = project.load(identity)
		self.assertEqual((row["used"]["calls"], row["meeting"]["pm_exhausted"]), (3, ["pm"]))
		self.assertEqual(self.provider.call_count, 3)

	def test_disallowed_registered_roles_fall_back_to_explicit_order(self):
		(self.hub / "plans.toml").write_text(self.plans.replace('backend=["one"]', 'backend=["forbidden"]')
			.replace('qa=["two"]', 'qa=["forbidden"]'))
		identity = self.new()
		meeting.ensure(identity, self.department)
		self.assertEqual({plan.id for plan, _ in self.calls[:2]}, {"one", "two"})
		self.assertNotIn("forbidden", [plan.id for plan, _ in self.calls])

	def test_three_distinct_consultants_use_identical_input(self):
		(self.hub / "plans.toml").write_text(self.plans + 'plan=["pm"]\n')
		self.barrier = threading.Barrier(3)
		identity = self.new()
		meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(len(row["meeting"]["opinions"]), 3)
		self.assertEqual(row["used"]["calls"], 4)
		self.assertEqual(len({args["prompt"] for _, args in self.calls[:3]}), 1)

	def test_insufficient_allowed_plans_and_forbidden_department_never_call(self):
		identity = self.new()
		disallowed = Department(**{**self.department.__dict__, "external_ai": False})
		with self.assertRaisesRegex(ValueError, "외부 AI"):
			meeting.ensure(identity, disallowed)
		one = Department(**{**self.department.__dict__, "allow_plans": ("one", "pm")})
		with self.assertRaisesRegex(ValueError, "2"):
			meeting.ensure(identity, one)
		self.provider.assert_not_called()
		self.assertEqual(project.load(identity)["used"]["calls"], 0)

	def test_pm_must_have_an_explicit_allowed_personal_plan_before_consulting(self):
		(self.hub / "plans.toml").write_text(self.plans.replace('pm=["pm"]', 'pm=["forbidden"]'))
		identity = self.new()
		with self.assertRaisesRegex(ValueError, "PM 요금제"):
			meeting.ensure(identity, self.department)
		self.provider.assert_not_called()
		self.assertEqual(project.load(identity)["used"]["calls"], 0)

	def test_budget_must_cover_all_calls_and_minimum_timeout(self):
		for budget in ({"calls": 2, "minutes": 10}, {"calls": 12, "minutes": 1}):
			with self.subTest(budget=budget):
				identity = self.new(budget)
				with self.assertRaisesRegex(ValueError, "예산"):
					meeting.ensure(identity, self.department)
				self.assertEqual(project.load(identity)["used"]["calls"], 0)
		self.provider.assert_not_called()

	def test_unsafe_agenda_or_spec_never_reaches_provider(self):
		values = ["synthetic-corp", "user" + "@" + "example.invalid", "~" + "/private",
			"/" + "Users/example/private", "ghp_" + "x" * 36]
		for value in values:
			with self.subTest(value=value[:8]):
				identity = self.new(topic=value)
				with self.assertRaises(ValueError):
					meeting.ensure(identity, self.department)
				identity = self.new()
				with project._edit(identity, "synthetic_spec") as row:
					row["spec"]["goal"] = value
					row["spec_hash"] = hashlib.sha256(json.dumps(row["spec"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
				with self.assertRaises(ValueError):
					meeting.ensure(identity, self.department)
		self.provider.assert_not_called()

	def test_failed_or_unfounded_advice_blocks_without_retry_or_pm(self):
		for failure in ("call", "fact"):
			with self.subTest(failure=failure):
				identity = self.new()
				self.calls.clear()
				self.barrier = threading.Barrier(2)
				if failure == "fact":
					self.reply["facts"][0]["text"] = "근거 없는 사실"
				else:
					def bad(plan, **kwargs):
						self.barrier.wait(timeout=3)
						raise providers.ProviderError("exit", "원문 기록 금지")
					self.provider.side_effect = bad
				with self.assertRaises(ValueError):
					meeting.ensure(identity, self.department)
				before = self.provider.call_count
				with self.assertRaises(ValueError):
					meeting.ensure(identity, self.department)
				self.assertEqual(self.provider.call_count, before)
				row = project.load(identity)
				self.assertEqual(row["meeting"]["status"], "failed")
				self.assertEqual(row["used"]["calls"], 2)
				self.assertEqual(row["decisions"], [])
				self.provider.side_effect = self.call

	def test_advice_cannot_contain_sensitive_output_or_write_files(self):
		for failure in ("output", "write"):
			with self.subTest(failure=failure):
				identity = self.new()
				self.barrier = threading.Barrier(2)
				if failure == "output":
					self.reply["reason"] = "user" + "@" + "example.invalid"
				else:
					self.reply["reason"] = "검사가 쉽다"
					def write(plan, **kwargs):
						kwargs["cwd"].chmod(0o700)
						(kwargs["cwd"] / "synthetic.txt").write_text("합성")
						kwargs["cwd"].chmod(0o500)
						self.barrier.wait(timeout=3)
						return providers.Reply(copy.deepcopy(self.reply), .25, {})
					self.provider.side_effect = write
				with self.assertRaises(ValueError):
					meeting.ensure(identity, self.department)
				row = project.load(identity)
				self.assertEqual(row["meeting"]["status"], "failed")
				self.assertEqual(row["used"]["calls"], 2)
				self.assertEqual(row["decisions"], [])
				self.provider.side_effect = self.call

	def test_one_consultant_failure_blocks_even_with_one_valid_opinion(self):
		identity = self.new()
		def partial(plan, **kwargs):
			self.barrier.wait(timeout=3)
			if plan.id == "one":
				raise providers.ProviderError("exit", "합성 오류")
			return providers.Reply(copy.deepcopy(self.reply), .25, {})
		self.provider.side_effect = partial
		with self.assertRaisesRegex(ValueError, "자문.*실패"):
			meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "failed")
		self.assertEqual(row["used"]["calls"], 2)
		self.assertEqual(self.provider.call_count, 2)
		with self.assertRaisesRegex(ValueError, "실패"):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 2)

	def test_actual_advice_time_over_budget_blocks_pm(self):
		identity = self.new()
		original = self.call
		def slow(plan, **kwargs):
			reply = original(plan, **kwargs)
			reply.seconds = 301
			return reply
		self.provider.side_effect = slow
		with self.assertRaises(ValueError):
			meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "failed")
		self.assertEqual(row["used"]["calls"], 2)
		self.assertEqual(row["used"]["seconds"], 602)
		self.assertEqual(len(self.calls), 2)

	def test_pm_time_shortfall_preserves_advice_without_repeating_it(self):
		identity = self.new({"calls": 12, "minutes": 2})
		original = self.call
		def slow(plan, **kwargs):
			reply = original(plan, **kwargs)
			reply.seconds = 46
			return reply
		self.provider.side_effect = slow
		with self.assertRaisesRegex(ValueError, "예산"):
			meeting.ensure(identity, self.department)
		self.assertEqual(project.load(identity)["meeting"]["status"], "advised")
		with self.assertRaisesRegex(ValueError, "예산"):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 2)

	def test_interrupted_reservation_is_charged_and_not_recalled(self):
		identity = self.new()
		with mock.patch.object(meeting, "_parallel", side_effect=KeyboardInterrupt):
			with self.assertRaises(KeyboardInterrupt):
				meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "consulting")
		self.assertEqual(row["used"]["calls"], 2)
		self.assertGreaterEqual(row["used"]["seconds"], 60)
		with self.assertRaisesRegex(ValueError, "중단"):
			meeting.ensure(identity, self.department)
		self.assertEqual(project.load(identity)["meeting"]["status"], "uncertain")
		self.provider.assert_not_called()

	def test_resume_after_advice_calls_only_pm(self):
		identity = self.new()
		with mock.patch.object(meeting, "_decide", side_effect=KeyboardInterrupt):
			with self.assertRaises(KeyboardInterrupt):
				meeting.ensure(identity, self.department)
		self.assertEqual(project.load(identity)["meeting"]["status"], "advised")
		self.assertEqual(len(self.calls), 2)
		meeting.ensure(identity, self.department)
		self.assertEqual(len(self.calls), 3)
		self.assertEqual(project.load(identity)["used"]["calls"], 3)

	def test_incomplete_saved_opinions_block_pm(self):
		identity = self.new()
		with mock.patch.object(meeting, "_decide", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, self.department)
		with project._edit(identity, "synthetic_corruption") as row:
			row["meeting"]["opinions"].pop()
		with self.assertRaisesRegex(ValueError, "인원"):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 2)

	def test_interrupted_pm_does_not_repeat_any_call(self):
		identity = self.new()
		original = self.call
		def interrupt(plan, **kwargs):
			if "chosen" in kwargs["schema"]["properties"]:
				raise KeyboardInterrupt
			return original(plan, **kwargs)
		self.provider.side_effect = interrupt
		with self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "deciding")
		self.assertEqual(row["used"]["calls"], 3)
		with self.assertRaisesRegex(ValueError, "중단"):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 3)

	def test_failed_pm_response_cannot_be_recorded_or_recalled(self):
		identity = self.new()
		self.decision["verification"] = ""
		with self.assertRaises(ValueError):
			meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "failed")
		self.assertEqual(row["checkpoint"]["step"], "meeting")
		self.assertEqual(row["decisions"], [])
		self.assertEqual(row["used"]["calls"], 3)
		with self.assertRaises(ValueError):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 3)

	def test_interruption_after_atomic_decision_does_not_duplicate(self):
		identity = self.new()
		write = project._write
		def interrupt(row, kind):
			write(row, kind)
			if kind == "meeting_decision":
				raise KeyboardInterrupt
		with mock.patch.object(project, "_write", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
			meeting.ensure(identity, self.department)
		row = project.load(identity)
		self.assertEqual(row["meeting"]["status"], "completed")
		self.assertEqual(row["checkpoint"]["step"], "meeting_done")
		self.assertEqual(len(row["decisions"]), 1)
		meeting.ensure(identity, self.department)
		self.assertEqual(len(project.load(identity)["decisions"]), 1)
		self.assertEqual(self.provider.call_count, 3)

	def test_inconsistent_completed_decision_is_not_silently_accepted(self):
		identity = self.new()
		meeting.ensure(identity, self.department)
		with project._edit(identity, "synthetic_corruption") as row:
			row["decisions"][0]["chosen"] = "다른 결과"
		with self.assertRaisesRegex(ValueError, "일치하지"):
			meeting.ensure(identity, self.department)
		self.assertEqual(self.provider.call_count, 3)

	def test_checkpoint_cannot_be_overwritten_before_decision(self):
		identity = self.new()
		with self.assertRaises(ValueError):
			project.set_checkpoint(identity, "branch", "합성")
		with self.assertRaises(ValueError):
			project.update_task(identity, "t1", status="실행 중")
		self.assertEqual(project.load(identity)["checkpoint"]["step"], "meeting")


if __name__ == "__main__":
	unittest.main()
