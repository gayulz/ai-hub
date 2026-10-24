"""임시 초안과 가짜 subprocess로 개발 지시의 저장·실행 경계를 확인한다."""

import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from office import actions, drafts
from office.common import action_id
from orchestrator import draft
from test_orchestrator_draft import DraftCase, sample


class OfficeDraftTest(DraftCase):
	def fake_draft(self, argv, **options):
		self.assertEqual(argv[:4], [sys.executable, "-m", "orchestrator", "draft"])
		self.assertIs(options["shell"], False)
		self.assertEqual(options["timeout"], 600)
		self.assertEqual(options["cwd"], actions.REPO)
		self.assertEqual(options["env"]["AI_HUB_HOME"], str(self.hub))
		self.assertEqual(options["env"]["PYTHONPATH"], str(actions.REPO))
		text_path = Path(argv[argv.index("--text-file") + 1])
		self.assertEqual(stat.S_IMODE(text_path.stat().st_mode), 0o600)
		self.assertTrue(text_path.read_text())
		self.assertNotIn(self.text.read_text(), " ".join(argv))
		out = Path(argv[argv.index("--out") + 1])
		role = argv[argv.index("--role") + 1]
		data = draft.validate(sample(), role, draft.Rules())
		fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
		with os.fdopen(fd, "w") as stream:
			stream.write(draft.serialize(data))
		return subprocess.CompletedProcess(argv, 0, json.dumps(draft.summary(data, "p01")), "")

	def generate(self, role="backend", text="합성 사용자 원문은 기록에 남기지 않습니다"):
		with mock.patch.object(drafts.subprocess, "run", side_effect=self.fake_draft) as command:
			status, value = actions.apply({"action": "dev_draft", "id": role, "text": text})
		self.assertEqual(status, 200, value)
		command.assert_called_once()
		return value["draft"]

	def meta(self, identity):
		return drafts.metadata(self.hub, identity)

	def test_draft_redraft_discard_permissions_history_pending_and_private_audit(self):
		value = self.generate(role="frontend")
		identity = value["id"]
		self.assertEqual(action_id("dev_run", identity), identity)
		self.assertEqual(self.meta(identity)["status"], "초안")
		self.assertEqual(self.meta(identity)["role"], "frontend")
		self.assertEqual(drafts.read_drafts(self.hub)["items"], [{"id": identity, "role": "frontend", "goal": value["goal"],
			"done": len(value["done"]), "owned_paths": value["owned_paths"], "created": value["created"]}])
		additional = "완료 조건을 한 개 더 적어 주세요"
		with mock.patch.object(drafts.subprocess, "run", side_effect=self.fake_draft) as command:
			status, redraft = actions.apply({"action": "dev_redraft", "id": identity, "text": additional})
		self.assertEqual(status, 200, redraft)
		argv = command.call_args.args[0]
		self.assertEqual(argv[-2:], ["--previous", str(self.hub / "office-drafts" / (identity + ".toml"))])
		second = redraft["draft"]["id"]
		self.assertEqual(self.meta(second)["previous"], identity)
		self.assertEqual(self.meta(second)["text"], additional)
		self.assertEqual(self.meta(identity)["status"], "버림")
		self.assertEqual([row["id"] for row in drafts.read_drafts(self.hub)["items"]], [second])
		before = (self.hub / "office-drafts" / (second + ".toml")).read_bytes()
		status, discarded = actions.apply({"action": "dev_discard", "id": second})
		self.assertEqual(status, 200, discarded)
		self.assertEqual(self.meta(second)["status"], "버림")
		self.assertEqual((self.hub / "office-drafts" / (second + ".toml")).read_bytes(), before)
		self.assertEqual(drafts.read_drafts(self.hub)["items"], [])
		for path in (self.hub / "office-drafts").iterdir():
			self.assertEqual(path.suffix in (".json", ".toml"), True)
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE((self.hub / "office-drafts").stat().st_mode), 0o700)
		audit = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assertEqual([json.loads(line)["action"] for line in audit.splitlines()], ["dev_draft", "dev_redraft", "dev_discard"])
		for text in ("합성 사용자 원문", additional, value["goal"], "owned_paths", "forbidden"):
			self.assertNotIn(text, audit)

	def test_run_detaches_without_shell_or_apply_logs_privately_and_cannot_repeat(self):
		identity = self.generate()["id"]
		def launch(argv, **options):
			self.assertEqual(argv, [sys.executable, "-m", "orchestrator", "run", str(self.hub / "office-drafts" / (identity + ".toml"))])
			self.assertIs(options["shell"], False)
			self.assertIs(options["start_new_session"], True)
			self.assertEqual(options["cwd"], actions.REPO)
			self.assertEqual(options["stdin"], subprocess.DEVNULL)
			self.assertEqual(options["stderr"], subprocess.STDOUT)
			options["stdout"].write("합성 실행 기록\n")
			return mock.Mock(pid=42)
		with mock.patch.object(drafts.subprocess, "Popen", side_effect=launch) as popen:
			status, value = actions.apply({"action": "dev_run", "id": identity})
			self.assertEqual(status, 200, value)
			self.assertEqual(self.meta(identity)["status"], "실행 시작")
			self.assertEqual(drafts.read_drafts(self.hub)["items"], [])
			for action in ("dev_run", "dev_redraft", "dev_discard"):
				payload = {"action": action, "id": identity}
				if action == "dev_redraft":
					payload["text"] = "추가 합성 지시"
				self.assertEqual(actions.apply(payload)[0], 409)
			popen.assert_called_once()
		log = self.hub / "logs/office-runs" / (identity + ".log")
		self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
		self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
		self.assertIn("합성 실행 기록", log.read_text())

	def test_exact_payload_role_id_text_validation_never_reaches_subprocess(self):
		bad = [{"action": "dev_draft", "id": role, "text": "합성"} for role in ("qa", "xyz", "../backend", [], None)]
		bad += [{"action": "dev_draft", "id": "plan", "text": text} for text in ("", " ", "a" * 2001, "a\x00b", [], 3, None)]
		bad += [{"action": "dev_draft", "id": "plan"}, {"action": "dev_draft", "id": "plan", "text": "합성", "extra": "xyz"}]
		for action in ("dev_redraft", "dev_run", "dev_discard"):
			for identity in ("../xyz", "d-20261324-100000-12345678", "d-20260230-100000-12345678",
					"d-20261024-246060-12345678", "d-20261024-100000-xyz", "d-20261024-100000-12345678/"):
				bad.append({"action": action, "id": identity, **({"text": "합성"} if action == "dev_redraft" else {})})
		bad.append({"action": "dev_run", "id": "d-20261024-100000-12345678", "text": "합성"})
		with mock.patch.object(drafts.subprocess, "run") as run, mock.patch.object(drafts.subprocess, "Popen") as popen:
			for payload in bad:
				with self.subTest(payload=repr(payload)[:80]):
					self.assertEqual(actions.apply(payload)[0], 400)
			run.assert_not_called()
			popen.assert_not_called()
		self.assertFalse((self.hub / "logs/office-actions.jsonl").exists())

	def test_recheck_rejects_tampered_request_metadata_budget_and_other_repositories(self):
		identity = self.generate()["id"]
		path = self.hub / "office-drafts" / (identity + ".toml")
		original = draft.read_draft(path)
		for change in ({"repo": str(self.root / "xyz")}, {"test_command": "xyz"}, {"role": "design"},
				{"owned_paths": ["../xyz"]}, {"commit_message": "Generated-By: p01"}, {"forbidden": []}):
			data = {"request": {**original["request"], **change}, "budget": original["budget"]}
			path.write_text(draft.serialize(data))
			with self.subTest(change=change), mock.patch.object(drafts.subprocess, "Popen") as popen:
				self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
				popen.assert_not_called()
		path.write_text(draft.serialize({**original, "budget": {"calls": 99, "minutes": 30, "fixes": 1}}))
		with mock.patch.object(drafts.subprocess, "Popen") as popen:
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			popen.assert_not_called()
		path.write_text(draft.serialize(original))
		row = self.meta(identity)
		row["summary"]["goal"] = "다른 합성 목표"
		drafts.save(self.hub, row)
		with mock.patch.object(drafts.subprocess, "run") as run:
			self.assertEqual(actions.apply({"action": "dev_redraft", "id": identity, "text": "추가 합성"})[0], 409)
			run.assert_not_called()

	def test_failure_and_timeout_preserve_previous_and_never_echo_subprocess_output(self):
		identity = self.generate()["id"]
		for failure, expected in ((subprocess.CompletedProcess([], 2, "사용자 글 원문", "사용자 글 원문"), 500),
				(subprocess.TimeoutExpired([], 600, output="사용자 글 원문"), 504), (OSError("사용자 글 원문"), 409)):
			with mock.patch.object(drafts.subprocess, "run") as command:
				if isinstance(failure, Exception):
					command.side_effect = failure
				else:
					command.return_value = failure
				status, value = actions.apply({"action": "dev_redraft", "id": identity, "text": "추가 합성 지시"})
			self.assertEqual(status, expected, value)
			self.assertNotIn("사용자 글 원문", json.dumps(value, ensure_ascii=False))
			self.assertEqual(self.meta(identity)["status"], "초안")
		self.assertNotIn("사용자 글 원문", (self.hub / "logs/office-actions.jsonl").read_text())

	def test_failure_shows_only_the_last_draft_reason_line_after_masking(self):
		token = "ghp_" + "a" * 36
		cases = (("사용자 글 원문\n" + draft.FAILURE + "owned_paths: 항목 수는 1~20개여야 합니다\n", "이유: owned_paths: 항목 수는 1~20개여야 합니다"),
			(draft.FAILURE + "acme 저장소 경로 오류", "사용자 글·요금제 설정을 확인하세요."),
			(draft.FAILURE + "토큰 " + token, None),
			(draft.FAILURE + "앞 줄\n사용자 글 원문", "사용자 글·요금제 설정을 확인하세요."))
		for stderr, expected in cases:
			with mock.patch.object(drafts.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", stderr)):
				status, value = actions.apply({"action": "dev_draft", "id": "backend", "text": "합성 개발 지시"})
			self.assertEqual(status, 500, value)
			if expected:
				self.assertTrue(value["message"].endswith(expected), value)
			for raw in ("사용자 글 원문", "acme", token):
				self.assertNotIn(raw, json.dumps(value, ensure_ascii=False))
		audit = (self.hub / "logs/office-actions.jsonl").read_text()
		self.assertIn("owned_paths: 항목 수는", audit)
		self.assertNotIn("acme", audit)
		self.assertNotIn(token, audit)

	def test_links_hardlinks_and_open_permissions_prevent_run(self):
		identity = self.generate()["id"]
		path = self.hub / "office-drafts" / (identity + ".toml")
		original = path.read_bytes()
		outside = self.root / "outside.toml"
		outside.write_bytes(original)
		outside.chmod(0o600)
		with mock.patch.object(drafts.subprocess, "Popen") as popen:
			path.chmod(0o644)
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			path.unlink()
			path.symlink_to(outside)
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			path.unlink()
			os.link(outside, path)
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			popen.assert_not_called()
		self.assertEqual(outside.read_bytes(), original)

	def test_generation_holds_the_existing_action_lock(self):
		started, release = threading.Event(), threading.Event()
		def wait(argv, **options):
			started.set()
			self.assertTrue(release.wait(5))
			return self.fake_draft(argv, **options)
		with mock.patch.object(drafts.subprocess, "run", side_effect=wait) as command, ThreadPoolExecutor() as pool:
			first = pool.submit(actions.apply, {"action": "dev_draft", "id": "backend", "text": "합성 지시"})
			try:
				self.assertTrue(started.wait(5))
				self.assertEqual(actions.apply({"action": "dev_draft", "id": "plan", "text": "다음 지시"})[0], 409)
			finally:
				release.set()
			self.assertEqual(first.result(timeout=5)[0], 200)
			command.assert_called_once()

	def test_failed_process_start_restores_pending_status_and_allows_retry(self):
		identity = self.generate()["id"]
		with mock.patch.object(drafts.subprocess, "Popen", side_effect=OSError("합성 실패")):
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
		self.assertEqual(self.meta(identity)["status"], "초안")
		self.assertFalse((self.hub / "logs/office-runs" / (identity + ".log")).exists())
		with mock.patch.object(drafts.subprocess, "Popen") as launch:
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 200)
			launch.assert_called_once()

	def test_linked_draft_folder_and_run_log_never_start_process_or_change_target(self):
		folder = self.hub / "office-drafts"
		folder.symlink_to(self.root, target_is_directory=True)
		with mock.patch.object(drafts.subprocess, "run") as command:
			self.assertEqual(actions.apply({"action": "dev_draft", "id": "backend", "text": "합성 지시"})[0], 409)
			command.assert_not_called()
		folder.unlink()
		identity = self.generate()["id"]
		logs = drafts.directory(self.hub, "logs/office-runs")
		target = self.root / "target.log"
		target.write_text("보존할 합성 파일")
		(logs / (identity + ".log")).symlink_to(target)
		with mock.patch.object(drafts.subprocess, "Popen") as launch:
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			launch.assert_not_called()
		self.assertEqual(target.read_text(), "보존할 합성 파일")
