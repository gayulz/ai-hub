"""접수 질문과 답변을 주고받는 CLI."""

import argparse
import sys

from collector.config import ConfigError

from . import intake


def main(argv) -> int:
	parser = argparse.ArgumentParser(prog="python3 -m agency", description="접수 되묻기")
	commands = parser.add_subparsers(dest="command", required=True)
	start = commands.add_parser("intake", help="명세 초안과 질문 생성")
	start.add_argument("id", help="프로젝트 식별자")
	reply = commands.add_parser("answer", help="열린 질문에 답변")
	reply.add_argument("id", help="프로젝트 식별자")
	reply.add_argument("no", type=int, help="질문 번호")
	reply.add_argument("text", help="답변, -이면 표준입력")
	try:
		args = parser.parse_args(argv)
	except SystemExit as exc:
		return exc.code
	try:
		if args.command == "intake":
			result = intake.step(args.id)
			for question in result["questions"]:
				print(f"[{question['no']}] {question['question']}")
			if not result["missing"] and not result["questions"]:
				print("명세 확정 대기 없음")
		else:
			text = sys.stdin.read() if args.text == "-" else args.text
			if intake.answer(args.id, args.no, text)["status"] == "명세 확정":
				print("명세 확정")
	except (ValueError, ConfigError, RuntimeError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	except OSError:
		print("오류: 로컬 파일을 읽거나 저장할 수 없습니다", file=sys.stderr)
		return 1
	return 0
