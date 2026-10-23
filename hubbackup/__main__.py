"""사용법: python3 -m hubbackup run | status | restore --key <파일> --to <폴더> [--overwrite]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import backup, crypto, restore


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(prog="hubbackup", description=__doc__)
	sub = parser.add_subparsers(dest="command", required=True)
	sub.add_parser("run", help="바뀐 파일을 암호화해 사본으로 저장")
	sub.add_parser("status", help="마지막 백업 시각과 크기·실패 수 확인")
	recovery = sub.add_parser("restore", help="비밀키와 목록으로 복원")
	recovery.add_argument("--key", required=True, type=Path, help="별도 보관한 비밀키 파일")
	recovery.add_argument("--to", required=True, type=Path, help="복원할 폴더")
	recovery.add_argument("--overwrite", action="store_true", help="기존 파일도 덮어쓰기")
	args = parser.parse_args(argv)
	try:
		if args.command == "run":
			return int(backup.run().failed > 0)
		if args.command == "restore":
			return int(restore.restore(args.key, args.to, args.overwrite).failed > 0)
		print(backup.status())
		return 0
	except Exception as exc:
		message = str(exc) if isinstance(exc, crypto.BackupError) else "키·파일 권한과 age 명령 설치를 확인하세요."
		print(f"작업 실패: {message}", file=sys.stderr)
		if args.command == "run":
			print(backup.BackupStats(failed=1).summary())
		elif args.command == "restore":
			print(restore.RestoreStats(failed=1).summary())
		return 1


if __name__ == "__main__":
	sys.exit(main())
