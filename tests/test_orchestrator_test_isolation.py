"""호스트 자료와 네트워크를 사용하지 않는 격리 정책 합성 시험."""
import platform
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from orchestrator import checks, test_isolation as isolation
RUNTIME_HASHES = {}


class IsolationTest(unittest.TestCase):
	def policy(self, files=None, **changes):
		content = b"assert True\n"
		values = dict(files=(isolation.SyntheticFile("tests/sample.py", content,
			isolation.digest(content), "synthetic-inline-v1"),) if files is None else files,
			runtime=(), python="/usr/bin/python3", protected=("/synthetic-protected",))
		values.update(changes)
		framework = Path(values["python"]).parent.parent
		stdlib = framework / "lib" / ("python" + framework.name)
		if values["runtime"] and stdlib.is_dir():
			if str(stdlib) not in RUNTIME_HASHES:
				RUNTIME_HASHES[str(stdlib)] = checks.isolation_runtime_tree(stdlib)
			values["stdlib_sha256"] = RUNTIME_HASHES[str(stdlib)]
		return isolation.Policy(**values)

	def test_unsupported_backend_never_starts_a_process(self):
		with mock.patch.object(isolation.platform, "system", return_value="Unsupported"), \
				mock.patch.object(isolation.subprocess, "Popen") as start:
			result = isolation.Backend().run(self.policy(), ("/usr/bin/python3", "sample.py"))
		start.assert_not_called()
		self.assertEqual((result["ok"], result["status"], result["executed"],
			result["unverified"]), (False, "unverified", False, 1))

	def test_missing_runtime_boundary_is_rejected(self):
		with self.assertRaises(isolation.IsolationError):
			isolation.validate(self.policy())

	def test_browser_and_connection_exceptions_are_rejected(self):
		for changes in ({"browser": True}, {"connections": ("127.0.0.1:12345",)}):
			with self.subTest(changes=changes), self.assertRaises(isolation.IsolationError):
				isolation.validate(self.policy(**changes))

	def test_profile_has_no_network_or_broad_execution_permission(self):
		boundary = {"reads": ["/synthetic-runtime/file"],
			"exec": ["/synthetic-runtime/python"], "python_root": "/synthetic-runtime",
			"site": "/synthetic-runtime/site-packages", "protected": ["/synthetic-protected"]}
		text = isolation.profile(boundary, Path("/synthetic-source"), Path("/synthetic-scratch"))
		self.assertIn("(deny default)", text)
		self.assertNotIn("allow network", text)
		self.assertIn('(allow process-exec (literal "/synthetic-runtime/python"))', text)
		self.assertIn('(deny file-read* file-write* (subpath "/synthetic-protected"))', text)

	def test_profile_quotes_rule_injection_as_data(self):
		boundary = {"reads": [' /synthetic/\") (allow default)'], "exec": [], "protected": [],
			"python_root": "/synthetic-runtime", "site": "/synthetic-runtime/site-packages"}
		text = isolation.profile(boundary, Path("/synthetic-source"), Path("/synthetic-scratch"))
		self.assertIn('\\"', text)
		self.assertIn("합성", isolation._quote("/합성"))

	def test_invalid_names_hashes_and_provenance_are_rejected_before_runtime_reads(self):
		for name, sha, source in (("../escape", isolation.digest(b"x"), "inline"),
				(".git/objects/item", isolation.digest(b"x"), "inline"),
				("sample.py", "0" * 64, "inline"), ("sample.py", isolation.digest(b"x"), "")):
			item = isolation.SyntheticFile(name, b"x", sha, source)
			policy = self.policy(files=(item,), runtime=(isolation.RuntimeFile("/unused", ""),))
			with self.subTest(name=name), mock.patch.object(isolation, "_real_file") as read:
				with self.assertRaises(isolation.IsolationError):
					isolation.validate(policy)
				read.assert_not_called()

	def test_relative_runtime_is_rejected_without_reading_contents(self):
		with self.assertRaises(isolation.IsolationError):
			isolation._real_file("relative-runtime")

	def test_process_output_is_bounded_before_a_run_can_fill_a_file(self):
		with tempfile.TemporaryDirectory() as temporary:
			command = [sys.executable, "-c", "import os; os.write(1, b'x' * 2097152)"]
			result, output = isolation._execute(command, temporary, {"HOME": temporary}, 10)
		self.assertEqual(result["status"], "output_limit")
		self.assertLessEqual(len(output), isolation.MAX_OUTPUT + 1)

	def test_guard_distinguishes_denial_from_missing_or_timed_out(self):
		namespace = {}
		exec(isolation.GUARD.split("secret,link,source", 1)[0], namespace)
		operation = namespace["operation"]
		for error, expected in ((PermissionError(errno.EPERM, "synthetic"), "denied"),
				(FileNotFoundError(errno.ENOENT, "synthetic"), "unverified"),
				(subprocess.TimeoutExpired("synthetic", 1), "unverified")):
			def fail():
				raise error
			self.assertEqual(operation(fail), expected)
		self.assertEqual(operation(lambda: None), "allowed")

	def test_completed_process_group_is_not_signalled_after_parent_exit(self):
		with tempfile.TemporaryDirectory() as folder, mock.patch.object(os, "killpg") as signal_group:
			result, _ = isolation._execute([sys.executable, "-c", "print('synthetic')"], folder, {}, 10)
		self.assertEqual(result["status"], "passed")
		signal_group.assert_not_called()

	def test_interrupted_process_is_reaped_before_the_lock_can_be_released(self):
		original = subprocess.Popen
		for error in (KeyboardInterrupt(), TypeError("synthetic")):
			created = []
			def start(*args, **kwargs):
				process = original(*args, **kwargs)
				created.append(process)
				return process
			with tempfile.TemporaryDirectory() as folder, \
					mock.patch.object(isolation.subprocess, "Popen", side_effect=start), \
					mock.patch.object(isolation.selectors.DefaultSelector, "select", side_effect=error), \
					self.assertRaises(type(error)):
				isolation._execute([sys.executable, "-c", "import time;time.sleep(30)"], folder, {}, 10)
			self.assertEqual(len(created), 1)
			self.assertIsNotNone(created[0].poll())

	def test_runtime_copy_never_follows_a_link_to_synthetic_protected_files(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			source, copy, protected = root / "source", root / "copy", root / "protected"
			for folder in (source, copy, protected):
				folder.mkdir()
			(source / "allowed.py").write_text("synthetic", encoding="utf-8")
			(protected / "sentinel").write_text("synthetic-private", encoding="utf-8")
			(source / "link").symlink_to(protected / "sentinel")
			(source / "directory-link").symlink_to(protected, target_is_directory=True)
			sha = checks.isolation_runtime_tree(source, copy)
			self.assertEqual(sha, checks.isolation_runtime_tree(copy))
			self.assertEqual(sorted(path.name for path in copy.iterdir()), ["allowed.py"])
			os.link(protected / "sentinel", source / "hardlink")
			with self.assertRaises(ValueError):
				checks.isolation_runtime_tree(source)

	def test_runtime_hash_never_follows_a_replaced_file_link(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			source, protected = root / "runtime", root / "protected"
			source.write_bytes(b"synthetic-runtime")
			protected.write_bytes(b"synthetic-private")
			self.assertEqual(checks.isolation_runtime_file_digest(source), isolation.digest(b"synthetic-runtime"))
			source.rename(root / "preserved-runtime")
			source.symlink_to(protected)
			with self.assertRaises((OSError, ValueError)):
				checks.isolation_runtime_file_digest(source)
			self.assertEqual(protected.read_bytes(), b"synthetic-private")

	def test_failed_attempt_needs_explicit_review_and_is_preserved_on_retry(self):
		with tempfile.TemporaryDirectory() as temporary:
			backend = isolation.Backend(Path(temporary).resolve() / "evidence")
			identity = "a" * 64
			first = backend._reserve(identity)
			with self.assertRaises(isolation.IsolationError):
				backend._reserve(identity)
			active = {"name": first.name, "sha256": isolation.digest(first.read_bytes())}
			with self.assertRaises(isolation.IsolationError):
				backend._reserve(identity, active)
			backend._save(first, {"identity": identity, "status": "failed"})
			previous = first.read_bytes()
			reviewed = {"name": first.name, "sha256": isolation.digest(previous)}
			second = backend._reserve(identity, reviewed)
			self.assertNotEqual(first, second)
			self.assertEqual(first.read_bytes(), previous)
			with self.assertRaises(isolation.IsolationError):
				backend._reserve(identity, reviewed)

	def test_interrupted_attempt_requires_dead_owner_and_exact_reviewed_bytes(self):
		with tempfile.TemporaryDirectory() as temporary:
			backend = isolation.Backend(Path(temporary).resolve() / "evidence")
			identity = "b" * 64
			first = backend._reserve(identity)
			backend._save(first, {"identity": identity, "status": "reserved", "pid": 2147483647})
			reviewed = {"name": first.name, "sha256": isolation.digest(first.read_bytes())}
			with self.assertRaises(isolation.IsolationError):
				backend._reserve(identity, {**reviewed, "sha256": "0" * 64})
			second = backend._reserve(identity, reviewed)
			self.assertTrue(first.exists() and second.exists())

	def test_evidence_creation_does_not_follow_a_raced_parent_link(self):
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			protected, parent = root / "protected", root / "entry"
			protected.mkdir(mode=0o700)
			folder = parent / "evidence"
			original = Path.mkdir
			def raced(path, *args, **kwargs):
				if path == folder and not parent.exists():
					parent.symlink_to(protected, target_is_directory=True)
				return original(path, *args, **kwargs)
			with mock.patch.object(Path, "mkdir", raced):
				try:
					isolation.Backend(folder)._reserve("a" * 64)
				except (OSError, ValueError):
					pass
			self.assertFalse((protected / "evidence").exists())

	def test_evidence_save_rejects_replaced_directory_and_file(self):
		for replacement in ("directory", "file"):
			with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
				root = Path(temporary).resolve()
				backend = isolation.Backend(root / "evidence")
				path = backend._reserve("b" * 64)
				if replacement == "directory":
					path.parent.rename(root / "preserved-directory")
					protected = root / "protected"
					protected.mkdir(mode=0o700)
					path.parent.symlink_to(protected, target_is_directory=True)
				else:
					path.rename(root / "preserved-file")
				path.write_bytes(b"synthetic-protected")
				path.chmod(0o600)
				with self.assertRaises((OSError, ValueError)):
					backend._save(path, {"status": "failed"})
				self.assertEqual(path.read_bytes(), b"synthetic-protected")

	def test_isolation_attempts_for_one_evidence_folder_cannot_overlap(self):
		with tempfile.TemporaryDirectory() as temporary:
			folder = Path(temporary).resolve()
			first = checks.isolation_execution_lock(folder)
			try:
				with self.assertRaises(isolation.IsolationError):
					checks.isolation_execution_lock(folder)
			finally:
				os.close(first)
			second = checks.isolation_execution_lock(folder)
			os.close(second)

	def test_lock_guard_releases_normally_and_keeps_lock_after_parent_eof(self):
		with tempfile.TemporaryDirectory() as temporary:
			folder = Path(temporary).resolve()
			for release in (True, False):
				lock = checks.isolation_execution_lock(folder)
				guardian, writer = checks.isolation_lock_guard(lock)
				os.close(lock)
				try:
					with self.assertRaises(isolation.IsolationError):
						checks.isolation_execution_lock(folder)
					status = checks.finish_isolation_guard(guardian, writer, release=release)
					self.assertEqual(status, "released" if release else "held")
					if not release:
						with self.assertRaises(isolation.IsolationError):
							checks.isolation_execution_lock(folder)
				finally:
					if guardian.poll() is None:
						guardian.kill()
						guardian.wait(timeout=5)
				again = checks.isolation_execution_lock(folder)
				os.close(again)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_unconfirmed_termination_keeps_the_global_lock(self):
		with tempfile.TemporaryDirectory() as temporary:
			backend = isolation.Backend(Path(temporary).resolve() / "evidence")
			original, guardians = checks.isolation_lock_guard, []
			def start(lock):
				process, writer = original(lock)
				guardians.append(process)
				return process, writer
			try:
				with mock.patch.object(isolation, "validate", return_value={"protected": []}), \
						mock.patch.object(checks, "isolation_lock_guard", side_effect=start), \
						mock.patch.object(checks, "copied_isolation_runtime", side_effect=isolation.TerminationUnverified("합성 종료 미확인")):
					result = backend.run(self.policy(), ("/usr/bin/python3", "-m", "unittest"))
				self.assertEqual(result["lock_guard"]["status"], "held")
				self.assertFalse(result["ok"])
				with self.assertRaises(isolation.IsolationError):
					checks.global_isolation_lock()
			finally:
				for process in guardians:
					if process.poll() is None:
						process.kill()
						process.wait(timeout=5)
			lock = checks.global_isolation_lock()
			os.close(lock)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_different_evidence_folders_share_the_global_execution_lock(self):
		with tempfile.TemporaryDirectory() as temporary:
			guard = checks.global_isolation_lock()
			try:
				for name in ("first", "second"):
					backend = isolation.Backend(Path(temporary).resolve() / name)
					with mock.patch.object(isolation, "validate", return_value={}), \
							mock.patch.object(backend, "_guard") as run_guard:
						result = backend.run(self.policy(), ("/usr/bin/python3", "-m", "unittest"))
					self.assertFalse(result["executed"])
					self.assertIn("다른 격리 시험", result["reason"])
					run_guard.assert_not_called()
					self.assertFalse((Path(temporary).resolve() / name).exists())
			finally:
				os.close(guard)
			lock = checks.global_isolation_lock()
			os.close(lock)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_runtime_copy_failure_never_starts_the_guard_or_test(self):
		launcher = Path(sys.executable).resolve()
		engine = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(runtime=runtime, python=str(launcher), protected=(str(root / "protected"),))
			backend = isolation.Backend(root / "evidence")
			with mock.patch.object(checks, "copied_isolation_runtime", side_effect=subprocess.CalledProcessError(1, "synthetic")), \
					mock.patch.object(backend, "_guard") as guard, mock.patch.object(isolation, "_execute") as execute:
				result = backend.run(policy, (str(launcher), "-m", "unittest", "discover"))
			self.assertEqual((result["ok"], result["status"], result["executed"]), (False, "unverified", False))
			guard.assert_not_called()
			execute.assert_not_called()

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_test_runs_after_guard_and_persistent_record_prevents_repetition(self):
		launcher = Path(sys.executable).resolve()
		framework = launcher.parent.parent
		engine = framework / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			protected = root / "protected"
			protected.mkdir()
			(protected / "sentinel").write_text("synthetic", encoding="utf-8")
			content = b"import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n"
			files = (isolation.SyntheticFile("tests/test_sample.py", content,
				isolation.digest(content), "synthetic-inline-v1"),)
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(files=files, runtime=runtime, python=str(launcher), protected=(str(protected),))
			command = (str(launcher), "-m", "unittest", "discover", "-s", "tests")
			backend = isolation.Backend(root / "evidence")
			counts = {"ran": 1, "failures": 0, "errors": 0, "skipped": 0}
			output = b"AIHUB_TEST_RESULT=" + json.dumps(counts).encode()
			with mock.patch.object(backend, "_guard", return_value=({"read": "denied", "source_read": "allowed"}, {"status": "passed"})) as guard, \
					mock.patch.object(backend, "_no_fork_guard", return_value=({"fork": "denied", "child": "denied"}, {})), \
					mock.patch.object(isolation, "_execute", return_value=({"status": "passed", "execution": {}}, output)) as execute:
				result = backend.run(policy, command, seconds=30)
			guard.assert_called_once()
			execute.assert_called_once()
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["status"], "passed")
			self.assertTrue(result["executed"])
			self.assertEqual(result["tests"], {"ran": 1, "failures": 0, "errors": 0, "skipped": 0})
			self.assertEqual(result["guard"], {"read": "denied", "source_read": "allowed"})
			self.assertEqual((protected / "sentinel").read_text(encoding="utf-8"), "synthetic")
			record = json.loads((root / "evidence" / (result["identity"] + ".json")).read_text(encoding="utf-8"))
			self.assertEqual(record, result)
			repeated = isolation.Backend(root / "evidence").run(policy, command, seconds=30)
			self.assertFalse(repeated["executed"])
			self.assertFalse(repeated["ok"])
			self.assertIn("반복하지 않습니다", repeated["reason"])
			for label, counts in (("empty", {"ran": 0, "failures": 0, "errors": 0, "skipped": 0}),
					("failed", {"ran": 1, "failures": 1, "errors": 0, "skipped": 0}),
					("error", {"ran": 1, "failures": 0, "errors": 1, "skipped": 0})):
				with self.subTest(label=label):
					candidate = isolation.Backend(root / ("evidence-" + label))
					output = b"AIHUB_TEST_RESULT=" + json.dumps(counts).encode()
					with mock.patch.object(candidate, "_guard", return_value=({}, {})), \
							mock.patch.object(candidate, "_no_fork_guard", return_value=({}, {})), \
							mock.patch.object(isolation, "_execute", return_value=({"status": "passed", "execution": {}}, output)):
						negative = candidate.run(policy, command, seconds=30)
					self.assertTrue(negative["executed"], negative)
					self.assertFalse(negative["ok"], negative)
					self.assertEqual(negative["status"], "failed")

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_skipped_synthetic_test_is_shown_with_pass(self):
		launcher = Path(sys.executable).resolve()
		engine = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			content = (b"import unittest\nclass Sample(unittest.TestCase):\n"
				b" @unittest.skip('synthetic')\n def test_skip(self): pass\n")
			files = (isolation.SyntheticFile("tests/test_sample.py", content,
				isolation.digest(content), "synthetic-inline-v1"),)
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(files=files, runtime=runtime, python=str(launcher), protected=(str(root / "protected"),))
			command = (str(launcher), "-m", "unittest", "discover", "-s", "tests")
			backend = isolation.Backend(root / "evidence")
			counts = {"ran": 1, "failures": 0, "errors": 0, "skipped": 1}
			output = b"AIHUB_TEST_RESULT=" + json.dumps(counts).encode()
			with mock.patch.object(backend, "_guard", return_value=({"synthetic": "denied"}, {"status": "passed"})), \
					mock.patch.object(backend, "_no_fork_guard", return_value=({"fork": "denied", "child": "denied"}, {})), \
					mock.patch.object(isolation, "_execute", return_value=({"status": "passed", "execution": {}}, output)):
				result = backend.run(policy, command, seconds=30)
			self.assertTrue(result["executed"], result)
			self.assertTrue(result["ok"])
			self.assertEqual(result["tests"]["skipped"], 1)
			self.assertEqual(result["unverified"], 1)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_failed_guard_never_runs_test_and_reserves_the_attempt(self):
		launcher = Path(sys.executable).resolve()
		engine = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(runtime=runtime, python=str(launcher), protected=(str(root / "protected"),))
			command = (str(launcher), "-m", "unittest", "discover", "-s", "tests")
			backend = isolation.Backend(root / "evidence")
			with mock.patch.object(backend, "_guard", side_effect=isolation.IsolationError("합성 차단 실패")), \
					mock.patch.object(isolation, "_execute") as execute:
				result = backend.run(policy, command)
			execute.assert_not_called()
			self.assertFalse(result["executed"])
			self.assertEqual(result["reason"], "합성 차단 실패")
			self.assertFalse(isolation.Backend(root / "evidence").run(policy, command)["executed"])
			first = root / "evidence" / (result["identity"] + ".json")
			previous = first.read_bytes()
			reviewed = {"name": first.name, "sha256": isolation.digest(previous)}
			counts = {"ran": 1, "failures": 0, "errors": 0, "skipped": 0}
			output = b"AIHUB_TEST_RESULT=" + json.dumps(counts).encode()
			with mock.patch.object(backend, "_guard", return_value=({"synthetic": "denied"}, {"status": "passed"})), \
					mock.patch.object(backend, "_no_fork_guard", return_value=({"fork": "denied", "child": "denied"}, {})), \
					mock.patch.object(isolation, "_execute", return_value=({"status": "passed", "execution": {}}, output)):
				retry = backend.run(policy, command, reviewed_attempt=reviewed)
			self.assertTrue(retry["executed"])
			self.assertTrue(retry["ok"])
			self.assertEqual(retry["reviewed_attempt"], reviewed)
			self.assertEqual(first.read_bytes(), previous)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	@unittest.skipIf(os.environ.get("AIHUB_SKIP_NESTED_SANDBOX") == "1", "운영체제 격리 중첩은 별도 합성 시험으로 확인")
	def test_real_sandbox_records_test_summary(self):
		launcher = Path(sys.executable).resolve()
		engine = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			content = (b"import os,unittest\nclass Sample(unittest.TestCase):\n def test_forge(self):\n"
				b"  print('AIHUB_TEST_RESULT={\"ran\":1,\"failures\":0,\"errors\":0,\"skipped\":0}',flush=True)\n"
				b"  os._exit(0)\n")
			files = (isolation.SyntheticFile("tests/test_sample.py", content,
				isolation.digest(content), "synthetic-inline-v1"),)
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(files=files, runtime=runtime, python=str(launcher), protected=(str(root / "protected"),))
			command = (str(launcher), "-m", "unittest", "discover", "-s", "tests")
			result = isolation.Backend(root / "evidence").run(policy, command, seconds=30)
			self.assertTrue(result["executed"], result)
			self.assertTrue(result["ok"], result)
			self.assertEqual(result["unverified"], 0)
			self.assertEqual(result["no_fork_guard"], {"fork": "denied", "child": "denied",
				"posix_spawn": "denied", "posix_spawnp": "denied"})
			self.assertEqual(result["guard"]["original_runtime"], "denied")
			self.assertEqual(result["guard"]["source_read"], "allowed")
			self.assertEqual(result["guard"]["scratch_write"], "allowed")
			self.assertEqual(result["guard"]["hardlink"], "denied")
			self.assertEqual(result["guard_process"]["controls"], "synthetic-operations-allowed")

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	@unittest.skipIf(os.environ.get("AIHUB_SKIP_NESTED_SANDBOX") == "1", "운영체제 격리 중첩은 별도 합성 시험으로 확인")
	def test_real_sandbox_honors_unicode_source_scratch_and_protected_paths(self):
		launcher = Path(sys.executable).resolve()
		engine = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			work, scratch, protected = root / "합성 원본", root / "합성 임시", root / "합성 보호"
			for folder in (work, scratch, protected):
				folder.mkdir()
			(work / "자료").write_text("synthetic", encoding="utf-8")
			(protected / "자료").write_text("synthetic", encoding="utf-8")
			runtime = tuple(isolation.RuntimeFile(str(path), isolation.digest(path.read_bytes()), index < 2)
				for index, path in enumerate((launcher, engine, library, launcher.parent.parent / "Python")))
			policy = self.policy(runtime=runtime, python=str(launcher), protected=(str(protected),))
			text = isolation.profile(isolation.validate(policy), work, scratch)
			code = ("import json,sys\n"
				"def read(p):\n try:\n  open(p,'rb').read();return 'allowed'\n except OSError:return 'denied'\n"
				"def write(p):\n try:\n  open(p,'wb').write(b'x');return 'allowed'\n except OSError:return 'denied'\n"
				"print(json.dumps([read(sys.argv[1]),write(sys.argv[2]),read(sys.argv[3])]))")
			env = {"HOME": str(scratch), "TMPDIR": str(scratch), "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
			result = subprocess.run(["/usr/bin/sandbox-exec", "-p", text, str(launcher), "-I", "-S", "-c", code,
				str(work / "자료"), str(scratch / "쓰기"), str(protected / "자료")], cwd=work,
				env=env, capture_output=True, text=True, timeout=15)
			self.assertEqual(result.returncode, 0)
			self.assertEqual(json.loads(result.stdout), ["allowed", "allowed", "denied"])


if __name__ == "__main__":
	unittest.main()
