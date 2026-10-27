"""볼트 R1·R3·R4: report로 미리보고, apply로 승인한 계획을 반영한다."""

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
	commands.add_parser("r3-report", help="개인 노트 본문 틀의 읽기 전용 계획과 보고서 생성")
	command = commands.add_parser("r3-apply", help="본문 틀을 10개씩 덧붙이고 로컬 커밋")
	command.add_argument("--limit", type=int, help="이번에 반영할 최대 노트 수")
	command = commands.add_parser("r4-report", help="개인 노트의 주제 중복 판정과 읽기 전용 보고서 생성")
	command.add_argument("--threshold", type=float, default=0.80, help="후보 쌍의 최소 코사인 유사도")
	command.add_argument("--max-groups", type=int, default=40, help="AI로 판정할 최대 묶음 수")
	command = commands.add_parser("r4-apply-links", help="관련 노트 링크만 10개씩 덧붙이고 로컬 커밋")
	command.add_argument("--limit", type=int, help="이번에 반영할 최대 노트 수")
	args = parser.parse_args(argv)
	try:
		if args.command == "r4-report":
			from .r4 import report as r4_report
			r4_report(args.threshold, args.max_groups)
			return 0
		if args.command == "r4-apply-links":
			from .r4_links import apply as r4_apply
			return r4_apply(args.limit)
		if args.command == "r3-report":
			from .r3 import report as r3_report
			r3_report()
			return 0
		if args.command == "r3-apply":
			from .r3_apply import apply as r3_apply
			return r3_apply(args.limit)
		if args.command == "report":
			report()
			return 0
		return apply(args.folder)
	except ConfigError as exc:
		print(str(exc), file=sys.stderr)
		return 2
	except (OSError, ValueError):
		stage = "R4" if args.command.startswith("r4-") else "R3" if args.command.startswith("r3-") else "R1"
		print(f"{stage} 실행 실패: 파일 접근 권한과 설정 형식을 확인하세요", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
