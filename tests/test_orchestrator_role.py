"""요청 역할의 저장·이전 실행 호환·잘못된 역할의 조기 거부."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import __main__ as cli
from orchestrator.record import Run


class OrchestratorRoleTest(unittest.TestCase):
	def test_explicit_and_default_roles_round_trip_without_running_an_ai(self):
		with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"AI_HUB_HOME": folder}):
			for role in (None, "plan", "backend", "frontend", "design"):
				request = {"goal": "합성 작업"}
				if role:
					request["role"] = role
				run = Run("2026-10-24-001", request, "/w/xyz")
				run.save()
				self.assertEqual(json.loads((run.folder / "run.json").read_text())["role"], role or "backend")
				self.assertEqual(Run.load(run.run_id).role, role or "backend")
			data = json.loads((run.folder / "run.json").read_text())
			data.pop("role")
			data["request"].pop("role", None)
			(run.folder / "run.json").write_text(json.dumps(data))
			self.assertEqual(Run.load(run.run_id).role, "backend")

	def test_request_cli_retains_role_before_execute(self):
		with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"AI_HUB_HOME": folder}):
			path = Path(folder) / "request.toml"
			path.write_text('[request]\ngoal="합성"\ndone=["합성"]\nowned_paths=["src/*"]\nrole="frontend"\n')
			with mock.patch.object(cli, "execute", return_value=0) as execute:
				self.assertEqual(cli.cmd_run(str(path)), 0)
			self.assertEqual(execute.call_args.args[0].role, "frontend")
			path.write_text(path.read_text().replace('role="frontend"', 'role="unknown"'))
			with mock.patch.object(cli, "new_run_id") as allocate, self.assertRaises(ValueError):
				cli.cmd_run(str(path))
			allocate.assert_not_called()
