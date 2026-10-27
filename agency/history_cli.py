"""사용자에게 회사 작업의 관련 노트와 커밋을 보여 준다.

이 명령의 출력은 회사 자료를 담으므로 AI 에이전트(Claude·Codex·AGY 등)가 대신 실행하거나 출력을 읽지 않는다. 사용자 터미널과 사무실 화면 전용
"""

import argparse
import re
import sys
from datetime import date

from . import history


def _note_date(path):
	for match in re.finditer(r"(?<![0-9])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}|[0-9]{6})(?![0-9])", path):
		compact = match.group().replace("-", "")
		if len(compact) == 6:
			compact = "20" + compact
		try:
			return date.fromisoformat(compact).isoformat() + " "
		except ValueError:
			continue
	return ""


def main(argv) -> int:
	parser = argparse.ArgumentParser(prog="python3 -m agency history", description="회사 작업 이력을 로컬에서 검색합니다")
	parser.add_argument("query", metavar="질의", help="찾을 문구")
	parser.add_argument("--limit", type=int, default=10, metavar="N", help="결과 개수(1~30)")
	if not argv or argv[0] != "history":
		parser.print_usage(sys.stderr)
		return 2
	try:
		args = parser.parse_args(argv[1:])
		if not 1 <= args.limit <= 30:
			parser.error("limit은 1~30 사이의 정수여야 합니다")
	except SystemExit as exc:
		return exc.code
	try:
		result = history.search(args.query, args.limit)
	except ValueError as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	print("■ 관련 노트")
	for note in result["notes"]:
		print(f"{note['rank']}. {_note_date(note['path'])}{note['heading']} — {note['path']}")
		print(f"   {note['excerpt']}")
	if not result["notes"]:
		print("찾은 것이 없습니다")
	if result.get("notes_error"):
		print(result["notes_error"])
	print("■ 관련 커밋")
	for commit in result["commits"]:
		print(f"{commit['date']} {commit['repo']} {commit['hash']} {commit['subject']}")
	if not result["commits"]:
		print("찾은 것이 없습니다")
	if result.get("commits_error"):
		print(result["commits_error"])
	print("이 결과에는 회사 자료가 들어 있습니다. AI 대화창에 붙여 넣지 마세요.")
	return 0
