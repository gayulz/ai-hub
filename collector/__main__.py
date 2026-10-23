"""사용법: python3 -m collector run [--dry-run] [--since 오늘|YYYY-MM-DD]
        python3 -m collector check [--since 오늘|YYYY-MM-DD]
        python3 -m collector archive
        python3 -m collector backfill --project ID --kind repos|sessions [--since YYYY-MM-DD] [--dry-run]
        python3 -m collector backfill --resume ID | --apply ID
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date

from . import archive, pipeline
from .config import ConfigError


def parse_since(value: str | None) -> date | None:
	if value is None:
		return None
	if value == "오늘":
		return date.today()
	return date.fromisoformat(value)


def cmd_archive() -> int:
	try:
		return int(archive.archive().failed > 0)
	except Exception as exc:
		print(f"보관 실패: {type(exc).__name__}")
		print(archive.ArchiveStats(failed=1).summary())
		return 1


def cmd_run(args: argparse.Namespace) -> int:
	if not args.dry_run:
		cmd_archive()
	sources, plans, warnings = pipeline.load_all()
	for line in warnings:
		print(f"경고: {line}")
	ledger = pipeline.open_ledger()
	run_id = ledger.start_run()
	code, summary = 0, ""
	try:
		since = pipeline.window(parse_since(args.since), date.today())
		runner = pipeline.Pipeline(sources, plans, ledger, args.dry_run)
		runner.run(since)
		summary = ", ".join(f"{k}={v}" for k, v in sorted(runner.counts.items()))
		print(f"{'[dry-run] ' if args.dry_run else ''}기간 {since}~ · {summary or '변화 없음'}")
		if args.dry_run and runner.excluded_titles:
			print("AGY 자동 호출로 제외한 대화(제목만, 확인 필요):")
			for title in runner.excluded_titles:
				print(f"  - {title}")
	except Exception:  # 실행 기록에 남기고 종료 코드로 알린다
		traceback.print_exc()
		summary = "예외"
		code = 1
	finally:
		ledger.finish_run(run_id, code, summary)
		ledger.close()
	return code


def cmd_check(args: argparse.Namespace) -> int:
	sources, plans, warnings = pipeline.load_all()
	for line in warnings:
		print(f"경고: {line}")
	ledger = pipeline.open_ledger()
	try:
		since = pipeline.window(parse_since(args.since), date.today())
		runner = pipeline.Pipeline(sources, plans, ledger, dry_run=True)
		unrecorded, unclassified, waiting = runner.audit(since)
		unregistered = runner.unregistered()
		print(f"기간 {since}~")
		print(f"바뀌었는데 기록 안 된 원천: {unrecorded}건")
		print(f"미분류(볼트에 안 씀, sources.toml에 규칙 추가 필요): {unclassified}건")
		print(f"요약 대기(최소 기록 상태): {waiting}건")
		print(f"미등록 Git 저장소: {len(unregistered)}곳")
		for path in unregistered:
			print(f"  - {path}")
	finally:
		ledger.close()
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(prog="collector")
	sub = parser.add_subparsers(dest="command", required=True)
	run = sub.add_parser("run", help="한 바퀴 수집해 볼트에 기록")
	run.add_argument("--dry-run", action="store_true", help="개수만 세고 쓰지 않는다")
	run.add_argument("--since", help="이 날짜 이후 시작한 대화(오늘 또는 YYYY-MM-DD)")
	check = sub.add_parser("check", help="누락 점검")
	check.add_argument("--since", help="점검 시작일(오늘 또는 YYYY-MM-DD)")
	sub.add_parser("archive", help="대화 원문을 로컬 압축 사본으로 보관")
	backfill = sub.add_parser("backfill", help="프로젝트 소급 결과 준비·재개·승인 반영")
	backfill.add_argument("--project", help="sources.toml에 등록한 원천 ID")
	backfill.add_argument("--kind", choices=("repos", "sessions"))
	backfill.add_argument("--since", type=date.fromisoformat, help="대화 시작일, 기본 2026-07-22")
	backfill.add_argument("--dry-run", action="store_true", help="원장까지 읽기 전용으로 미리보기")
	mode = backfill.add_mutually_exclusive_group()
	mode.add_argument("--resume", help="같은 묶음의 결과 준비를 이어 간다")
	mode.add_argument("--apply", help="확인·승인한 묶음의 캐시만 볼트에 쓴다")
	args = parser.parse_args(argv)
	if args.command == "backfill":
		if args.resume or args.apply:
			if args.project or args.kind or args.since:
				parser.error("--resume/--apply와 프로젝트·종류·기간을 함께 지정할 수 없습니다")
		elif not args.project or not args.kind:
			parser.error("backfill에는 --project와 --kind가 필요합니다")
	try:
		if args.command == "backfill":
			from .backfill import execute
			return execute(project_id=args.project, kind=args.kind, since=args.since,
				dry_run=args.dry_run, resume=args.resume, apply=args.apply)
		if args.command == "archive":
			return cmd_archive()
		return cmd_run(args) if args.command == "run" else cmd_check(args)
	except ConfigError as exc:
		print(f"설정 오류: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	sys.exit(main())
