"""시간대 자료 허용과 기존 합성 격리 경계의 회귀 시험."""
import errno
import platform
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import checks, test_isolation as isolation


class TimezoneIsolationTest(unittest.TestCase):
	def test_profile_limits_timezone_access_and_keeps_denials(self):
		boundary = {"reads": [], "exec": [], "python_root": "/synthetic-runtime",
			"site": "/synthetic-runtime/site-packages", "protected": ["/synthetic-protected"]}
		text = isolation.profile(boundary, Path("/synthetic-source"), Path("/synthetic-scratch"))
		for path in ("/usr/share/zoneinfo", "/private/var/db/timezone", "/var/db/timezone"):
			self.assertIn(f'(allow file-read* (subpath "{path}"))', text)
			self.assertIn(f'(path-ancestors "{path}")', text)
		self.assertNotIn('(allow file-read* (subpath "/private/var/db"))', text)
		self.assertNotIn("allow network", text)
		self.assertIn('(deny file-read* file-write* (subpath "/synthetic-protected"))', text)

	@unittest.skipUnless(platform.system() == "Darwin", "macOS 격리 합성 시험")
	def test_timezone_and_protected_data_with_fork_denied(self):
		launcher = Path(sys.executable).resolve()
		framework = launcher.parent.parent
		engine = framework / "Resources/Python.app/Contents/MacOS/Python"
		library = Path("/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib").resolve()
		if not engine.is_file() or not library.is_file():
			self.skipTest("검증할 수 있는 Python 런타임이 없습니다")
		with tempfile.TemporaryDirectory() as temporary:
			root = Path(temporary).resolve()
			protected = (root / "user", root / "target")
			for folder in protected:
				folder.mkdir()
				(folder / "sentinel").write_bytes(b"synthetic")
			content = ("import errno, unittest\nfrom datetime import datetime, timedelta\n"
				"from zoneinfo import ZoneInfo\nclass Sample(unittest.TestCase):\n"
				" def test_timezone_and_protected(self):\n"
				"  self.assertEqual(datetime(2030, 1, 2, tzinfo=ZoneInfo('Asia/Seoul')).utcoffset(), timedelta(hours=9))\n"
				f"  for path in {tuple(str(folder / 'sentinel') for folder in protected)!r}:\n"
				"   with self.assertRaises(PermissionError) as error:\n"
				"    open(path, 'rb').read()\n"
				f"   self.assertIn(error.exception.errno, ({errno.EPERM}, {errno.EACCES}))\n").encode()
			files = (isolation.SyntheticFile("tests/test_sample.py", content,
				isolation.digest(content), "synthetic-inline-v1"),)
			runtime = tuple(isolation.RuntimeFile(str(path), checks.isolation_runtime_file_digest(path), index < 2)
				for index, path in enumerate((launcher, engine, library, framework / "Python")))
			stdlib = framework / "lib" / ("python" + framework.name)
			policy = isolation.Policy(files, runtime, str(launcher), tuple(map(str, protected)),
				stdlib_sha256=checks.isolation_runtime_tree(stdlib))
			backend = isolation.Backend(root / "evidence")
			guard = backend._guard
			guard_errors = []
			def observe_guard(*args):
				try:
					return guard(*args)
				except OSError as error:
					frame = traceback.extract_tb(error.__traceback__)[-1]
					guard_errors.append((type(error).__name__, error.errno, frame.name, frame.lineno))
					raise
			with mock.patch.object(backend, "_guard", side_effect=observe_guard):
				result = backend.run(policy,
					(str(launcher), "-m", "unittest", "discover", "-s", "tests"), seconds=60)
			self.assertTrue(result["ok"], (result, guard_errors))
			self.assertEqual(result["tests"], {"ran": 1, "failures": 0, "errors": 0, "skipped": 0})
			self.assertEqual(result["no_fork_guard"], {"fork": "denied", "child": "denied",
				"posix_spawn": "denied", "posix_spawnp": "denied"})
			for key in ("read", "read_link", "read_volume_alias", "tcp", "unix", "binary", "script"):
				self.assertEqual(result["guard"][key], "denied", key)
			for folder in protected:
				self.assertEqual((folder / "sentinel").read_bytes(), b"synthetic")


if __name__ == "__main__":
	unittest.main()
