"""대상 목록의 정책과 초안의 서버 확정·재검사 연결을 확인한다."""

import contextlib
import io
import json
from unittest import mock

from orchestrator import draft, targets
from orchestrator.providers import Reply
from test_orchestrator_draft import DraftCase, sample
from test_orchestrator_targets import init_repo, write_repos


class DraftTargetsTest(DraftCase):
	def setUp(self):
		super().setUp()
		self.repo = init_repo(self.root / "repo")
		self.other = init_repo(self.root / "other")
		patch = mock.patch.object(targets, "REPO", self.repo)
		patch.start()
		self.addCleanup(patch.stop)
		self.row = {"id": "blog-x", "path": str(self.other), "test_command": 'python3 -m unittest discover -s "qa tests"',
			"commit_pattern": r"feat\(clip\): .+", "forbidden": ["재기동은 별도 승인", "원천을 보존한다"]}
		write_repos(self.hub, self.row)

	def generate(self, value=None, identity="blog-x", previous=None):
		args = ["--role", "backend", "--target", identity, "--text-file", str(self.text), "--out", str(self.out)]
		if previous:
			args += ["--previous", str(previous)]
		value = value if value is not None else {**sample(), "commit_message": "feat(clip): 합성 개선"}
		with mock.patch.object(draft.providers, "call", return_value=Reply(value, .1, {})) as call, \
				contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
			code = draft.main(args)
		return code, output.getvalue(), errors.getvalue(), call

	def test_server_values_ignore_ai_fields_and_planner_uses_target_cwd(self):
		value = {**sample(), "commit_message": "feat(clip): 합성 개선", "repo": str(self.repo), "test_command": "xyz"}
		code, output, errors, call = self.generate(value)
		self.assertEqual((code, errors), (0, ""))
		data = draft.read_draft(self.out)
		self.assertEqual(data["request"]["repo"], str(self.other))
		self.assertEqual(data["request"]["test_command"], self.row["test_command"])
		self.assertEqual(data["request"]["forbidden"], value["forbidden"] + draft.DEFAULT_FORBIDDEN + self.row["forbidden"])
		self.assertEqual(json.loads(output)["target"], "blog-x")
		self.assertNotIn(str(self.other), output)
		call.assert_called_once()
		self.assertEqual(call.call_args.kwargs["cwd"], self.other)
		self.assertFalse(call.call_args.kwargs["writer"])
		prompt = call.call_args.kwargs["prompt"]
		for text in ("blog-x", "AGENTS.md", "RULES.md", "commit_message", "clip"):
			self.assertIn(text, prompt)
		self.assertNotIn(str(self.other), prompt)

	def test_missing_target_never_calls_ai_and_commit_requires_fullmatch(self):
		code, _, errors, call = self.generate(identity="missing")
		self.assertEqual(code, 2)
		self.assertIn("목록에 없는 대상 저장소입니다", errors)
		call.assert_not_called()
		for message in ("합성 개선", "앞 feat(clip): 합성 개선", "feat(clip): 합성 개선\n뒤"):
			with self.subTest(message=message):
				code, _, errors, call = self.generate({**sample(), "commit_message": message})
				self.assertEqual(code, 2)
				self.assertIn("커밋 꼴", errors)
				self.assertFalse(self.out.exists())

	def test_read_draft_rechecks_repo_command_policy_tail_and_commit(self):
		self.assertEqual(self.generate()[0], 0)
		original = draft.read_draft(self.out)
		for change in ({"repo": str(self.root / "unknown")}, {"test_command": "xyz"},
				{"forbidden": original["request"]["forbidden"][:-1]}, {"commit_message": "다른 꼴"}):
			with self.subTest(change=change):
				self.out.write_text(draft.serialize({**original, "request": {**original["request"], **change}}))
				with self.assertRaises(ValueError):
					draft.read_draft(self.out)
		self.out.write_text(draft.serialize(original))
		write_repos(self.hub, {**self.row, "forbidden": ["바뀐 금지 사항"]})
		with self.assertRaises(ValueError):
			draft.read_draft(self.out)

	def test_redraft_cannot_change_target(self):
		self.assertEqual(self.generate()[0], 0)
		previous = self.out
		self.out = self.root / "next.toml"
		code, _, _, call = self.generate(identity="ai-hub", previous=previous)
		self.assertEqual(code, 2)
		call.assert_not_called()
		self.assertEqual(self.generate(previous=previous)[0], 0)
