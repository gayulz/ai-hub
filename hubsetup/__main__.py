"""사용법: python3 -m hubsetup install [--apply] | check"""

from __future__ import annotations

import argparse
import sys

from .check import check
from .config import SetupError, context
from .install import install


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(prog="hubsetup", description=__doc__)
	sub = parser.add_subparsers(dest="command", required=True)
	setup = sub.add_parser("install", help="변경 비교 (실제 적용은 --apply)")
	setup.add_argument("--apply", action="store_true", help="파일·권한·작성자·예약 작업 적용")
	setup.add_argument("--author-name", help="볼트 로컬 Git 작성자 이름")
	setup.add_argument("--author-email", help="볼트 로컬 Git 작성자 이메일")
	inspect = sub.add_parser("check", help="읽기 전용 점검표, 실패가 있으면 종료 코드 1")
	for command in (setup, inspect):
		command.add_argument("--label-prefix", help="launchd 라벨 접두어 (기본: com.로그인사용자)")
	args = parser.parse_args(argv)
	try:
		if args.command == "check":
			return check(args.label_prefix)
		return install(context(args.label_prefix), args.apply, args.author_name, args.author_email)
	except Exception as exc:
		message = str(exc) if isinstance(exc, SetupError) else "설정 파일·설치 경로·파일 권한을 확인하세요. (값 생략)"
		print(f"설치·점검 실패: {message}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
