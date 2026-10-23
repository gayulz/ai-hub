"""기본은 비교만 한다. 적용할 때에도 관리 목록 외의 파일은 건드리지 않는다."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from pathlib import Path

from .config import Context, SetupError, author, command, git
from .templates import current, render


def write_file(path: Path, text: str, mode: int) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	# 쓰기가 끝난 파일만 교체한다. 실패한 임시 파일도 자동으로 지우지 않는다.
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
			prefix=".hubsetup-", delete=False) as outgoing:
		temporary = Path(outgoing.name)
		os.fchmod(outgoing.fileno(), mode)
		outgoing.write(text)
		outgoing.flush()
		os.fsync(outgoing.fileno())
	os.replace(temporary, path)


def install(ctx: Context, apply: bool = False, author_name: str | None = None,
		author_email: str | None = None) -> int:
	items = render(ctx)
	changes = []
	for item in items:
		before, mode = current(item)
		if (before, mode) == (item.text, item.mode):
			continue
		changes.append(item)
		print(f"변경: {item.path}")
		if before != item.text:
			print("".join(difflib.unified_diff(before.splitlines(keepends=True), item.text.splitlines(keepends=True),
				fromfile="현재", tofile="템플릿", n=3)), end="")
		if mode != item.mode:
			print(f"권한: {'없음' if mode is None else oct(mode)} → {oct(item.mode)}")
	identity = author(author_name, author_email)
	author_changes = []
	if identity is None:
		print("작성자 설정 건너뜀: sources.toml의 [hubsetup] author_name·author_email 또는 두 작성자 인자를 주세요.")
	else:
		for key, value in zip(("user.name", "user.email"), identity):
			if git(ctx.vault, "config", "--local", "--get", key).stdout.strip() != value:
				author_changes.append((key, value))
				print(f"작성자 설정 변경: {key} (값 생략)")
	registered = 0
	if apply:
		if not shutil.which("launchctl"):
			raise SetupError("launchctl을 찾을 수 없습니다. 파일은 적용하지 않았습니다.")
		for item in changes:
			if item.label is None:
				write_file(item.path, item.text, item.mode)
		for key, value in author_changes:
			if git(ctx.vault, "config", "--local", key, value).returncode:
				raise SetupError("볼트 작성자 설정에 실패했습니다. Git 설정 권한을 확인하세요.")
		(ctx.hub_home / "logs").mkdir(parents=True, exist_ok=True)
		for item in items:
			if item.label is None:
				continue
			target = f"{ctx.domain}/{item.label}"
			loaded = command(["launchctl", "print", target]).returncode == 0
			if item not in changes and loaded:
				continue
			if loaded and command(["launchctl", "bootout", target]).returncode:
				raise SetupError("예약 작업 해제에 실패했습니다. launchd 상태를 확인하세요.")
			if item in changes:
				write_file(item.path, item.text, item.mode)
			if command(["launchctl", "bootstrap", ctx.domain, str(item.path)]).returncode:
				raise SetupError("예약 작업 등록에 실패했습니다. launchd 상태를 확인한 뒤 다시 적용하세요.")
			registered += 1
	print(f"변경 파일 {len(changes)}개 · 작성자 설정 {len(author_changes)}개 · 예약 등록 {registered}개")
	if not apply:
		print("미리보기입니다. --apply를 주면 적용하고 예약 등록 상태를 점검합니다.")
	return 0
