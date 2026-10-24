"""상태 JSON 한 줄을 출력하는 읽기 전용 PM 대화 명령."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from collector.config import ConfigError

from . import chat_record as records
from .chat import ChatError, Engine

FAILURE = "대화 처리 실패: "


class Parser(argparse.ArgumentParser):
	def error(self, message):
		raise ChatError("명령과 필수 인자를 확인하세요")


def read_input(path, maximum):
	path = path.absolute()
	parent = records.directory(path.parent)
	try:
		fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
		with os.fdopen(fd, "r", encoding="utf-8") as stream:
			if records.regular(stream.fileno()).st_size > maximum * 4:
				raise ChatError("입력 파일이 너무 큽니다")
			value = stream.read(maximum + 1)
			if len(value) > maximum:
				raise ChatError("입력 파일이 너무 큽니다")
			return value
	finally:
		os.close(parent)


def parser():
	root = Parser(prog="python3 -m orchestrator chat", add_help=False)
	sub = root.add_subparsers(dest="command", required=True)
	for command in ("plans", "list", "new", "send", "answer", "confirm", "status", "close"):
		item = sub.add_parser(command, add_help=False)
		if command == "new":
			item.add_argument("--target", required=True)
			item.add_argument("--plan", required=True)
		elif command not in ("plans", "list"):
			item.add_argument("id")
		if command in ("send", "answer", "confirm"):
			item.add_argument("--source", choices=records.SOURCES, default="cli")
		if command == "send":
			item.add_argument("--text-file", type=Path, required=True)
		elif command in ("answer", "confirm"):
			item.add_argument("--card", required=True)
			if command == "answer":
				item.add_argument("--answers-file", type=Path, required=True)
	return root


def main(argv):
	try:
		args = parser().parse_args(argv)
		engine = Engine()
		if args.command == "plans":
			state = engine.plans()
		elif args.command == "list":
			state = engine.list()
		elif args.command == "new":
			state = engine.new(args.target, args.plan)
		elif args.command == "send":
			state = engine.send(args.id, read_input(args.text_file, 2000), source=args.source)
		elif args.command == "answer":
			state = engine.answer(args.id, args.card, json.loads(read_input(args.answers_file, 30000)), source=args.source)
		elif args.command == "confirm":
			state = engine.confirm(args.id, args.card, source=args.source)
		elif args.command == "close":
			state = engine.close(args.id)
		else:
			state = engine.status(args.id)
		print(json.dumps(state, ensure_ascii=False, allow_nan=False))
		if args.command in ("send", "confirm") and state["failure"]:
			print(FAILURE + state["failure"], file=sys.stderr)
			return 2
		return 0
	except ChatError as exc:
		reason = str(exc)
	except ConfigError:
		reason = "요금제·대상 저장소 설정을 확인하세요"
	except OSError:
		reason = "대화 파일이나 설정을 읽고 저장하지 못했습니다"
	except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
		reason = "입력·대화 기록·설정의 형식을 확인하세요"
	print(FAILURE + reason, file=sys.stderr)
	return 2
