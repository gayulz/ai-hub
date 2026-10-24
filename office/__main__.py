"""사용법: python3 -m office [--port 8790]"""

import argparse

from .server import create_server
from . import telegram_bridge


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--port", type=int, default=8790, help="로컬 서버 포트")
	args = parser.parse_args(argv)
	if not 1 <= args.port <= 65535:
		parser.error("포트는 1~65535여야 합니다")
	bridge = None
	try:
		try:
			bridge = telegram_bridge.start()
		except Exception as exc:
			telegram_bridge.failure(exc)
		with create_server(args.port) as server:
			print(f"사무실 보기: http://127.0.0.1:{server.server_port}", flush=True)
			server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		if bridge is not None:
			bridge.stop.set()
			bridge.health["status"] = "꺼짐"
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
