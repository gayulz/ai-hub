"""준비 명령과 개인 자료를 임시 홈의 합성 입력으로 검사한다."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agency import inputs, planner, private_provider, project, runner
from agency.departments import Department
from jobs import materials, storage
from orchestrator import providers


class InputsTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.department = Department("docs", "합성 부서", "합성 문서", (), ("test",), True,
			"sample", (), "", (), (), {"calls": 5, "minutes": 5}, deliverable="document")
		self.identity = project.new(self.department, "합성", "합성 요청")["id"]

	def department_with(self, *commands, **fields):
		return replace(self.department, prepare=tuple(tuple(command) for command in commands), **fields)

	def test_success_is_private_binary_bounded_and_never_repeated_on_retry(self):
		counter, flag = self.hub / "counter", self.hub / "flag"
		first = [sys.executable, "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
			"p.write_bytes((p.read_bytes() if p.exists() else b'')+b'x'); sys.stdout.buffer.write(b'first')", str(counter)]
		second = [sys.executable, "-c", "import pathlib,sys; sys.stderr.write('hidden'); "
			"sys.exit(1) if not pathlib.Path(sys.argv[1]).exists() else sys.stdout.write('second')", str(flag)]
		department = self.department_with(first, second)
		with self.assertRaisesRegex(inputs.InputError, "2번 실패: 종료 코드 실패"):
			inputs.collect(self.identity, department)
		self.assertEqual(counter.read_bytes(), b"x")
		flag.touch()
		paths = inputs.collect(self.identity, department)
		self.assertEqual(counter.read_bytes(), b"x")
		self.assertEqual([Path(path).read_bytes() for path in paths], [b"first", b"second"])
		self.assertTrue(all(stat.S_IMODE(Path(path).stat().st_mode) == 0o600 for path in paths))
		self.assertEqual(inputs.collect(self.identity, department), paths)
		self.assertEqual(counter.read_bytes(), b"x")
		inputs.verify(self.identity, department, {"inputs": paths})
		events = (project._folder(self.identity) / "events.jsonl").read_text(encoding="utf-8")
		self.assertNotIn("first", events)
		self.assertNotIn("hidden", events)

	def test_argument_array_has_no_shell_expansion(self):
		word = "literal; not a command"
		department = self.department_with([sys.executable, "-c", "import sys; print(sys.argv[1])", word])
		path = Path(inputs.collect(self.identity, department)[0])
		self.assertEqual(path.read_text(encoding="utf-8").strip(), word)

	def test_overflow_timeout_and_missing_program_fail_without_output_files(self):
		cases = [([sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x'*{inputs.MAX_OUTPUT + 1})"], "크기 초과"),
			([sys.executable, "-c", "import time; time.sleep(20)"], "시간 초과"),
			(["no-such-synthetic-program"], "시작할 수 없습니다")]
		for command, expected in cases:
			with self.subTest(expected=expected):
				identity = project.new(self.department, "합성", "합성 요청")["id"]
				department = self.department_with(command, prepare_timeout=1)
				started = time.monotonic()
				with self.assertRaisesRegex(inputs.InputError, expected):
					inputs.collect(identity, department)
				self.assertLess(time.monotonic() - started, 5)
				self.assertFalse((project._folder(identity) / "inputs/prepare-1.txt").exists())
				self.assertEqual(project.load(identity)["input_prepares"]["1"]["state"], "failed")

	def test_exact_limit_and_tampering_or_interrupted_command_block(self):
		command = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x'*{inputs.MAX_OUTPUT})"]
		department = self.department_with(command)
		path = Path(inputs.collect(self.identity, department)[0])
		self.assertEqual(path.stat().st_size, inputs.MAX_OUTPUT)
		path.write_bytes(b"changed")
		with self.assertRaisesRegex(inputs.InputError, "결과가 바뀌었습니다"):
			inputs.collect(self.identity, department)
		with project._edit(self.identity, "prepare") as row:
			row["input_prepares"]["1"]["state"] = "running"
		with self.assertRaisesRegex(inputs.InputError, "중단 상태"):
			inputs.collect(self.identity, department)

	def test_only_allowed_latest_jobs_are_exported_with_existing_lock_and_checks(self):
		with storage.transaction() as root:
			old, _ = materials.add(root, "합성 이전 자료")
			materials.permission(root, old["id"], True)
			latest, _ = materials.add(root, "합성 최신 자료", previous=old["id"])
			materials.permission(root, latest["id"], True)
			restricted, _ = materials.add(root, "합성 비허용 자료")
		department = replace(self.department, inputs=("jobs-allowed",))
		paths = inputs.collect(self.identity, department)
		self.assertEqual([Path(path).name for path in paths], ["materials-" + latest["id"] + ".md"])
		self.assertEqual(Path(paths[0]).read_text(encoding="utf-8"), "합성 최신 자료")
		self.assertEqual(stat.S_IMODE(Path(paths[0]).stat().st_mode), 0o600)
		folder = project._folder(self.identity) / "inputs"
		self.assertFalse((folder / ("materials-" + old["id"] + ".md")).exists())
		self.assertFalse((folder / ("materials-" + restricted["id"] + ".md")).exists())
		with storage.transaction() as root:
			inputs.verify(self.identity, department, {"inputs": paths}, root)
			materials.permission(root, latest["id"], False)
		with storage.transaction() as root:
			with self.assertRaisesRegex(inputs.InputError, "허용 자료"):
				inputs.verify(self.identity, department, {"inputs": paths}, root)
		events = (project._folder(self.identity) / "events.jsonl").read_text(encoding="utf-8")
		self.assertNotIn("합성 최신 자료", events)

	def test_external_ai_forbidden_allows_prepare_without_exporting_or_calling_ai(self):
		command = [sys.executable, "-c", "print('local')"]
		department = self.department_with(command, external_ai=False, inputs=("jobs-allowed",))
		paths = inputs.collect(self.identity, department, include_materials=False)
		self.assertEqual(Path(paths[0]).read_text(encoding="utf-8").strip(), "local")
		self.assertFalse((self.hub / "jobs").exists())
		with self.assertRaisesRegex(inputs.InputError, "외부 AI 금지"):
			inputs.collect(self.identity, department)
		self.assertFalse((self.hub / "jobs").exists())
		with mock.patch.object(runner, "load_departments", return_value={"docs": department}), \
				mock.patch("agency.ai.ask", side_effect=AssertionError("AI 호출 금지")):
			with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
				runner.run(self.identity)
		identity = project.new(department, "합성", "합성 요청")["id"]
		project.update_spec(identity, goal="합성", target="합성", out_of_scope="합성", done_criteria=["합성"])
		project.confirm_spec(identity, department)
		with mock.patch.object(planner, "load_departments", return_value={"docs": department}), \
				mock.patch("agency.ai.ask", side_effect=AssertionError("AI 호출 금지")):
			with self.assertRaisesRegex(ValueError, "외부 AI 금지"):
				planner.plan(identity)
		self.assertEqual((project._folder(identity) / "inputs/prepare-1.txt").read_text().strip(), "local")
		self.assertFalse((self.hub / "jobs").exists())

	def test_jobs_permission_and_symlink_checks_are_reused(self):
		with storage.transaction() as root:
			item, _ = materials.add(root, "합성 자료")
			materials.permission(root, item["id"], True)
			path = root / "materials" / (item["id"] + ".json")
			path.chmod(0o644)
		department = replace(self.department, inputs=("jobs-allowed",))
		with self.assertRaisesRegex(inputs.InputError, "권한 검사"):
			inputs.collect(self.identity, department)
		path.chmod(0o600)
		input_dir = project._folder(self.identity) / "inputs"
		input_dir.rmdir()
		input_dir.symlink_to(self.hub)
		with self.assertRaisesRegex(inputs.InputError, "심볼릭 링크"):
			inputs.collect(self.identity, department)

	def test_private_provider_never_saves_failed_output(self):
		plan = mock.Mock(id="test", tool="claude", model=None)
		result = subprocess.CompletedProcess([], 1, stdout="합성 비공개 원문", stderr="합성 비공개 원문")
		with mock.patch.object(private_provider.subprocess, "run", return_value=result), \
				mock.patch.object(providers, "_log_unclassified", side_effect=AssertionError("로그 금지")):
			with self.assertRaises(providers.ProviderError) as caught:
				private_provider.call(plan, False, "합성 요청", self.hub, {}, 30)
		self.assertNotIn("합성 비공개 원문", str(caught.exception))
		self.assertFalse((self.hub / "logs").exists())
