"""기획 AI는 대역으로 두고 초안의 검증·직렬화·실행기 호환을 확인한다."""

import contextlib
import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from collector.config import Plan, Plans
from orchestrator import __main__ as cli, draft
from orchestrator.flow import role_plans
from orchestrator.providers import ProviderError, Reply


def sample():
	return {"goal": "개발 지시 추가", "done": ["합성 요청으로 초안의 형식 검사를 통과한다"],
		"owned_paths": ["office/*.py", "tests/test_office_*.py"], "forbidden": ["원천을 수정하지 않는다"],
		"details": "기존 규칙을 따른다", "commit_message": "개발 지시 초안 추가",
		"budget": {"calls": 4, "minutes": 30, "fixes": 1}}


class DraftCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.hub = self.root / "hub"
		(self.hub / "redaction").mkdir(parents=True)
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "company01"},
			{"value": "xyz", "kind": "서비스명", "token": "service01"}]}), encoding="utf-8")
		(self.hub / "plans.toml").write_text('''[[plans]]
id="p01"
tool="codex"
owner="personal"
allow=["personal"]
[[plans]]
id="p02"
tool="claude"
owner="personal"
allow=["personal"]
[orchestration]
implementer=["p01"]
reviewer=["p02"]
''', encoding="utf-8")
		self.text = self.root / "text.txt"
		self.text.write_text("합성 개발 지시입니다", encoding="utf-8")
		self.out = self.root / "draft.toml"
		for patch in (mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01")):
			patch.start()
			self.addCleanup(patch.stop)


class OrchestratorDraftTest(DraftCase):
	def generate(self, data=None, role="backend", previous=None):
		arguments = ["draft", "--role", role, "--text-file", str(self.text), "--out", str(self.out)]
		if previous:
			arguments += ["--previous", str(previous)]
		with mock.patch.object(draft.providers, "call", return_value=Reply(data if data is not None else sample(), .1, {})) as call, \
				contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
			code = cli.main(arguments)
		return code, output.getvalue(), errors.getvalue(), call

	def test_read_only_single_call_valid_toml_private_mode_and_cmd_run_rules(self):
		code, output, errors, call = self.generate(role="frontend")
		self.assertEqual((code, errors), (0, ""))
		self.assertEqual(len(output.splitlines()), 1)
		value = json.loads(output)
		data = draft.read_draft(self.out)
		self.assertEqual(data["request"]["repo"], str(draft.REPO))
		self.assertEqual(data["request"]["kind"], "code")
		self.assertEqual(data["request"]["test_command"], draft.DEFAULT_TEST)
		self.assertEqual(value["role"], "frontend")
		self.assertEqual(value["plan"], "p01")
		self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
		call.assert_called_once()
		self.assertFalse(call.call_args.kwargs["writer"])
		self.assertEqual(call.call_args.kwargs["cwd"], draft.REPO)
		self.assertIn("AGENTS.md", call.call_args.kwargs["prompt"])
		self.assertIn(self.text.read_text(), call.call_args.kwargs["prompt"])
		self.assertEqual(set(call.call_args.kwargs["schema"]["properties"]), set(sample()))
		with mock.patch.object(cli, "execute", return_value=0) as execute:
			self.assertEqual(cli.cmd_run(str(self.out)), 0)
		self.assertEqual(execute.call_args.args[0].role, "frontend")

	def test_budget_clamps_server_fields_ignore_ai_values_and_defaults_always_follow(self):
		for budget, expected in (({"calls": -1, "minutes": 1, "fixes": -1}, {"calls": 2, "minutes": 10, "fixes": 0}),
				({"calls": 99, "minutes": 999, "fixes": 99}, {"calls": 8, "minutes": 120, "fixes": 2})):
			data = {**sample(), "budget": budget, "repo": "/tmp/xyz", "kind": "analysis", "role": "design", "test_command": "xyz"}
			checked = draft.validate(data, "plan", draft.Rules())
			self.assertEqual(checked["budget"], expected)
			self.assertEqual(checked["request"]["role"], "plan")
			self.assertEqual(checked["request"]["forbidden"], data["forbidden"] + draft.DEFAULT_FORBIDDEN)
		data = draft.validate({**sample(), "forbidden": []}, "design", draft.Rules())
		self.assertEqual(data["request"]["forbidden"], draft.DEFAULT_FORBIDDEN)

	def test_bad_ai_fields_fail_with_reason_and_never_write(self):
		cases = [{"owned_paths": [path]} for path in ("", "*", "**", "**/*", "/tmp/a.py", "../a.py", "src/../a.py",
			"src/..a", ".git/config", "src/.git/config", "~/a.py", "a~.py", "C:/a.py", "C:\\a.py", "src\na.py", ".")]
		cases += [{"goal": "a" * 301}, {"goal": " "}, {"done": []}, {"done": ["a"] * 11}, {"done": ["a" * 501]},
			{"owned_paths": []}, {"owned_paths": ["src/a.py"] * 21}, {"commit_message": "a" * 201},
			{"commit_message": "Generated-By: p01"}, {"commit_message": "acme 수정"}, {"goal": "xyz 추가"},
			{"details": "acme 자료"}, {"done": ["a\x00b"]}, {"forbidden": [1]}, {"details": []},
			{"budget": {"calls": True, "minutes": 30, "fixes": 1}}, {"budget": {"calls": "4", "minutes": 30, "fixes": 1}},
			{"budget": {"calls": 4, "minutes": 30.5, "fixes": 1}}, {"budget": {}}]
		for change in cases:
			with self.subTest(change=repr(change)[:80]):
				code, output, errors, call = self.generate({**sample(), **change})
				self.assertEqual(code, 2)
				self.assertFalse(self.out.exists())
				self.assertEqual(output, "")
				self.assertTrue(errors.strip())
				call.assert_called_once()

	def test_company_service_secret_home_and_invalid_user_text_never_call_ai(self):
		for text in ("acme 개선", "xyz 추가", "password=" + "a" * 24, "/Users/p01/data", "", "a" * 2001, "a\x00b"):
			with self.subTest(text=text[:20]):
				self.text.write_text(text)
				code, _, _, call = self.generate()
				self.assertEqual(code, 2)
				call.assert_not_called()
				self.assertFalse(self.out.exists())

	def test_toml_quotes_controls_newlines_and_backslashes_round_trip(self):
		value = '따옴표 "한 줄"\n역슬래시 \\ 다음\t\b\f\r\x01\x7f'
		data = sample()
		for key in ("goal", "details", "commit_message"):
			data[key] = value
		data["done"] = [value]
		data["forbidden"] = [value]
		code, _, _, _ = self.generate(data)
		self.assertEqual(code, 0)
		with self.out.open("rb") as stream:
			read = tomllib.load(stream)
		for key in ("goal", "details", "commit_message"):
			self.assertEqual(read["request"][key], value)
		self.assertEqual(read["request"]["done"], [value])
		self.assertEqual(read["request"]["forbidden"], [value] + draft.DEFAULT_FORBIDDEN)

	def test_previous_draft_and_additional_instructions_are_checked_before_call(self):
		self.assertEqual(self.generate()[0], 0)
		previous = self.out
		self.out = self.root / "next.toml"
		self.text.write_text("완료 조건을 더 구체적으로 적어 주세요")
		code, _, _, call = self.generate(previous=previous)
		self.assertEqual(code, 0)
		prompt = call.call_args.kwargs["prompt"]
		self.assertIn("이전 초안", prompt)
		self.assertIn("추가 지시", prompt)
		self.assertIn(sample()["goal"], prompt)
		self.assertIn(self.text.read_text(), prompt)
		self.assertNotIn(str(draft.REPO), prompt)
		self.out.unlink()
		previous.write_text(previous.read_text().replace('role = "backend"', 'role = "design"'))
		code, _, _, call = self.generate(previous=previous)
		self.assertEqual(code, 2)
		call.assert_not_called()

	def test_planner_order_models_personal_filter_and_implementer_fallback(self):
		plan = Plan("p01", "codex", "", "personal", ("personal",), None, None)
		private = Plan("p02", "claude", "", "company", ("company",), None, None)
		plans = Plans({"p01": plan, "p02": private}, {})
		config = {"planner": ["p02", "missing", "p01"], "models": {"p01": "model01"}, "implementer": ["p02"]}
		selected = role_plans(plans, config, "draft")
		self.assertEqual([(p.id, p.model) for p in selected], [("p01", "model01")])
		self.assertEqual(role_plans(plans, {"implementer": ["p01"]}, "draft"), [plan])
		self.assertEqual(role_plans(plans, {"planner": [], "implementer": ["p01"]}, "draft"), [plan])
		path = self.hub / "plans.toml"
		path.write_text(path.read_text() + 'planner=["p02"]\n')
		self.assertEqual(self.generate()[3].call_args.args[0].id, "p02")

	def test_provider_failure_does_not_retry_or_write_and_existing_output_survives(self):
		with mock.patch.object(draft.providers, "call", side_effect=ProviderError("exit", "합성 실패")) as call, \
				contextlib.redirect_stderr(io.StringIO()) as errors:
			self.assertEqual(draft.main(["--role", "backend", "--text-file", str(self.text), "--out", str(self.out)]), 2)
		call.assert_called_once()
		self.assertEqual(errors.getvalue().strip(), draft.FAILURE + "AI 호출 실패(exit). 요금제 한도·로그인·네트워크를 확인하세요")
		self.assertFalse(self.out.exists())
		self.out.write_text("보존할 합성 파일")
		self.assertEqual(self.generate()[0], 2)
		self.assertEqual(self.out.read_text(), "보존할 합성 파일")
