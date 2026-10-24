"""Codex·Claude 의 새 대화·이어 가기 인자를 완전히 비교하고 AGY 는 거부하는지 본다. 실제 AI는 부르지 않는다."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.config import Plan
from orchestrator import providers


class ChatProviderTest(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		self.git_repo, self.plain = self.root / "repo", self.root / "plain"
		self.git_repo.mkdir()
		self.plain.mkdir()
		subprocess.run(["git", "init", "-q", str(self.git_repo)], check=True, capture_output=True)
		self.schema = {"type": "object", "properties": {"reply": {"type": "string"}}}
		self.data = {"reply": "합성 응답"}
		self.usage = {"input_tokens": 2, "output_tokens": 3}

	def plan(self, tool, model=None):
		return Plan("p01", tool, "합성", "personal", ("personal",), model, None)

	def run_call(self, tool, session=None, model=None, git=True, *, session_present=True):
		calls = []
		cwd = self.git_repo if git else self.plain
		def fake(argv, **kwargs):
			calls.append((argv[:], kwargs))
			if argv[0] == "git":
				return subprocess.CompletedProcess(argv, 0 if git else 1)
			if tool == "codex":
				schema_file = Path(argv[argv.index("--output-schema") + 1])
				self.assertEqual(json.loads(schema_file.read_text()), self.schema)
				Path(argv[argv.index("-o") + 1]).write_text(json.dumps(self.data))
				stdout = "읽을 수 없는 사건 한 줄\n" + json.dumps({"type": "thread.started", **({"thread_id": "thread-01"} if session_present else {})})
				stdout += "\n" + json.dumps({"type": "turn.completed", "usage": self.usage})
			else:
				stdout = json.dumps({"structured_output": self.data, "usage": self.usage,
					**({"session_id": "thread-01"} if session_present else {})})
			return subprocess.CompletedProcess(argv, 0, stdout, "출력하지 않을 원문")
		with mock.patch.object(providers.subprocess, "run", side_effect=fake):
			reply = providers.chat_call(self.plan(tool, model), "합성 질문", cwd, self.schema, 600, session)
		return reply, calls, cwd

	def test_exact_argv_for_all_tools_new_resume_models_and_git_modes(self):
		for tool in ("codex", "claude"):
			for session in (None, "thread-00"):
				for model in (None, "model-01"):
					for git in ((True, False) if tool == "codex" else (True,)):
						with self.subTest(tool=tool, session=session, model=model, git=git):
							reply, calls, cwd = self.run_call(tool, session, model, git)
							argv, kwargs = calls[-1]
							if tool == "codex":
								schema_file, last_file = argv[argv.index("--output-schema") + 1], argv[argv.index("-o") + 1]
								self.assertEqual(Path(schema_file).parent, Path(last_file).parent)
								expected = (["codex", "exec", "-C", str(cwd), "-s", "read-only", "--json"] if session is None
									else ["codex", "exec", "resume", session, "--json", "-c", 'sandbox_mode="read-only"'])
								expected += ["--output-schema", schema_file, "-o", last_file]
								if model:
									expected += ["-m", model]
								if not git:
									expected += ["--skip-git-repo-check"]
								expected += ["-"]
								self.assertEqual(calls[0], (["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"], {"capture_output": True}))
							elif tool == "claude":
								expected = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(self.schema),
									"--allowedTools", "Read,Grep,Glob", "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,Bash,WebFetch,WebSearch", "--strict-mcp-config"]
								if model:
									expected += ["--model", model]
								if session:
									expected += ["--resume", session]
							self.assertEqual(argv, expected)
							self.assertEqual(kwargs, {"input": "합성 질문", "cwd": cwd,
								"capture_output": True, "text": True, "timeout": 600})
							for flag in ("--ephemeral", "--no-session-persistence", "workspace-write", "acceptEdits", "accept-edits", "danger", "yolo", "--danger", "--yolo"):
								self.assertNotIn(flag, argv)
							self.assertNotIn("shell", kwargs)
							self.assertEqual((reply.data, reply.usage, reply.session), (self.data, self.usage, "thread-01"))
							self.assertGreaterEqual(reply.seconds, 0)

	def test_no_session_id_is_none_even_after_resume(self):
		for tool in ("codex", "claude"):
			for session in (None, "thread-00"):
				with self.subTest(tool=tool, session=session):
					reply, _, _ = self.run_call(tool, session, session_present=False)
					self.assertIsNone(reply.session)

	def test_unwrap_reuses_fenced_and_nested_results_and_ignores_session_paths(self):
		outer = {"result": "```json\n" + json.dumps(self.data) + "\n```", "session_id": str(self.root / "session.json")}
		with mock.patch.object(providers.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(outer))):
			reply = providers.chat_call(self.plan("claude"), "질문", self.plain, self.schema, 600, None)
		self.assertEqual(reply.data, self.data)
		self.assertIsNone(reply.session)

	def test_failures_keep_provider_error_kinds_without_echoing_output(self):
		for result, kind in ((subprocess.TimeoutExpired("합성 원문", 600), "timeout"),
				(OSError("합성 원문"), "missing"), (subprocess.CompletedProcess([], 1, "합성 원문"), "exit"),
				(subprocess.CompletedProcess([], 0, "합성 원문"), "json"),
				(subprocess.CompletedProcess([], 0, '[]'), "json")):
			with self.subTest(kind=kind), mock.patch.object(providers.subprocess, "run") as run:
				if isinstance(result, BaseException):
					run.side_effect = result
				else:
					run.return_value = result
				with self.assertRaises(providers.ProviderError) as caught:
					providers.chat_call(self.plan("claude"), "질문", self.plain, self.schema, 600, None)
				self.assertEqual(caught.exception.kind, kind)
				self.assertNotIn("합성 원문", str(caught.exception))

	def test_codex_empty_last_message_or_bad_json(self):
		for text, kind in (("", "empty"), ("문자열 응답", "json")):
			def fake(argv, **kwargs):
				if argv[0] == "codex":
					Path(argv[argv.index("-o") + 1]).write_text(text)
				return subprocess.CompletedProcess(argv, 0, '{}')
			with mock.patch.object(providers.subprocess, "run", side_effect=fake), self.assertRaises(providers.ProviderError) as caught:
				providers.chat_call(self.plan("codex"), "질문", self.git_repo, self.schema, 600, None)
			self.assertEqual(caught.exception.kind, kind)

	def test_unsupported_tool_and_invalid_session_never_start_a_process(self):
		for tool, session in (("ollama", None), ("codex", "--yolo"), ("claude", "../session"), ("agy", None), ("agy", "conv-01")):
			with mock.patch.object(providers.subprocess, "run") as run, self.assertRaises(providers.ProviderError):
				providers.chat_call(self.plan(tool), "질문", self.plain, self.schema, 600, session)
			run.assert_not_called()
