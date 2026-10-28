"""개인 자료·공고·근거 기반 초안 명령."""

from __future__ import annotations

import argparse
import json
import sys

from collector.config import ConfigError

from . import applications, drafts, materials, storage


def parser():
	root = argparse.ArgumentParser(prog="python3 -m jobs")
	commands = root.add_subparsers(dest="command", required=True)
	material = commands.add_parser("material", help="개인 자료 등록과 AI 사용 허용")
	material_commands = material.add_subparsers(dest="action", required=True)
	add = material_commands.add_parser("add", help="파일 또는 표준입력을 새 자료 버전으로 등록")
	input_group(add)
	add.add_argument("--previous", default="", help="직접 입력을 기존 자료의 다음 버전으로 등록")
	for action in ("allow", "deny", "show"):
		material_commands.add_parser(action).add_argument("id")
	material_commands.add_parser("list")
	register = commands.add_parser("register", help="공고 URL과 수동 입력 본문 등록")
	register.add_argument("--url", default=applications.UNKNOWN)
	register.add_argument("--company", required=True)
	register.add_argument("--role", required=True)
	register.add_argument("--deadline", default=applications.UNKNOWN, help="시간대가 있는 ISO 시각 또는 미확인")
	input_group(register)
	commands.add_parser("list", help="공고별 상태 목록")
	commands.add_parser("deadlines", help="마감 임박 순 목록")
	commands.add_parser("show", help="공고와 서류 버전 확인").add_argument("id")
	commands.add_parser("draft", help="허용 자료의 발췌로 새 초안 작성").add_argument("id")
	status = commands.add_parser("status", help="공고 상태 변경")
	status.add_argument("id")
	status.add_argument("state", choices=applications.STATES)
	return root


def input_group(command):
	group = command.add_mutually_exclusive_group(required=True)
	group.add_argument("--file", help="UTF-8 텍스트 파일")
	group.add_argument("--stdin", action="store_true", help="표준입력 텍스트")


def read_input(args):
	if args.file:
		return materials.source_file(args.file)
	return materials.text_value(sys.stdin.read(materials.MAX_BYTES + 1)), "직접 입력", ""


def material_summary(row):
	return {key: row[key] for key in ("id", "source", "registered_at", "sha256", "ai_allowed", "previous_id", "version")}


def application_summary(row):
	return {key: row[key] for key in ("id", "company", "role", "deadline", "status", "updated_at")}


def execute(args):
	input_data = read_input(args) if (args.command == "register" or
		(args.command == "material" and args.action == "add")) else None
	with storage.transaction() as root:
		if args.command == "material":
			if args.action == "add":
				row, created = materials.add(root, *input_data, previous=args.previous)
				return {"created": created, "material": material_summary(row)}
			if args.action in ("allow", "deny"):
				row = materials.permission(root, args.id, args.action == "allow")
				return material_summary(row)
			if args.action == "show":
				return materials.checked(storage.get(root, "materials", args.id, "m"))
			return [material_summary(row) for row in materials.records(root)]
		if args.command == "register":
			row, created = applications.register(root, args.url, input_data[0], args.company, args.role, args.deadline)
			return {"created": created, "application": application_summary(row)}
		if args.command in ("list", "deadlines"):
			rows = applications.deadlines(root) if args.command == "deadlines" else applications.records(root)
			return [application_summary(row) for row in rows]
		if args.command == "show":
			return applications.get(root, args.id)
		if args.command == "draft":
			return drafts.create(root, args.id)
		row = applications.get(root, args.id)
		applications.set_status(root, row, args.state)
		return application_summary(row)


def main():
	args = parser().parse_args()
	try:
		result = execute(args)
	except drafts.DraftError as exc:
		print(json.dumps({"error": "초안을 만들지 못했습니다", "kind": exc.kind}, ensure_ascii=False), file=sys.stderr)
		return 1
	except (ValueError, ConfigError) as exc:
		message = str(exc) if isinstance(exc, ValueError) else "설정을 확인하세요"
		print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
		return 1
	except (OSError, UnicodeError):
		print(json.dumps({"error": "입력 또는 저장 파일을 확인하세요"}, ensure_ascii=False), file=sys.stderr)
		return 1
	print(json.dumps(result, ensure_ascii=False, indent=2))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
