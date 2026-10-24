"""합성 회의·가짜 명령으로 쓰기 창구의 형식·재확인·비밀 경계를 검사한다."""

import copy
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

from office import actions, meetings
from office.common import action_id
from orchestrator import meeting_record
from test_orchestrator_draft import DraftCase

MID = "m-" + "a" * 32
CID = "q-" + "b" * 32


def meeting_state(status="questions_pending", identity=MID):
	state = meeting_record.new("합성 안건 본문", "ai-hub")
	state.update(id=identity, status=status, created="2026-10-24T10:00:00+09:00",
		participants=[{"role": "backend", "reason": "합성 서버 검토"}, {"role": "qa", "reason": "합성 시험 검토"}])
	state["cards"] = [{"id": CID, "turn": 1, "status": "waiting", "source": "analysis", "answers": [], "summary": "",
		"questions": [{"id": "scope", "text": "합성 질문 본문", "reason": "합성 추천 이유", "recommended": "a", "allow_delegate": True,
			"options": [{"id": "a", "label": "A안", "difference": "합성 작은 범위", "reply": "A안으로 가"},
				{"id": "b", "label": "B안", "difference": "합성 넓은 범위", "reply": "B안으로 가"}]}]}]
	return state


def write_meeting(root, state):
	folder = root / "meetings" / state["id"]
	folder.mkdir(mode=0o700, parents=True, exist_ok=True)
	path = folder / "meeting.json"
	path.write_text(json.dumps(state, ensure_ascii=False))
	path.chmod(0o600)
	return path


class MeetingCase(DraftCase):
	def setUp(self):
		super().setUp()
		with (self.hub / "plans.toml").open("a") as stream:
			stream.write('\n[orchestration.meeting]\nbackend=["p01"]\nqa=["p02"]\nplan=["p01"]\ndesign=["p02"]\n')
		self.agenda = {"action": "meeting_analyze", "id": "ai-hub", "text": "합성 사용자 안건 원문", "origin": 4}
		self.answer = {"action": "meeting_answer", "id": MID, "card": CID,
			"answers": [{"question_id": "scope", "kind": "text", "value": "합성 사용자 답변 원문"}]}
		self.start = {"action": "meeting_start", "id": MID, "participants": ["backend", "qa"]}
		self.confirm = {"action": "meeting_confirm", "id": MID, "card": CID}
		for patch in (mock.patch.object(meetings.subprocess, "run", side_effect=AssertionError("실제 명령 금지")),
				mock.patch.object(meetings.subprocess, "Popen", side_effect=AssertionError("실제 명령 금지")),
				mock.patch("socket.create_connection", side_effect=AssertionError("실제 연결 금지"))):
			patch.start()
			self.addCleanup(patch.stop)


class OfficeMeetingsTest(MeetingCase):
	def test_analyze_exact_command_temp_mode_deletion_draft_and_private_audit(self):
		state = meeting_state("participants_pending")
		state["cards"][0]["summary"] = "합성 대기 요약"
		state["rounds"]["1"] = ["응답에 포함하지 않을 합성 발언"]
		paths = []
		def run(argv, **options):
			path = Path(argv[-1])
			paths.append(path)
			self.assertEqual(argv[:-1], [sys.executable, "-m", "orchestrator", "meeting", "analyze", "--target", "ai-hub", "--text-file"])
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			self.assertEqual(path.read_text(), self.agenda["text"])
			self.assertEqual(options["timeout"], 900)
			self.assertIs(options["shell"], False)
			self.assertEqual(options["cwd"], actions.REPO)
			self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
			self.assertEqual(options["env"]["PYTHONPATH"], str(actions.REPO))
			self.assertEqual(options["stderr"], subprocess.PIPE)
			return subprocess.CompletedProcess(argv, 0, json.dumps(state), "")
		with mock.patch.object(meetings.subprocess, "run", side_effect=run) as command:
			status, value = actions.apply(self.agenda)
		self.assertEqual(status, 200, value)
		command.assert_called_once()
		self.assertEqual(value, {"ok": True, "message": "회의 참석자 제안을 확인해 주세요.", "output": "", "restart_needed": False,
			"draft": {"id": MID, "status": "participants_pending", "participants": state["participants"],
				"cards": [{"id": CID, "turn": 1, "summary": "합성 대기 요약"}]}})
		self.assertFalse(paths[0].exists())
		log = self.hub / "logs/office-actions.jsonl"
		self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
		entry = json.loads(log.read_text())
		self.assertEqual(set(entry), {"at", "action", "id", "exit_code", "seconds", "ok", "message"})
		self.assertTrue(entry["ok"])
		for text in (self.agenda["text"], state["agenda"], state["cards"][0]["questions"][0]["text"]):
			self.assertNotIn(text, log.read_text())

	def test_shapes_ids_lengths_unknown_targets_and_origin_never_run_commands(self):
		bad = []
		for payload in (self.agenda, self.start, self.answer, self.confirm):
			bad += [{**payload, "extra": "xyz"}]
			bad += [{k: v for k, v in payload.items() if k != key} for key in payload]
			bad += [{**payload, "id": value} for value in (None, [], {}, 1, "../xyz", "", "m-" + "A" * 32)]
		bad += [{**self.agenda, "id": value} for value in ("missing", "Meeting", "xyz/abc", "a" * 32)]
		bad += [{**self.agenda, "text": value} for value in ("", " ", "a" * 2001, "a\x00b", "a\x1bb", "\ud800", [], 1, None)]
		bad += [{**self.agenda, "origin": value} for value in (None, "4", 4.0, True, {}, -1, 999)]
		bad += [{**self.start, "participants": value} for value in (None, "backend", [], ["qa"], ["qa", "qa"],
			["backend", []], ["backend", "missing"], ["plan", "backend", "qa", "design", "frontend"])]
		for payload in (self.answer, self.confirm):
			bad += [{**payload, "card": value} for value in (None, [], "q-" + "B" * 32, MID, CID + "/", "../xyz")]
		bad += [{**self.answer, "answers": value} for value in (None, {}, [], [{}] * 4,
			[{"question_id": "scope", "kind": "text", "value": "a" * 2001}],
			[{"question_id": "../xyz", "kind": "text", "value": "합성"}],
			[{"question_id": "scope", "kind": "unknown", "value": "합성"}],
			[{"question_id": "scope", "kind": "text", "value": "합성", "source": "telegram"}])]
		with mock.patch.object(meetings.subprocess, "run") as run, mock.patch.object(meetings.subprocess, "Popen") as popen:
			for payload in bad:
				with self.subTest(payload=repr(payload)[:150]):
					self.assertEqual(actions.apply(payload)[0], 400)
			run.assert_not_called()
			popen.assert_not_called()
		self.assertFalse((self.hub / "logs/office-actions.jsonl").exists())
		for action in ("meeting_start", "meeting_answer", "meeting_confirm"):
			self.assertEqual(action_id(action, MID), MID)
		self.assertEqual(action_id("meeting_card", CID), CID)

	def test_origin_uses_scene_people_and_two_thousand_characters_are_accepted(self):
		with mock.patch.object(actions, "scene", return_value={"people": [{"id": 42}]}), \
				mock.patch.object(meetings, "execute", return_value=(200, actions.result(True, "합성"))) as execute:
			self.assertEqual(actions.apply(self.agenda)[0], 400)
			execute.assert_not_called()
			payload = {**self.agenda, "origin": 42, "text": "가" * 2000}
			self.assertEqual(actions.apply(payload)[0], 200)
			self.assertEqual(execute.call_args.args[1], payload)

	def test_sensitive_agendas_are_refused_before_ai_with_a_safe_reason(self):
		values = ("acme 안건", "xyz 안건", "토큰 " + "ghp_" + "a" * 36,
			"연락 " + "p01" + "@" + "acme.invalid", "/" + "home/p01/private", "/" + "home/p02/private",
			"~" + "/private", "C:" + "\\Users\\p02\\private")
		with mock.patch.object(meetings.subprocess, "run") as run:
			for text in values:
				status, value = actions.apply({**self.agenda, "text": text})
				self.assertEqual(status, 400)
				self.assertIn("회사·서비스 이름", value["message"])
				self.assertIn("포함돼 거부했습니다", value["message"])
				self.assertNotIn(text, json.dumps(value, ensure_ascii=False))
			run.assert_not_called()

	def test_answer_sends_private_json_source_office_and_returns_only_summary(self):
		original = meeting_state()
		path = write_meeting(self.hub, original)
		paths = []
		def run(argv, **options):
			text_file = Path(argv[-3])
			self.assertEqual(argv, [sys.executable, "-m", "orchestrator", "meeting", "answer", MID,
				"--card", CID, "--answers-file", str(text_file), "--source", "office"])
			paths.append(text_file)
			self.assertEqual(stat.S_IMODE(text_file.stat().st_mode), 0o600)
			self.assertEqual(json.loads(text_file.read_text()), self.answer["answers"])
			self.assertIs(options["shell"], False)
			self.assertEqual(options["timeout"], 900)
			state = copy.deepcopy(original)
			state["status"] = "answers_pending"
			state["cards"][0].update(status="answered", summary="합성 확인 요약",
				answers=[{**self.answer["answers"][0], "source": "office"}])
			return subprocess.CompletedProcess(argv, 0, json.dumps(state), "")
		before = path.read_bytes()
		with mock.patch.object(meetings.subprocess, "run", side_effect=run):
			status, value = actions.apply(self.answer)
		self.assertEqual(status, 200, value)
		self.assertEqual(value["draft"], {"id": MID, "card": CID, "summary": "합성 확인 요약"})
		self.assertFalse(paths[0].exists())
		self.assertEqual(path.read_bytes(), before)
		audit = (self.hub / "logs/office-actions.jsonl").read_text()
		for text in (original["agenda"], self.answer["answers"][0]["value"], original["cards"][0]["questions"][0]["text"], "합성 확인 요약"):
			self.assertNotIn(text, audit)

	def test_card_and_state_rechecks_reject_stale_or_already_answered_requests(self):
		with mock.patch.object(meetings.subprocess, "run") as run:
			for source, label in (("telegram", "텔레그램에서"), ("office", "사무실·명령줄에서"), ("cli", "사무실·명령줄에서")):
				for status in ("answers_pending", "running", "completed"):
					state = meeting_state(status)
					state["cards"][0].update(status="answered", answers=[{"source": source}])
					write_meeting(self.hub, state)
					code, value = actions.apply(self.answer)
					self.assertEqual((code, value["message"]), (409, "이미 답했습니다(" + label + ")"))
			for status in ("participants_pending", "running", "completed", "failed"):
				write_meeting(self.hub, meeting_state(status))
				self.assertEqual(actions.apply(self.answer)[0], 409)
			state = meeting_state()
			state["cards"].append({**copy.deepcopy(state["cards"][0]), "id": "q-" + "c" * 32, "turn": 2})
			write_meeting(self.hub, state)
			self.assertEqual(actions.apply(self.answer)[0], 409)
			self.assertEqual(actions.apply({**self.answer, "card": "q-" + "d" * 32})[0], 409)
			run.assert_not_called()

	def test_invalid_answer_count_choice_delegate_ids_and_sensitive_text_never_run(self):
		state = meeting_state()
		state["cards"][0]["questions"][0]["allow_delegate"] = False
		write_meeting(self.hub, state)
		values = ([{"question_id": "other", "kind": "text", "value": "합성"}], self.answer["answers"] * 2,
			[{"question_id": "scope", "kind": "choice", "value": "missing"}],
			[{"question_id": "scope", "kind": "delegate", "value": "알아서"}],
			[{"question_id": "scope", "kind": "text", "value": "네"}],
			[{"question_id": "scope", "kind": "text", "value": "acme 내용"}])
		with mock.patch.object(meetings.subprocess, "run") as run:
			for answers in values:
				self.assertEqual(actions.apply({**self.answer, "answers": answers})[0], 400)
			run.assert_not_called()

	def test_all_answer_kinds_use_the_engine_validator_without_changing_records(self):
		from orchestrator import question_cards
		state = meeting_state()
		path = write_meeting(self.hub, state)
		before = path.read_bytes()
		for kind, value in (("choice", "a"), ("choice", "B안으로 가"), ("text", "구체적인 합성 답"), ("delegate", "알아서")):
			payload = {**self.answer, "answers": [{"question_id": "scope", "kind": kind, "value": value}]}
			def run(argv, **options):
				result = copy.deepcopy(state)
				question_cards.accept(result["cards"][0], CID, payload["answers"], "office", meetings.Rules())
				result["status"] = "answers_pending"
				return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
			with mock.patch.object(meetings.subprocess, "run", side_effect=run) as command:
				status, result = actions.apply(payload)
			self.assertEqual(status, 200, result)
			self.assertTrue(result["draft"]["summary"])
			command.assert_called_once()
			self.assertEqual(path.read_bytes(), before)

	def test_draft_projection_masks_sensitive_ai_text_and_omits_private_fields(self):
		state = meeting_state("participants_pending")
		state["participants"][0]["reason"] = "acme " + "ghp_" + "a" * 36
		state["cards"][0]["summary"] = "p01" + "@" + "acme.invalid /" + "home/p01/private"
		state["cards"][0]["answers"] = [{"source": "office", "value": "합성 숨길 답 원문"}]
		with mock.patch.object(meetings.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(state), "")):
			status, result = actions.apply(self.agenda)
		self.assertEqual(status, 200, result)
		encoded = json.dumps(result, ensure_ascii=False)
		for raw in ("acme", "ghp_", "@", "/home/", "합성 숨길 답 원문"):
			self.assertNotIn(raw, encoded)
		self.assertIn("[보안값]", encoded)

	def test_telegram_answer_between_recheck_and_command_returns_source_conflict(self):
		state = meeting_state()
		write_meeting(self.hub, state)
		def race(*args, **kwargs):
			state["status"] = "answers_pending"
			state["cards"][0].update(status="answered", answers=[{"source": "telegram"}])
			write_meeting(self.hub, state)
			return subprocess.CompletedProcess([], 2, "합성 답 원문", "회의 처리 실패: ValueError")
		with mock.patch.object(meetings.subprocess, "run", side_effect=race):
			status, value = actions.apply(self.answer)
		self.assertEqual((status, value["message"]), (409, "이미 답했습니다(텔레그램에서)"))
		self.assertNotIn("합성 답 원문", json.dumps(value, ensure_ascii=False))

	def test_failures_and_timeouts_remove_input_and_do_not_log_reason_or_raw_text(self):
		for failure, expected in ((subprocess.CompletedProcess([], 2, "합성 원문", "합성 원문\n회의 처리 실패: ValueError"), 500),
				(subprocess.TimeoutExpired([], 900, output="합성 원문", stderr="합성 원문"), 504), (OSError("합성 원문"), 409),
				(subprocess.CompletedProcess([], 0, "{", ""), 409)):
			paths = []
			def run(argv, **options):
				paths.append(Path(argv[-1]))
				if isinstance(failure, Exception):
					raise failure
				return failure
			with mock.patch.object(meetings.subprocess, "run", side_effect=run):
				status, value = actions.apply(self.agenda)
			self.assertEqual(status, expected, value)
			self.assertFalse(paths[0].exists())
			self.assertNotIn("합성 원문", json.dumps(value, ensure_ascii=False))
			if expected == 500:
				self.assertTrue(value["message"].endswith("회의 처리 실패: ValueError"))
		log = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assertNotIn("합성 원문", log)
		self.assertNotIn("ValueError", log)

	def test_failure_reason_is_only_last_prefixed_line_and_masks_sensitive_data(self):
		for stderr in ("회의 처리 실패: acme 원문", "회의 처리 실패: 앞줄\n원문", "원문", "회의 처리 실패: " + "ghp_" + "a" * 36,
			"회의 처리 실패: /" + "home/p01/private", "회의 처리 실패: p01" + "@" + "acme.invalid"):
			with mock.patch.object(meetings.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "원문", stderr)):
				status, value = actions.apply(self.agenda)
			self.assertEqual(status, 500)
			for forbidden in ("acme", "원문", "ghp_", "/home/", "@"):
				self.assertNotIn(forbidden, value["message"])

	def test_existing_global_lock_and_unsafe_audit_prevent_all_meeting_execution(self):
		with actions.private_file(self.hub, "run", "office-action.lock") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			for payload in (self.agenda, self.start, self.answer, self.confirm):
				self.assertEqual(actions.apply(payload), (409, actions.result(False, "이미 실행 중")))
		(self.hub / "logs").mkdir(exist_ok=True)
		(self.hub / "logs/office-actions.jsonl").symlink_to(self.root / "outside")
		self.assertEqual(actions.apply(self.agenda)[0], 409)
		self.assertFalse((self.root / "outside").exists())
