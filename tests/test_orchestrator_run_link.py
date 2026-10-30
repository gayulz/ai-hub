"""실행 번호 연결이 준비·AI 호출보다 앞서는지 검사한다."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator import __main__ as orchestration
from orchestrator.flow import Flow
from orchestrator.providers import ProviderError
from orchestrator.record import Run, new_run_id


class StartLinkTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		root = Path(temporary.name).resolve()
		self.enterContext(mock.patch("orchestrator.targets.REPO", root))
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(root / "hub")})
		patch.start()
		self.addCleanup(patch.stop)
		self.path = root / "request.toml"
		request = {"repo": str(root), "goal": "합성 연결", "done": ["합성 검사"],
			"owned_paths": ["sample.py"]}
		self.path.write_text("[request]\n" + "\n".join(key + " = " + json.dumps(value, ensure_ascii=False)
			for key, value in request.items()), encoding="utf-8")

	def test_failed_link_cannot_prepare_or_call_an_agent(self):
		created = []
		def link(run):
			created.append(run.run_id)
			self.assertEqual((run.step, run.worktree, run.calls), ("prepare", "", []))
			with self.assertRaises(RuntimeError), orchestration.locked(run):
				self.fail("실행 잠금이 유지되어야 합니다")
			raise ValueError("합성 연결 거부")
		with mock.patch.object(orchestration, "_execute_locked") as execute, \
				self.assertRaisesRegex(ValueError, "합성 연결 거부"):
			orchestration.start_run(str(self.path), on_created=link)
		execute.assert_not_called()
		self.assertEqual(len(created), 1)
		run = Run.load(created[0])
		self.assertEqual((run.status, run.worktree, run.calls), ("실패", "", []))

	def test_successful_link_precedes_execution_under_same_lock(self):
		order = []
		def link(run):
			order.append("연결")
			self.assertEqual((run.step, run.worktree, run.calls), ("prepare", "", []))
		def execute(run):
			order.append("실행")
			with self.assertRaises(RuntimeError), orchestration.locked(run):
				self.fail("실행 잠금이 유지되어야 합니다")
			run.finish("완료", "합성 완료")
			return 0
		with mock.patch.object(orchestration, "_execute_locked", side_effect=execute):
			run = orchestration.start_run(str(self.path), on_created=link)
		self.assertEqual(order, ["연결", "실행"])
		self.assertEqual(run._exit_code, 0)

	def test_failed_provider_records_elapsed_seconds(self):
		request = orchestration.read_request(str(self.path))["request"]
		run = Run(new_run_id(), request, request["repo"])
		run.save()
		plan = SimpleNamespace(id="fake", tool="codex")
		def fail(*args):
			raise ProviderError("timeout", "합성 시간 초과")
		flow = Flow(run, mock.Mock(), {}, caller=fail)
		with mock.patch("orchestrator.flow.role_plans", return_value=[plan]):
			self.assertIsNone(flow.ask("implement", "합성", {}, True))
		self.assertFalse(run.calls[0]["ok"])
		self.assertGreaterEqual(run.calls[0]["seconds"], 0)
