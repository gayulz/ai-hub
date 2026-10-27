"""명세 확정 프로젝트의 PM 계획 명령."""

import argparse
import sys

from collector.config import ConfigError

from .planner import plan


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(prog="python3 -m agency plan", description=__doc__)
	parser.add_argument("id", help="프로젝트 식별자")
	if not argv or argv[0] != "plan":
		parser.print_usage(sys.stderr)
		return 2
	try:
		args = parser.parse_args(argv[1:])
	except SystemExit as exc:
		return exc.code
	try:
		result = plan(args.id)
		for task in result["tasks"]:
			title = task["title"].replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
			dependencies = ", ".join(task["depends_on"]) or "없음"
			print(f"- {task['id']} {title} (의존: {dependencies})")
	except (ValueError, ConfigError, RuntimeError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	except OSError:
		print("오류: 로컬 파일을 읽거나 저장할 수 없습니다", file=sys.stderr)
		return 1
	return 0
