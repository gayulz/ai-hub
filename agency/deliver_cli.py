"""납품 묶음을 보여 주고 정확한 hash로 승인하거나 반려한다."""

import argparse
import sys

from collector.config import ConfigError

from . import delivery, project


def _show(identity: str, bundle: dict) -> None:
	print(f"프로젝트: {identity}\n가지: {bundle['branch']}\n가지 끝: {bundle['branch_tip']}")
	print("기준별 판정:")
	labels = {"pass": "통과", "fail": "실패", "unverified": "미검증"}
	for item in bundle["criteria"]:
		print(f"- {item['criterion']}: {labels[item['verdict']]}")
	tests = bundle["tests"]
	detail = " ".join(str(tests.get("detail", "")).splitlines())
	print("시험 결과: " + ("통과" if tests.get("ok") else "실패") + (" · " + detail if detail else ""))
	print(f"바뀐 파일: {len(bundle['changed'])}개")
	print("실행될 바깥 작업:")
	for action in bundle["actions"]:
		print("- " + {"merge": "본 가지 병합(시험 후 빨리 감기)", "push": "push 및 원격 해시 확인",
			"restart": "재기동 안내만 남김"}[action])
	if "restart" in bundle["actions"]:
		print("재기동 필요: " + bundle["restart_notice"])
	else:
		print("재기동 안내: 없음")
	print(f"승인 명령: python3 -m agency deliver {identity} --approve {bundle['hash']}")


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(prog="python3 -m agency deliver", description=__doc__)
	parser.add_argument("id", help="프로젝트 식별자")
	choice = parser.add_mutually_exclusive_group()
	choice.add_argument("--approve", metavar="hash", help="표시된 납품 묶음 hash 전체로 승인")
	choice.add_argument("--reject", metavar="이유", help="납품 반려 이유")
	if not argv or argv[0] != "deliver":
		parser.print_usage(sys.stderr)
		return 2
	try:
		args = parser.parse_args(argv[1:])
	except SystemExit as exc:
		return exc.code
	try:
		if args.approve is None and args.reject is None:
			_show(args.id, delivery.load_bundle(args.id))
			return 0
		result = delivery.approve(args.id, args.approve) if args.approve is not None else delivery.reject(args.id, args.reject)
		print("상태: " + result["status"])
		if result.get("merge_commit"):
			print("병합 커밋: " + result["merge_commit"])
		if "pushed" in result:
			print("push: " + ("완료" if result["pushed"] else "안 됨"))
			print("원격 해시 일치: " + ("일치" if result["remote_matches"] else "미확인 또는 불일치"))
		if result.get("restart_notice"):
			print("재기동 필요: " + result["restart_notice"])
		if result["status"] == "막힘":
			print("막힘 이유: " + project.load(args.id)["blocked_reason"])
		return 3 if result["status"] == "막힘" else 0
	except (ValueError, ConfigError, RuntimeError) as exc:
		print(f"오류: {exc}", file=sys.stderr)
		return 1
	except OSError:
		print("오류: 로컬 파일을 읽거나 저장할 수 없습니다", file=sys.stderr)
		return 1
