"""시험 단계가 시간 초과나 정상 종료 뒤에 하위 프로세스(브라우저 등)를 남기지 않는지 합성 프로세스로 확인한다."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import checks

PARENT = (
	"import subprocess, sys, time\n"
	"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
	"open(sys.argv[1], 'w').write(str(child.pid))\n"
	"{tail}"
)


def alive(pid):
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	return True


class RunTestsCleanupTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)

	def run_script(self, tail):
		script, record = self.root / "parent.py", self.root / "child.pid"
		script.write_text(PARENT.format(tail=tail), encoding="utf-8")
		result = checks.run_tests(f"{sys.executable} {script} {record}", self.root)
		child = int(record.read_text())
		self.addCleanup(lambda: alive(child) and os.kill(child, 9))
		return result, child

	def wait_dead(self, pid):
		for _ in range(50):
			if not alive(pid):
				return True
			time.sleep(0.1)
		return False

	def test_timeout_ends_the_whole_group_not_only_the_test_process(self):
		with mock.patch.object(checks, "TEST_SECONDS", 2), mock.patch.object(checks, "KILL_GRACE_SECONDS", 2):
			result, child = self.run_script("time.sleep(120)\n")
		self.assertEqual((result["ok"], result["detail"]), (False, "시간 초과(2초)"))
		self.assertTrue(self.wait_dead(child))

	def test_children_left_behind_after_a_normal_exit_are_ended_too(self):
		result, child = self.run_script("print('시험 통과')\n")
		self.assertEqual((result["ok"], result["detail"]), (True, "시험 통과"))
		self.assertTrue(self.wait_dead(child))

	def test_failed_run_keeps_the_output_tail_and_a_term_ignoring_child_is_killed(self):
		tail = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('실패 출력')\nsys.exit(3)\n"
		result, child = self.run_script(tail)
		self.assertEqual((result["ok"], result["detail"]), (False, "실패 출력"))
		self.assertTrue(self.wait_dead(child))


if __name__ == "__main__":
	unittest.main()
