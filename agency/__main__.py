"""프로젝트 접수·조회·취소와 부서·스킬 설정 점검 명령."""

import argparse
import sys

from collector.config import ConfigError

from . import project, skills
from .departments import load_departments


def _parser():
	parser = argparse.ArgumentParser(prog="python3 -m agency", description="로컬 대행사 프로젝트 원장")
	commands = parser.add_subparsers(dest="command", required=True)
	new = commands.add_parser("new", help="프로젝트 접수")
	new.add_argument("--dept", required=True, help="부서 식별자")
	new.add_argument("--title", required=True, help="프로젝트 제목")
	new.add_argument("request", nargs="?", default="-", help="요청 글, 생략하거나 -이면 표준입력")
	commands.add_parser("list", help="프로젝트 목록")
	show = commands.add_parser("show", help="프로젝트 상태·명세·작업·예산")
	show.add_argument("id", help="프로젝트 식별자")
	cancel = commands.add_parser("cancel", help="프로젝트 취소")
	cancel.add_argument("id", help="프로젝트 식별자")
	commands.add_parser("check", help="부서 설정과 참조 스킬 점검")
	return parser


def _show(identity):
	row = project.load(identity)
	department = load_departments().get(row["department"])
	if department is None:
		raise ValueError("프로젝트의 부서 설정이 없습니다")
	print(f"프로젝트: {row['id']}\n부서: {row['department']}\n상태: {row['status']}\n제목: {row['title']}")
	print(f"가지: {row['branch']}")
	checkpoint = row["checkpoint"]
	if checkpoint["step"] or checkpoint["detail"]:
		print(f"체크포인트: {checkpoint['step']} · {checkpoint['detail']}")
	else:
		print("체크포인트: 없음")
	missing = project.spec_missing(row, department)
	print("명세 빈칸: " + (", ".join(missing) if missing else "없음"))
	print("작업 목록:")
	for task in row["tasks"]:
		print(f"- {task['id']} [{task['status']}] {task['title']}")
	if not row["tasks"]:
		print("- 없음")
	print(f"예산 사용: 호출 {row['used']['calls']}/{row['budget']['calls']}회, "
		f"시간 {row['used']['seconds']:g}/{row['budget']['minutes'] * 60}초")


def _check():
	found = load_departments()
	problems = 0
	for department in found.values():
		names = dict.fromkeys((department.playbook, *department.skills, department.review_skill))
		for name in names:
			if not name:
				continue
			try:
				skills.load(name)
			except ConfigError as exc:
				print(f"{department.id}: {name}: {exc}")
				problems += 1
	if not problems:
		print(f"설정과 스킬을 확인했습니다 (부서 {len(found)}개)")
	return 1 if problems else 0


# 조각마다 자기 CLI 모듈을 가진다(접수 되묻기·PM 계획·실행기). 모듈은 부를 때만 불러온다.
DELEGATES = {"intake": "intake_cli", "answer": "intake_cli", "plan": "plan_cli", "run": "runner_cli",
	"review": "review_cli", "deliver": "deliver_cli", "tick": "tick_cli", "telegram": "telegram_cli",
	"history": "history_cli"}


def _delegate(argv: list[str]) -> int | None:
	if not argv or argv[0] not in DELEGATES:
		return None
	from importlib import import_module
	return import_module(f"{__package__}.{DELEGATES[argv[0]]}").main(argv)


def main(argv=None) -> int:
	argv = sys.argv[1:] if argv is None else argv
	delegated = _delegate(argv)
	if delegated is not None:
		return delegated
	args = _parser().parse_args(argv)
	try:
		if args.command == "new":
			department = load_departments().get(args.dept)
			if department is None:
				raise ValueError("등록되지 않은 부서입니다")
			request = sys.stdin.read() if args.request == "-" else args.request
			print(project.new(department, args.title, request)["id"])
		elif args.command == "list":
			for row in project.list_projects():
				title = row["title"].replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
				print(f"{row['id']}\t{row['department']}\t{row['status']}\t{title}")
		elif args.command == "show":
			_show(args.id)
		elif args.command == "cancel":
			row = project.cancel(args.id)
			print(f"{row['id']}: {row['status']}")
		elif args.command == "check":
			return _check()
	except (ConfigError, ValueError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	except OSError:
		print("오류: 로컬 파일을 읽거나 저장할 수 없습니다", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
