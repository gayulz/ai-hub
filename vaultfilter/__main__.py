"""사용법:
	python3 -m vaultfilter candidates          사전 후보(이름과 출현 수만) 출력
	python3 -m vaultfilter add 값 종류         사전에 항목 추가(종류: 회사명, 서비스명, 사람이름 ...)
	python3 -m vaultfilter verify encrypted|messages <범위>   푸시 전 누출 검사(위치·종류만 출력)
	python3 -m vaultfilter verify message-file 파일          커밋 메시지 검사(commit-msg 훅 용)
	python3 -m vaultfilter verify public <rev-list 인자>     공개 저장소 push 전 검사(새 줄·경로·작성자·메시지)
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from collector.config import load_sources

from . import candidates
from .store import KINDS, Dictionary


def cmd_candidates() -> int:
	# 회사 저장소 목록은 저장소에 적지 않고 ~/.ai-hub/sources.toml 에서 읽는다
	roots = [Path(repo.path) for repo in load_sources().repos if repo.cls == "company"]
	names = candidates.scan_names(roots)
	print("## 폴더·파일 이름에서 나온 후보 (출현 수)")
	for word, count in names.most_common(120):
		print(f"{count}\t{word}")
	print("## 커밋 작성자 (커밋 수)")
	authors = sum((candidates.commit_authors(root) for root in roots), Counter())
	for name, count in authors.most_common():
		print(f"{count}\t{name}")
	return 0


def cmd_add(value: str, kind: str) -> int:
	if kind not in KINDS:
		print(f"종류는 {', '.join(KINDS)} 중 하나", file=sys.stderr)
		return 2
	dictionary = Dictionary()
	token = dictionary.token_for(value, kind)
	dictionary.save()
	print(f"추가됨: {token}")
	return 0


def main(argv: list[str]) -> int:
	if not argv:
		print(__doc__, file=sys.stderr)
		return 2
	command = argv[0]
	try:
		if command == "candidates":
			return cmd_candidates()
		if command == "add" and len(argv) == 3:
			return cmd_add(argv[1], argv[2])
		if command == "verify":
			from .verify import main as verify_main
			return verify_main(argv[1:])
	except Exception as exc:  # 검사 실패를 숨기지 않는다(훅이 종료 코드로 거부한다)
		print(f"vaultfilter 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
		return 1
	print(__doc__, file=sys.stderr)
	return 2


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
