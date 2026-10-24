"""읽기 전용 회의 명령. 응답은 상태 전체 JSON이며 참석자와 답변 요약을 명시 확인한다.

meeting analyze --target ai-hub --text-file agenda.txt
meeting status <id>
meeting start <id> --participants backend qa
meeting answer <id> --card <card-id> --answers-file answers.json
meeting resume <id> [--confirm-answers <card-id>]

답변 파일: [{"question_id":"scope","kind":"choice","value":"a"}]
직접 답변은 kind=text, 명시 위임은 kind=delegate와 구체적인 value 또는 알아서를 쓴다.
실제 설정은 만들지 않는다. orchestration.meeting 표의 역할 값은 기존 개인
요금제 ID 목록이다. 화면·텔레그램·개발 초안·숏폼 접수 연결은 후속 단계다.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from collector.backfill_common import safe
from collector.config import ConfigError

from .meeting import Engine


def read_input(path, maximum):
	if ".." in path.parts or not safe(path):
		raise ValueError("입력 파일은 링크·비밀 경로일 수 없습니다")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "r", encoding="utf-8") as stream:
		info = os.fstat(stream.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum * 4:
			raise ValueError("입력 파일의 종류·크기를 확인하세요")
		value = stream.read(maximum + 1)
		if len(value) > maximum:
			raise ValueError("입력 파일이 너무 큽니다")
		return value


def main(argv):
	parser = argparse.ArgumentParser(prog="python3 -m orchestrator meeting", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = parser.add_subparsers(dest="command", required=True)
	analyze = sub.add_parser("analyze")
	analyze.add_argument("--target", required=True)
	analyze.add_argument("--text-file", type=Path, required=True)
	for command in ("status", "start", "answer", "resume"):
		item = sub.add_parser(command)
		item.add_argument("id")
		if command == "start":
			item.add_argument("--participants", nargs="+", required=True)
		elif command == "answer":
			item.add_argument("--card", required=True)
			item.add_argument("--answers-file", type=Path, required=True)
		elif command == "resume":
			item.add_argument("--confirm-answers")
	args = parser.parse_args(argv)
	try:
		engine = Engine()
		if args.command == "analyze":
			state = engine.analyze(read_input(args.text_file, 2000), args.target)
		elif args.command == "start":
			state = engine.start(args.id, args.participants)
		elif args.command == "answer":
			state = engine.answer(args.id, args.card, json.loads(read_input(args.answers_file, 10000)), source="cli")
		elif args.command == "resume":
			state = engine.resume(args.id, confirm_card=args.confirm_answers)
		else:
			state = engine.status(args.id)
		print(json.dumps(state, ensure_ascii=False, allow_nan=False))
		return 1 if state["status"] in ("failed", "budget_exhausted", "questions_exhausted", "record_pending") else 0
	except (OSError, ValueError, RuntimeError, TypeError, KeyError, ConfigError, RecursionError) as exc:
		# 경로·설정·공급자의 예외 원문은 터미널에 노출하지 않는다.
		print("회의 처리 실패: " + type(exc).__name__, file=sys.stderr)
		return 2
