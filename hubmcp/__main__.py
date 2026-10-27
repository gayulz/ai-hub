"""개인 노트 기억 서버.

사용:
	python3 -m hubmcp    표준 입출력으로 MCP 요청을 받는다

로그 파일은 남기지 않는다. 시작 오류만 표준 오류에 쓰며 질의·결과는 기록하지 않는다.
"""

import os
import sys

from .client import DEFAULT_URL, SearchClient
from .server import Server, serve


def main() -> int:
	try:
		client = SearchClient(os.environ.get("AI_HUB_SEARCH_URL", DEFAULT_URL))
	except ValueError as exception:
		sys.stderr.write(str(exception) + "\n")
		return 2
	serve(Server(client), sys.stdin.buffer, sys.stdout)
	return 0


if __name__ == "__main__":
	sys.exit(main())
