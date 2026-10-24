"""볼트 R1: report로 미리보고, 승인 후 apply --folder 이름으로 반영한다."""

from __future__ import annotations

import argparse
import sys

from collector.config import ConfigError

from .apply import apply
from .report import report


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	commands = parser.add_subparsers(dest="command", required=True)
	commands.add_parser("report", help="볼트를 바꾸지 않고 계획과 승인 보고서 생성")
	command = commands.add_parser("apply", help="승인한 맨 위 폴더만 반영하고 로컬 커밋")
	command.add_argument("--folder", required=True, help="이모지를 뗀 폴더 이름, 루트는 root")
	args = parser.parse_args(argv)
	try:
		if args.command == "report":
			report()
			return 0
		return apply(args.folder)
	except ConfigError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	except (OSError, ValueError):
		print("R1 실행 실패: 파일 접근 권한과 설정 형식을 확인하세요", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
