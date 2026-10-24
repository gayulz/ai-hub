"""사무실 초안의 대상 선택·재작성·실행 경계를 합성 설정으로 확인한다."""

from unittest import mock

from collector.config import ConfigError
from office import actions, drafts
from orchestrator import targets
from test_orchestrator_draft import DraftCase
from test_orchestrator_targets import init_repo, write_repos
import test_office_drafts as existing


class OfficeDraftTargetsTest(DraftCase):
	fake_draft = existing.OfficeDraftTest.fake_draft

	def setUp(self):
		super().setUp()
		self.repo = init_repo(self.root / "repo")
		self.other = init_repo(self.root / "other")
		patch = mock.patch.object(targets, "REPO", self.repo)
		patch.start()
		self.addCleanup(patch.stop)
		write_repos(self.hub, {"id": "blog-x", "path": str(self.other), "test_command": "python3 -m unittest",
			"forbidden": ["합성 대상의 금지 사항"]})
		self.listing = targets.targets()
		patch = mock.patch.object(targets, "targets", return_value=self.listing)
		patch.start()
		self.addCleanup(patch.stop)

	def test_draft_exact_target_argv_metadata_and_redraft_keeps_previous_target(self):
		with mock.patch.object(drafts.subprocess, "run", side_effect=self.fake_draft) as command:
			status, value = actions.apply({"action": "dev_draft", "id": "frontend", "text": "합성 개발 지시", "target": "blog-x"})
		self.assertEqual(status, 200, value)
		self.assertEqual(command.call_args.args[0][4:8], ["--role", "frontend", "--target", "blog-x"])
		identity = value["draft"]["id"]
		row, data = drafts.load(self.hub, identity)
		self.assertEqual(row["target"], "blog-x")
		self.assertEqual(data["request"]["repo"], str(self.other))
		self.assertEqual(drafts.read_drafts(self.hub)["items"][0]["target"], "blog-x")
		with mock.patch.object(drafts.subprocess, "run", side_effect=self.fake_draft) as command:
			status, value = actions.apply({"action": "dev_redraft", "id": identity, "text": "추가 합성 지시"})
		self.assertEqual(status, 200, value)
		self.assertEqual(command.call_args.args[0][4:8], ["--role", "frontend", "--target", "blog-x"])
		self.assertEqual(value["draft"]["target"], "blog-x")
		identity = value["draft"]["id"]
		with mock.patch.object(drafts.subprocess, "run") as command, mock.patch.object(drafts.subprocess, "Popen") as launch:
			self.assertEqual(actions.apply({"action": "dev_redraft", "id": identity, "text": "추가", "target": "ai-hub"})[0], 400)
			command.assert_not_called()
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 200)
			launch.assert_called_once()
			self.assertNotIn("apply", launch.call_args.args[0])

	def test_unknown_target_broken_list_and_target_metadata_tampering_do_not_execute(self):
		with mock.patch.object(drafts.subprocess, "run") as command:
			self.assertEqual(actions.apply({"action": "dev_draft", "id": "backend", "text": "합성", "target": "missing"})[0], 400)
			with mock.patch.object(targets, "targets", side_effect=ConfigError("repos.toml:1: 형식 오류")):
				self.assertEqual(actions.apply({"action": "dev_draft", "id": "backend", "text": "합성", "target": "blog-x"})[0], 400)
			command.assert_not_called()
		with mock.patch.object(drafts.subprocess, "run", side_effect=self.fake_draft):
			status, value = actions.apply({"action": "dev_draft", "id": "backend", "text": "합성", "target": "blog-x"})
		self.assertEqual(status, 200)
		identity = value["draft"]["id"]
		row, data = drafts.load(self.hub, identity)
		with mock.patch.object(targets, "targets", side_effect=ConfigError("repos.toml:1: 형식 오류")):
			with self.assertRaisesRegex(ValueError, "대상 저장소 목록"):
				drafts.load(self.hub, identity)
			with self.assertRaisesRegex(ValueError, "대상 저장소 목록"):
				drafts.card(row, data)
		row = drafts.metadata(self.hub, identity)
		row["target"] = "ai-hub"
		drafts.save(self.hub, row)
		with mock.patch.object(drafts.subprocess, "Popen") as launch:
			self.assertEqual(actions.apply({"action": "dev_run", "id": identity})[0], 409)
			launch.assert_not_called()
