"""프로젝트 가지의 독립 검수 명령."""

import sys

from collector.config import ConfigError

from .acceptance import review


def main(argv: list[str]) -> int:
	if len(argv) != 2 or argv[0] != "review" or argv[1].startswith("-"):
		print("사용법: python3 -m agency review <id>", file=sys.stderr)
		return 2
	try:
		result = review(argv[1])
	except (ValueError, ConfigError, RuntimeError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	except OSError:
		print("오류: 로컬 파일을 읽거나 저장할 수 없습니다", file=sys.stderr)
		return 1
	print("검수: " + ("통과" if result["passed"] else "불합격"))
	print("상태: " + result["status"])
	print("보고서: " + result["report"])
	if result["status"] == "납품 대기":
		print(f"납품 승인: python3 -m agency deliver {argv[1]}")
	return 3 if result["status"] == "막힘" else 0
