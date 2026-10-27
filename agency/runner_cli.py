"""프로젝트 순차 실행 명령."""

import sys

from collector.config import ConfigError

from .runner import run


def main(argv: list[str]) -> int:
	if len(argv) != 2 or argv[0] != "run" or argv[1].startswith("-"):
		print("사용법: python3 -m agency run <id>", file=sys.stderr)
		return 2
	try:
		result = run(argv[1])
	except (ValueError, ConfigError, RuntimeError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	print("상태: " + result["status"])
	print("완료 작업: " + (", ".join(result["done"]) or "없음"))
	if result["blocked_reason"]:
		print("막힘 이유: " + result["blocked_reason"])
	return 3 if result["status"] == "막힘" else 0
