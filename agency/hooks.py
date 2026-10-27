"""자동 진행기가 상태 변화 때 부르는 연결 지점. 알림(텔레그램 등)은 여기에 등록한다.

진행기는 알림 모듈을 직접 알지 않는다. 알림이 실패해도 프로젝트 진행은 멈추지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable

# before_tick(): 한 바퀴를 돌기 전에 부른다(예: 텔레그램 답을 원장으로 옮기기).
# changed(identity, before, after): 프로젝트 상태가 바뀐 뒤 부른다(예: 막힘·납품 대기 알림).
_before: list[Callable[[], None]] = []
_changed: list[Callable[[str, str, str], None]] = []


def on_before_tick(function: Callable[[], None]) -> None:
	_before.append(function)


def on_changed(function: Callable[[str, str, str], None]) -> None:
	_changed.append(function)


def before_tick() -> list[str]:
	"""등록된 함수를 모두 부르고 실패한 함수 이름만 돌려준다(예외 원문은 담지 않는다)."""
	failed = []
	for function in list(_before):
		try:
			function()
		except Exception:  # 알림 실패가 진행을 막지 않게 한다
			failed.append(getattr(function, "__name__", "unknown"))
	return failed


def changed(identity: str, before: str, after: str) -> list[str]:
	failed = []
	if before == after:
		return failed
	for function in list(_changed):
		try:
			function(identity, before, after)
		except Exception:  # 알림 실패가 진행을 막지 않게 한다
			failed.append(getattr(function, "__name__", "unknown"))
	return failed


def clear() -> None:
	"""시험용: 등록을 모두 지운다."""
	_before.clear()
	_changed.clear()
