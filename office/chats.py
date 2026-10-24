"""별도 명령 프로세스로 PM 대화 다섯 동작만 실행한다. 원문은 로그에 남기지 않는다."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from orchestrator import chat_record, question_cards, targets
from orchestrator.draft import user_text
from vaultfilter.public import Rules

from . import chat_state, drafts
from .chat_state import ERRORS, STATUSES, MissingChat, detail, read_state, snapshot
from .common import action_id
from .meetings import answered, private_file

ACTIONS = ('chat_new', 'chat_send', 'chat_answer', 'chat_confirm', 'chat_close')
KEYS = {
	"chat_new": {"action", "id", "plan"},
	"chat_send": {"action", "id", "text"},
	"chat_answer": {"action", "id", "card", "answers"},
	"chat_confirm": {"action", "id", "card"},
	"chat_close": {"action", "id"},
}
# 대화는 새 대화·글·답·요약 확인·닫기만 한다. 반영·실행·push·삭제·재시작·임의 명령은 없다.
# dev_draft는 후속 화면에서 사용자가 기존 dev_draft에 직접 붙인다. 새 실행 동작을 만들지 않는다.


class Invalid(ValueError):
	pass


class Conflict(ValueError):
	pass


class CommandFailed(RuntimeError):
	pass


def checked_text(value, rules: Rules) -> str:
	try:
		return question_cards.text(user_text(value, rules), "사용자 글", 2000, rules=rules)
	except (ValueError, TypeError):
		raise Invalid("사용자 글의 길이·형식 또는 회사·서비스 이름, 비밀값, 이메일·홈 경로가 포함돼 거부했습니다") from None


def validate(payload: dict) -> None:
	try:
		if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
			raise ValueError
		action = payload["action"]
		question_cards.exact(payload, KEYS[action])
		action_id(action, payload["id"])
		if action == "chat_new":
			if not isinstance(payload["plan"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["plan"]):
				raise ValueError
			targets.target(payload["id"])
		if action in ("chat_answer", "chat_confirm"):
			action_id("chat_card", payload["card"])
		if action == "chat_answer":
			answers = payload["answers"]
			if not isinstance(answers, list) or not 1 <= len(answers) <= 3:
				raise ValueError
			for answer in answers:
				question_cards.exact(answer, ("question_id", "kind", "value"))
				question_cards.identifier(answer["question_id"])
				if answer["kind"] not in ("choice", "text", "delegate"):
					raise ValueError
	except (ValueError, TypeError, KeyError):
		raise Invalid("허용된 대화 동작과 본문 키·식별자·답변 형식이 필요합니다.") from None
	if action in ("chat_new", "chat_send", "chat_answer"):
		try:
			rules = Rules()
		except ERRORS:
			raise Invalid("사용자 글 검사 설정을 확인할 수 없어 거부했습니다.") from None
		if action == "chat_new":
			checked_text(payload["id"], rules)
			checked_text(payload["plan"], rules)
		elif action == "chat_send":
			checked_text(payload["text"], rules)
		else:
			for answer in payload["answers"]:
				checked_text(answer["value"], rules)


def recheck(payload: dict) -> dict:
	# 서버는 엔진 잠금을 잡지 않는다. 엔진은 항상 별도 명령 프로세스에서 재확인한다.
	state = chat_record.Store().read(payload["id"])
	if state is None:
		raise Conflict("대화 상태를 확인할 수 없습니다. 상태를 다시 읽어 주세요.")
	if state["status"] == "closed":
		raise Conflict("닫힌 대화에는 보낼 수 없습니다.")
	if state["status"] == "busy":
		raise Conflict("이미 답을 쓰는 중입니다.")
	action = payload["action"]
	card = state["cards"][-1] if state["cards"] else None
	if action == "chat_send" and card and card["status"] != "confirmed":
		raise Conflict("질문 카드에 답하고 요약을 먼저 확인하세요")
	if action in ("chat_answer", "chat_confirm"):
		if card is None or card["id"] != payload["card"]:
			raise Conflict("현재 답변·요약 확인 대기 중인 카드가 아닙니다.")
		if action == "chat_answer" and card["status"] != "waiting":
			raise Conflict(answered(card))
		if action == "chat_confirm" and card["status"] != "answered":
			raise Conflict("답변 요약 확인이 필요한 현재 카드가 아닙니다.")
		try:
			rules = Rules()
			if action == "chat_answer":
				card = copy.deepcopy(card)
				question_cards.accept(card, payload["card"], payload["answers"], "office", rules)
			checked_text(card["summary"], rules)
		except ERRORS:
			raise Invalid("질문 수·선택지·답변 형식과 검사 설정을 확인해 주세요.") from None
	return state


def input_file(root: Path, content: str) -> Path:
	folder = drafts.directory(root, "run/office-chats")
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as stream:
		path = Path(stream.name)
		try:
			os.fchmod(stream.fileno(), 0o600)
			stream.write(content)
			stream.flush()
		except BaseException:
			path.unlink(missing_ok=True)
			raise
	return path


def command(root: Path, repo: Path, argv: list[str], content: str | None = None) -> dict:
	path = None
	try:
		if content is not None:
			path = input_file(root, content)
			argv = [*argv, "--answers-file", str(path), "--source", "office"]
		completed = subprocess.run([sys.executable, "-m", "orchestrator", "chat", *argv], cwd=repo,
			env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)},
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60, shell=False)
		if completed.returncode != 0:
			raise CommandFailed
		state = json.loads(completed.stdout)
		return chat_record.check(state, state["id"])
	finally:
		if path is not None:
			path.unlink(missing_ok=True)


def log(root: Path, identity: str, action: str, code: int, status: str) -> None:
	with private_file(root, "logs/office-chats", identity + ".log") as output:
		output.write(json.dumps({"action": action, "exit_code": code, "status": status}, ensure_ascii=False) + "\n")


def finish(process, lock_fd: int, root: Path, identity: str, action: str, path: Path | None) -> None:
	code, status = -1, "확인 불가"
	try:
		try:
			stdout, _ = process.communicate()
			code = process.returncode
			state = json.loads(stdout)
			if state["id"] == identity and state["status"] in STATUSES:
				status = state["status"]
		except ERRORS:
			pass
		log(root, identity, action, code, status)
	except ERRORS:
		pass
	finally:
		try:
			if path is not None:
				path.unlink(missing_ok=True)
		finally:
			os.close(lock_fd)


def launch(root: Path, repo: Path, payload: dict, lock) -> None:
	identity, action = payload["id"], payload["action"]
	path, lock_fd = None, None
	try:
		with private_file(root, "logs/office-chats", identity + ".log"):
			pass
		if action == "chat_send":
			path = input_file(root, payload["text"])
			argv = ["send", identity, "--text-file", str(path), "--source", "office"]
		else:
			argv = ["confirm", identity, "--card", payload["card"], "--source", "office"]
		lock_fd = os.dup(lock.fileno())
		# 원문은 파이프로만 받고 종료 코드·상태·동작 이름만 기록한다.
		process = subprocess.Popen([sys.executable, "-m", "orchestrator", "chat", *argv], cwd=repo,
			env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)}, stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
			shell=False, start_new_session=True, pass_fds=(lock_fd,))
	except BaseException:
		if lock_fd is not None:
			os.close(lock_fd)
		if path is not None:
			path.unlink(missing_ok=True)
		raise
	try:
		threading.Thread(target=finish, args=(process, lock_fd, root, identity, action, path), daemon=True).start()
	except RuntimeError:
		process.terminate()
		finish(process, lock_fd, root, identity, action, path)
		raise


def execute(root: Path, payload: dict, audit, repo: Path) -> tuple[int, dict]:
	from .actions import result
	started, status, code, value = time.monotonic(), 200, 0, None
	action, identity = payload["action"], payload["id"]
	try:
		validate(payload)
		if action == "chat_new":
			state = command(root, repo, ["new", "--target", identity, "--plan", payload["plan"]])
			if state["target"] != identity or state["plan"] != payload["plan"] or state["status"] != "idle":
				raise ValueError("새 대화 처리 결과 형식 오류")
			value = chat_state.summary(state, Rules())
			log(root, state["id"], action, 0, state["status"])
			message = "새 대화를 만들었습니다."
		else:
			with private_file(root, "run/office-chats", identity + ".lock") as lock:
				try:
					fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
				except BlockingIOError:
					raise Conflict("이미 답을 쓰는 중입니다.") from None
				recheck(payload)
				if action in ("chat_send", "chat_confirm"):
					launch(root, repo, payload, lock)
					message = "답을 쓰는 중입니다. 진행은 패널에서 봅니다."
				else:
					try:
						state = command(root, repo, ["answer", identity, "--card", payload["card"]],
							json.dumps(payload["answers"], ensure_ascii=False)) if action == "chat_answer" else command(root, repo, ["close", identity])
					except CommandFailed:
						recheck(payload)
						raise
					if state["id"] != identity or state["status"] != ("idle" if action == "chat_answer" else "closed"):
						raise ValueError("대화 처리 결과 형식 오류")
					if action == "chat_answer":
						card = state["cards"][-1] if state["cards"] else None
						if card is None or card["id"] != payload["card"] or card["status"] != "answered":
							raise ValueError("답변 처리 결과 형식 오류")
						value = {"id": identity, "card": payload["card"], "summary": chat_state.clean(card["summary"], Rules())}
						message = "답변 요약을 확인해 주세요. 확인 뒤 대화를 계속합니다."
					else:
						message = "대화를 닫았습니다."
					log(root, identity, action, 0, state["status"])
	except Invalid as exc:
		status, code, message = 400, 2, str(exc)
	except Conflict as exc:
		status, code, message = 409, 2, str(exc)
	except subprocess.TimeoutExpired:
		status, code, message = 504, 124, "대화 처리 시간이 제한을 넘었습니다. 상태를 다시 확인하세요."
	except CommandFailed:
		status, code, message = 500, 2, "대화 처리가 실패했습니다. 설정·상태를 확인하세요."
	except ERRORS:
		status, code, message = 409, 2, "대화 상태·실행 기록을 확인할 수 없습니다. 상태를 다시 읽어 주세요."
	entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "id": identity,
		"exit_code": code, "seconds": round(time.monotonic() - started, 3), "ok": code == 0,
		"message": message if code == 0 else "대화 요청을 처리하지 못했습니다. 상태를 다시 확인하세요."}
	try:
		audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		status, code, message = 500, 2, "활동 기록 저장에 실패했습니다. 다시 실행하기 전에 상태를 확인하세요."
	response = result(code == 0, message)
	if value is not None and code == 0:
		response["draft"] = value
	return status, response
