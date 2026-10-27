"""텔레그램 질문 동기화와 프로젝트별 처리 수를 확인한다."""

import sys

from collector.config import ConfigError

from . import project


def main(argv):
	if argv not in (["telegram", "sync"], ["telegram", "status"]):
		print("사용법: telegram sync | telegram status", file=sys.stderr)
		return 2
	from . import telegram_link
	try:
		enabled = telegram_link.client.status == "켜짐"
		if argv[1] == "sync":
			if enabled:
				telegram_link.sync()
			else:
				print("텔레그램 연동이 꺼져 있습니다")
		else:
			print("텔레그램 연동: " + ("켜짐" if enabled else "꺼짐"))
			for row in project.list_projects():
				state = telegram_link.read_state(row["id"])
				sent = sum(ask_id.startswith("k-") for ask_id in state["asks"])
				print(f"{row['id']}: 보낸 질문 {sent}개 · 답 반영 {state['answered']}개")
		return 0
	except (ConfigError, OSError, RuntimeError, ValueError):
		print("텔레그램 연동을 처리하지 못했습니다. 설정과 기록을 확인하세요", file=sys.stderr)
		return 2
