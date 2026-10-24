"""회의는 합성 설정·규칙과 순수 호출 대역만 사용한다."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from collector import vault
from collector.config import ConfigError
from orchestrator import meeting, meeting_prompts, targets
from orchestrator.providers import Reply


def question(identity="scope"):
	return {"id": identity, "text": "어느 범위로 진행할까요?", "options": [
		{"id": "a", "label": "작게", "difference": "한 화면부터 점검", "reply": "A안으로 가"},
		{"id": "b", "label": "넓게", "difference": "전체 화면을 점검", "reply": "B안으로 가"}],
		"recommended": "a", "reason": "빠르게 확인할 수 있음", "allow_delegate": True}


class FakeAI:
	def __init__(self, roles=("backend", "qa")):
		self.roles, self.calls = roles, []
		self.analysis_questions, self.round_questions = [], {}
		self.fail_at = None

	def __call__(self, plan, **kwargs):
		self.calls.append((plan, kwargs))
		if self.fail_at == len(self.calls):
			raise RuntimeError("외부 원문은 기록하지 않음")
		properties = kwargs["schema"]["properties"]
		if "participants" in properties:
			data = {"summary": "합성 안건의 방법을 비교한다", "participants": [
				{"role": role, "reason": "서로 다른 관점의 점검"} for role in self.roles],
				"questions": copy.deepcopy(self.analysis_questions)}
		elif "method" in properties:
			role = properties["role"]["enum"][0]
			data = {"role": role, "method": "작은 범위부터 점검한다", "risks": ["누락 가능성"],
				"changes": ["점검 항목을 정한다"], "questions": copy.deepcopy(self.round_questions.get(role, []))}
		elif "agree" in properties:
			role = properties["role"]["enum"][0]
			other = properties["agree"]["items"]["properties"]["role"]["enum"][0]
			data = {"role": role, "agree": [{"role": other, "text": "작은 범위부터 점검한다"}],
				"disagree": [], "supplement": []}
		else:
			context = json.loads(kwargs["prompt"].split("\n담당: moderator\n", 1)[1].splitlines()[0])
			ref = next(ref for ref in context["발언 출처"] if ref.startswith("r1:"))
			item = {"text": "작은 범위부터 점검한다", "refs": [ref]}
			data = {"conclusion": [item], "todos": [], "open_questions": [], "decisions": []}
		return Reply(data, .01, {})


class MeetingCase(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.hub, self.repo, self.vault = (self.root / name for name in ("hub", "repo", "vault"))
		(self.hub / "redaction").mkdir(parents=True)
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "synthetic-corp", "kind": "회사명", "token": "company01"}]}))
		self.repo.mkdir()
		subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
		(self.repo / "AGENTS.md").write_text("개인 프로젝트. 루트 RULES.md가 정본이다.")
		(self.repo / "RULES.md").write_text("원천은 읽기만 한다. 코드와 Git을 바꾸지 않는다.")
		self.plan_text = '''[[plans]]
id="p01"
tool="codex"
owner="personal"
allow=["personal"]
[orchestration]
implementer=["p01"]
[orchestration.meeting]
plan=["p01"]
backend=["p01"]
frontend=["p01"]
qa=["p01"]
'''
		self.write_plans()
		(self.hub / "sources.toml").write_text("vault=" + json.dumps(str(self.vault)))
		vault.daily_path(self.vault, "personal", date.today()).parent.mkdir(parents=True)
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="synthetic-user"),
				mock.patch.object(targets, "REPO", self.repo)):
			patch.start()
			self.addCleanup(patch.stop)
		self.ai = FakeAI()
		patch = mock.patch("orchestrator.providers.call", side_effect=self.ai)
		self.mock_call = patch.start()
		self.addCleanup(patch.stop)
		self.engine = meeting.Engine()

	def write_plans(self, text=None):
		(self.hub / "plans.toml").write_text(self.plan_text if text is None else text)

	def analyze(self):
		return self.engine.analyze("합성 개인 화면 개선 방법", "ai-hub")

	def start(self, state):
		return self.engine.start(state["id"], list(self.ai.roles))


class MeetingTest(MeetingCase):
	def test_two_to_four_participants_and_all_calls_are_read_only(self):
		for roles in (("backend", "qa"), ("plan", "backend", "qa"), ("plan", "backend", "frontend", "qa")):
			with self.subTest(roles=roles):
				self.ai.roles = roles
				state = self.analyze()
				self.assertEqual(state["status"], "participants_pending")
				self.assertFalse(state["confirmed"])
				self.assertEqual(len(state["calls"]), 1)
				state = self.start(state)
				self.assertEqual(state["status"], "completed")
				self.assertEqual(len(state["calls"]), len(roles) * 2 + 2)
				self.assertEqual([r["role"] for r in state["rounds"]["1"]], list(roles))
				self.assertEqual([r["role"] for r in state["rounds"]["2"]], list(roles))
				self.assertEqual(state["journal"], "written")
			for _, call in self.ai.calls:
				self.assertIs(call["writer"], False)
				self.assertNotEqual(call["cwd"], self.repo)
				self.assertIn("정본", call["prompt"])
				self.assertNotIn(str(self.repo), call["prompt"])

	def test_resume_and_wrong_participants_never_start_discussion(self):
		state = self.analyze()
		self.assertEqual(self.engine.resume(state["id"])["status"], "participants_pending")
		with self.assertRaises(ValueError):
			self.engine.start(state["id"], ["backend", "design"])
		self.assertEqual(len(self.ai.calls), 1)

	def test_confirmed_participants_can_add_exclude_or_replace_configured_roles(self):
		for proposed, confirmed in ((["backend", "qa"], ["backend", "frontend", "qa"]),
				(["backend", "frontend", "qa"], ["backend", "qa"]),
				(["backend", "qa"], ["frontend", "qa"])):
			with self.subTest(proposed=proposed, confirmed=confirmed):
				self.ai.roles = proposed
				state = self.analyze()
				proposal = copy.deepcopy(state["analysis"]["participants"])
				self.ai.roles = confirmed
				state = self.engine.start(state["id"], confirmed)
				self.assertEqual(state["status"], "completed")
				self.assertEqual(state["analysis"]["participants"], proposal)
				self.assertEqual([p["role"] for p in state["participants"]], confirmed)
				for number in ("1", "2"):
					self.assertEqual([p["role"] for p in state["rounds"][number]], confirmed)
				confirmation = state["participant_confirmation"]
				self.assertEqual(confirmation["roles"], confirmed)
				self.assertEqual(confirmation["proposed"], proposal)
				self.assertEqual(confirmation["source"], "cli")
				for action, roles in (("added", set(confirmed) - set(proposed)),
						("excluded", set(proposed) - set(confirmed))):
					self.assertEqual({p["role"] for p in confirmation["adjustments"] if p["action"] == action}, roles)
				for change in confirmation["adjustments"]:
					self.assertTrue(change["reason"])
					self.assertEqual(change["source"], "cli")
				minutes = (self.hub / "meetings" / state["id"] / "minutes.md").read_text()
				self.assertIn("참석자 확인·조정·출처", minutes)
				self.assertIn('"adjustments"', minutes)

	def test_invalid_confirmed_participants_leave_proposal_pending_without_calls(self):
		state = self.analyze()
		for roles in ([], ["backend"], ["plan", "backend", "frontend", "qa", "design"],
				["backend", "backend"], ["backend", "company"], ["backend", "design"],
				["backend", {}], "backend qa"):
			with self.subTest(roles=roles), self.assertRaises(ValueError):
				self.engine.start(state["id"], roles)
			self.assertEqual(self.engine.status(state["id"]), state)
		self.assertEqual(len(self.ai.calls), 1)

	def test_bad_ai_schema_roles_lengths_and_summary_evidence_fail_closed(self):
		baseline = {"summary": "안건을 비교한다", "participants": [
			{"role": "backend", "reason": "구현 관점"}, {"role": "qa", "reason": "검증 관점"}], "questions": []}
		for change in ({"participants": [{"role": "company", "reason": "검토"}] * 2},
				{"participants": [{"role": "qa", "reason": "검토"}] * 2},
				{"summary": "a" * 2001}, {"command": "무언가 실행"}):
			with mock.patch("orchestrator.providers.call", return_value=Reply({**baseline, **change}, 0, {})):
				state = self.analyze()
			self.assertEqual(state["status"], "failed")
			self.assertEqual(state["participants"], [])
		state = self.analyze()
		original = self.ai.__call__
		def fabricated(plan, **kwargs):
			reply = original(plan, **kwargs)
			if "conclusion" in kwargs["schema"]["properties"]:
				reply.data["conclusion"][0]["text"] = "발언에 없는 결정"
			return reply
		with mock.patch("orchestrator.providers.call", side_effect=fabricated):
			state = self.start(state)
		self.assertEqual(state["status"], "failed")
		self.assertIsNone(state["summary"])

	def test_bad_round_one_or_two_never_becomes_completed_or_runs_commands(self):
		original = self.ai.__call__
		for phase in ("round1", "round2"):
			state = self.analyze()
			def invalid(plan, **kwargs):
				reply = original(plan, **kwargs)
				fields = kwargs["schema"]["properties"]
				if phase == "round1" and "method" in fields:
					reply.data["method"] = "synthetic-corp 정보"
				elif phase == "round2" and "agree" in fields:
					reply.data["agree"] = [{"role": reply.data["role"], "text": "본인 발언"}]
				return reply
			with mock.patch("orchestrator.providers.call", side_effect=invalid), \
					mock.patch("orchestrator.checks.git", side_effect=AssertionError("Git 금지")), \
					mock.patch("subprocess.run", side_effect=AssertionError("실행 금지")):
				state = self.start(state)
			self.assertEqual(state["status"], "failed")
			self.assertIsNone(state["summary"])
			self.assertFalse(state["calls"][-1]["ok"])

	def test_only_root_rules_are_read_and_links_or_unsafe_rules_refused_before_ai(self):
		(self.repo / "RULES.md").unlink()
		outside = self.root / "outside"
		outside.write_text("읽으면 안 되는 합성 내용")
		(self.repo / "RULES.md").symlink_to(outside)
		with self.assertRaises((ValueError, OSError, ConfigError)):
			self.analyze()
		self.mock_call.assert_not_called()

	def test_rules_with_external_references_are_not_followed_and_sensitive_rules_are_masked(self):
		(self.repo / "AGENTS.md").write_text("정본 RULES.md만 적용. 다른 문서는 outside.md를 참고한다.")
		outside = self.repo / "outside.md"
		outside.write_text("synthetic-corp 비공개 합성 자료")
		self.assertEqual(self.analyze()["status"], "participants_pending")
		self.assertNotIn("비공개", self.ai.calls[-1][1]["prompt"])
		for value in ("synthetic-corp 규칙", "password=" + "d" * 24, "/" + "home/other-user/data"):
			(self.repo / "RULES.md").write_text(value)
			self.assertEqual(self.analyze()["status"], "participants_pending")
			prompt = self.ai.calls[-1][1]["prompt"]
			self.assertNotIn(value, prompt)
			self.assertIn("[보안값]", prompt)

	def test_journal_retry_does_not_repeat_ai_or_create_landing_folder(self):
		state = self.analyze()
		landing = vault.daily_path(self.vault, "personal", date.today()).parent
		landing.rmdir()
		state = self.start(state)
		self.assertEqual(state["status"], "record_pending")
		self.assertFalse(landing.exists())
		count = len(self.ai.calls)
		landing.mkdir()
		state = self.engine.resume(state["id"])
		self.assertEqual(state["status"], "completed")
		self.assertEqual(len(self.ai.calls), count)
		journal = next(landing.glob("*.md")).read_text()
		self.assertEqual(journal.count("ai-hub:meeting:" + state["id"]), 1)
