"""자동 진행 한 바퀴의 상태와 오류를 출력한다."""

import importlib
import sys

from .tick import tick


def main(argv: list[str]) -> int:
	if argv != ["tick"]:
		print("사용법: python3 -m agency tick", file=sys.stderr)
		return 2
	try:
		try:
			importlib.import_module("agency.telegram_link")
		except ModuleNotFoundError:
			pass
		result = tick()
	except Exception as exc:  # 예약 실행 오류는 출력으로 보고하고 종료 코드는 유지한다.
		print(f"오류: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
		return 0
	if result["skipped"]:
		print("다른 진행이 이미 돌고 있습니다")
		return 0
	# 알림 연결(텔레그램 등)이 실패해도 진행은 계속하지만, 로그에는 남겨야 놓친 질문을 찾을 수 있다.
	for name in result.get("hook_failures", []):
		print(f"알림 연결 실패: {name}")
	for row in result["projects"]:
		print(f"{row['id']} {row['before']} -> {row['after']} ({row['action']})")
		if row["error"]:
			print("  오류: " + row["error"])
	return 0
