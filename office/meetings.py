"""회의 명령만 실행하고 화면에 필요한 상태를 읽는다. 원문은 실행 기록에 쓰지 않는다."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector.config import ConfigError, home
from collector.redact import MASK, redact
from collector.redact_pii import redact_pii
from orchestrator import meeting_prompts, meeting_record, question_cards, targets
from orchestrator.draft import safe_text, string
from vaultfilter.public import Rules

from . import drafts
from .common import action_id, normal, unavailable

ACTIONS = ('meeting_analyze', 'meeting_start', 'meeting_answer', 'meeting_confirm')
KEYS = {
	"meeting_analyze": {"action", "id", "text", "origin"},
	"meeting_start": {"action", "id", "participants"},
	"meeting_answer": {"action", "id", "card", "answers"},
	"meeting_confirm": {"action", "id", "card"},
}
FAILURE = "회의 처리 실패: "
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, ConfigError, RuntimeError)


class Invalid(ValueError):
	pass


class Conflict(ValueError):
	pass


class CommandFailed(RuntimeError):
	pass


class MissingMeeting(LookupError):
	pass


def candidates() -> list[dict]:
	configured = meeting_prompts.configuration(Rules())
	return [{"key": key, "name": role["name"]} for key, role in meeting_prompts.ROLES.items() if key in configured]


def validate(payload: dict) -> None:
	action = payload["action"]
	if action == "meeting_analyze":
		targets.target(payload["id"])
		try:
			rules = Rules()
		except ERRORS:
			raise Invalid("안건 검사 설정을 확인할 수 없어 거부했습니다.") from None
		try:
			question_cards.text(payload["text"], "안건", rules=rules)
			safe_text(payload["id"], rules)
		except ValueError:
			raise Invalid("회사·서비스 이름, 비밀값, 이메일 또는 홈 경로가 포함돼 거부했습니다") from None
	elif action == "meeting_start":
		roles = payload["participants"]
		if (not isinstance(roles, list) or not 2 <= len(roles) <= 4
				or any(not isinstance(role, str) or role not in meeting_prompts.ROLES for role in roles)
				or len(set(roles)) != len(roles)):
			raise Invalid("서로 다른 참석자 역할 2~4개가 필요합니다.")
	else:
		action_id("meeting_card", payload["card"])
		if action == "meeting_answer":
			answers = payload["answers"]
			if not isinstance(answers, list) or not 1 <= len(answers) <= 3:
				raise Invalid("모든 질문의 답 1~3개가 필요합니다.")
			for answer in answers:
				question_cards.exact(answer, ("question_id", "kind", "value"))
				question_cards.identifier(answer["question_id"])
				if answer["kind"] not in ("choice", "text", "delegate"):
					raise Invalid("답변 종류가 올바르지 않습니다.")
				string(answer["value"], "답변", 2000)


def masked(value: str, rules: Rules) -> str:
	if not isinstance(value, str):
		raise ValueError("화면 글 형식 오류")
	value = meeting_prompts.LOCAL_PATH.sub(MASK, unicodedata.normalize("NFC", value))
	value = redact_pii(redact(value))
	if rules.names:
		value = rules.names.sub(MASK, value)
	return MASK if rules.kinds(value) else value


def reason(stderr: str) -> str:
	lines = stderr.strip().splitlines()
	if not lines or not lines[-1].startswith(FAILURE):
		return ""
	try:
		value = redact_pii(redact(lines[-1][len(FAILURE):].strip()))
		return "" if Rules().kinds(value) or question_cards.HOME_PATH.search(value) else value[:200]
	except ERRORS:
		return ""


def read(identity: str) -> dict:
	state = meeting_record.Store().read(identity)
	if state is None:
		raise Conflict("회의 상태를 확인할 수 없습니다. 상태를 다시 읽어 주세요.")
	return state


def current_card(state: dict, identity: str) -> dict:
	card = next((card for card in state["cards"] if card["id"] == identity), None)
	if card is None:
		raise Conflict("현재 대기 카드가 아닙니다. 상태를 다시 읽어 주세요.")
	return card


def answered(card: dict) -> str:
	source = "텔레그램에서" if any(a.get("source") == "telegram" for a in card["answers"]) else "사무실·명령줄에서"
	return "이미 답했습니다(" + source + ")"


def recheck(payload: dict) -> dict:
	state = read(payload["id"])
	if payload["action"] == "meeting_start":
		if state["status"] != "participants_pending" or state.get("confirmed"):
			raise Conflict("참석자 확정 대기 중인 회의가 아닙니다.")
		if set(payload["participants"]) - {role["key"] for role in candidates()}:
			raise Invalid("설정된 회의 후보 역할만 참석할 수 있습니다.")
		return state
	card = current_card(state, payload["card"])
	if payload["action"] == "meeting_answer" and (card["answers"] or card["status"] in ("answered", "confirmed")):
		raise Conflict(answered(card))
	status, card_status = ("questions_pending", "waiting") if payload["action"] == "meeting_answer" else ("answers_pending", "answered")
	if state["status"] != status or card is not state["cards"][-1] or card["status"] != card_status:
		raise Conflict("현재 답변·요약 확인 대기 중인 카드가 아닙니다.")
	if payload["action"] == "meeting_answer":
		try:
			question_cards.accept(copy.deepcopy(card), payload["card"], payload["answers"], "office", Rules())
		except ERRORS:
			raise Invalid("질문 수·선택지·답변 형식과 검사 설정을 확인해 주세요.") from None
	return state


def command(root: Path, repo: Path, argv: list[str], content: str, option: str) -> dict:
	folder = drafts.directory(root, "run/office-meetings")
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder) as stream:
		os.fchmod(stream.fileno(), 0o600)
		stream.write(content)
		stream.flush()
		args = [sys.executable, "-m", "orchestrator", "meeting", *argv, option, stream.name]
		if argv[0] == "answer":
			args += ["--source", "office"]
		completed = subprocess.run(args,
			cwd=repo, env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)},
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=900, shell=False)
	if completed.returncode != 0:
		raise CommandFailed(reason(completed.stderr))
	return json.loads(completed.stdout)


@contextmanager
def private_file(root: Path, relative: str, name: str):
	folder = drafts.directory(root, relative)
	fd = os.open(folder / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
	with os.fdopen(fd, "a", encoding="utf-8") as stream:
		info = os.fstat(stream.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
			raise ValueError("회의 실행 기록은 링크 없는 일반 파일이어야 합니다")
		os.fchmod(stream.fileno(), 0o600)
		yield stream


def finish(process, lock_fd: int, root: Path, identity: str) -> None:
	try:
		stdout, _ = process.communicate()
		row = {"exit_code": process.returncode}
		try:
			state = json.loads(stdout)
			if state["id"] == identity and state["status"] in meeting_record.STATUSES:
				row["status"] = state["status"]
		except ERRORS:
			pass
		with private_file(root, "logs/office-meetings", identity + ".log") as output:
			output.write(json.dumps(row, ensure_ascii=False) + "\n")
	except ERRORS:
		pass
	finally:
		os.close(lock_fd)


def launch(root: Path, repo: Path, payload: dict, lock) -> None:
	identity = payload["id"]
	argv = ["start", identity, "--participants", *payload["participants"]] if payload["action"] == "meeting_start" else [
		"resume", identity, "--confirm-answers", payload["card"], "--source", "office"]
	with private_file(root, "logs/office-meetings", identity + ".log") as output:
		output.write(json.dumps({"action": payload["action"], "status": "시작 요청"}, ensure_ascii=False) + "\n")
		output.flush()
	lock_fd = os.dup(lock.fileno())
	try:
		# 원본 JSON에는 안건·답이 있으므로 파이프로 받고 허용한 상태 칸만 기록한다.
		process = subprocess.Popen([sys.executable, "-m", "orchestrator", "meeting", *argv], cwd=repo,
			env={**os.environ, "AI_HUB_HOME": str(root), "PYTHONPATH": str(repo)}, stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
			shell=False, start_new_session=True, pass_fds=(lock_fd,))
	except OSError:
		os.close(lock_fd)
		raise
	try:
		threading.Thread(target=finish, args=(process, lock_fd, root, identity), daemon=True).start()
	except RuntimeError:
		os.close(lock_fd)
		raise


def analyze_draft(state: dict) -> dict:
	identity = action_id("meeting_start", state["id"])
	if state["status"] not in meeting_record.STATUSES:
		raise ValueError("회의 상태 형식 오류")
	rules = Rules()
	participants = [{"role": p["role"], "reason": masked(p["reason"], rules)} for p in state["participants"]
		if p["role"] in meeting_prompts.ROLES]
	cards = [{"id": action_id("meeting_card", card["id"]), "turn": card["turn"], "summary": masked(card["summary"], rules)}
		for card in state["cards"] if card["status"] == "waiting"]
	if any(type(card["turn"]) is not int or not 1 <= card["turn"] <= 5 for card in cards):
		raise ValueError("질문 차수 형식 오류")
	return {"id": identity, "status": state["status"], "participants": participants, "cards": cards}


def execute(root: Path, payload: dict, audit, repo: Path) -> tuple[int, dict]:
	from .actions import result
	started, status, code, value = time.monotonic(), 200, 0, None
	action, identity = payload["action"], payload["id"]
	try:
		if action == "meeting_analyze":
			validate(payload)
			state = command(root, repo, ["analyze", "--target", identity], payload["text"], "--text-file")
			value = analyze_draft(state)
			message = "회의 참석자 제안을 확인해 주세요."
		else:
			with private_file(root, "run/office-meetings", identity + ".lock") as lock:
				try:
					fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
				except BlockingIOError:
					raise Conflict("같은 회의가 이미 실행 중입니다.") from None
				recheck(payload)
				if action == "meeting_answer":
					try:
						state = command(root, repo, ["answer", identity, "--card", payload["card"]],
							json.dumps(payload["answers"], ensure_ascii=False), "--answers-file")
					except CommandFailed:
						recheck(payload)
						raise
					card = current_card(state, payload["card"])
					if state["id"] != identity or state["status"] != "answers_pending" or card["status"] != "answered":
						raise ValueError("답변 처리 결과 형식 오류")
					value = {"id": identity, "card": payload["card"], "summary": masked(card["summary"], Rules())}
					message = "답변 요약을 확인해 주세요. 확인 뒤 회의를 계속합니다."
				else:
					launch(root, repo, payload, lock)
					message = "회의를 시작했습니다. 진행은 패널에서 봅니다." if action == "meeting_start" else "회의를 계속합니다. 진행은 패널에서 봅니다."
	except Invalid as exc:
		status, code, message = 400, 2, str(exc)
	except Conflict as exc:
		status, code, message = 409, 2, str(exc)
	except subprocess.TimeoutExpired:
		status, code, message = 504, 124, "회의 처리 시간이 제한을 넘었습니다. 상태를 다시 확인하세요."
	except CommandFailed as exc:
		status, code, message = 500, 2, "회의 처리가 실패했습니다." + (" " + FAILURE + str(exc) if str(exc) else " 설정·상태를 확인하세요.")
	except ERRORS:
		status, code, message = 409, 2, "회의 상태·실행 기록을 확인할 수 없습니다. 상태를 다시 읽어 주세요."
	entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "action": action, "id": identity,
		"exit_code": code, "seconds": round(time.monotonic() - started, 3), "ok": code == 0,
		"message": message if code == 0 else "회의 요청을 처리하지 못했습니다. 상태를 다시 확인하세요."}
	try:
		audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
		audit.flush()
	except OSError:
		status, code, message = 500, 2, "활동 기록 저장에 실패했습니다. 다시 실행하기 전에 상태를 확인하세요."
	response = result(code == 0, message)
	if value is not None and code == 0:
		response["draft"] = value
	return status, response


def read_state(root: Path) -> dict:
	items, questions, broken = [], [], False
	try:
		roles = candidates()
	except ERRORS:
		roles = []
	try:
		if (root / "meetings").exists() or (root / "meetings").is_symlink():
			fd = meeting_record.directory(root / "meetings")
			try:
				names = {name for name in os.listdir(fd) if meeting_record.ID.fullmatch(name)}
			finally:
				os.close(fd)
			states = meeting_record.Store().list_states()
			broken = names != {state["id"] for state in states}
			rules = Rules()
			for state in sorted(states, key=lambda state: datetime.fromisoformat(state["created"]).timestamp(), reverse=True):
				try:
					row = {key: state[key] for key in ("id", "status", "created", "active_seconds")}
					row.update(agenda=masked(state["agenda"], rules)[:60], target=masked(state["target"], rules),
						participants=[p["role"] for p in state["participants"] if p["role"] in meeting_prompts.ROLES],
						participant_reasons=participant_reasons(state, rules), calls=len(state["calls"]))
					if "absent" in state:
						row["absent"] = [p["role"] if isinstance(p, dict) else p for p in state["absent"]
							if (p.get("role") if isinstance(p, dict) else p) in meeting_prompts.ROLES]
					if state["status"] == "questions_pending" and state["cards"] and state["cards"][-1]["status"] == "waiting":
						card = state["cards"][-1]
						if type(card["turn"]) is not int or not 1 <= card["turn"] <= 5:
							raise ValueError("질문 차수 형식 오류")
						card_questions = []
						for q in card["questions"]:
							for identity in (q["id"], q["recommended"], *(o["id"] for o in q["options"])):
								safe_text(question_cards.identifier(identity), rules)
							card_questions.append({"id": q["id"], **{key: masked(q[key], rules) for key in ("text", "reason")},
								"recommended": q["recommended"], "allow_delegate": q["allow_delegate"],
								"options": [{key: masked(o[key], rules) for key in ("id", "label", "difference", "reply")} for o in q["options"]]})
						questions.append({"meeting": state["id"], "card": card["id"], "turn": card["turn"], "questions": card_questions})
					items.append(row)
				except ERRORS:
					broken = True
	except ERRORS:
		broken = True
	group = unavailable("일부 회의 기록을 확인할 수 없습니다") if broken else normal()
	return {"meetings": {**group, "items": items[:20]}, "questions": {**group, "items": questions}, "meeting_roles": roles}


def participant_reasons(state: dict, rules: Rules) -> dict:
	from .telegram_bridge import PATH
	return {p["role"]: " ".join(PATH.sub("[경로 생략]", masked(p["reason"], rules)).split())[:120] for p in state.get("participants", [])
		if p["role"] in meeting_prompts.ROLES and p.get("reason")}


def detail(identity: str) -> dict:
	"""쓰기용 잠금·복구를 거치지 않고 공개 가능한 칸만 투영한다."""
	from orchestrator.meeting import MAX_CALLS, MAX_SECONDS
	from .telegram_bridge import PATH
	meeting_record.identity(identity)
	try:
		root_fd = meeting_record.directory(home() / "meetings")
		try:
			folder_fd = os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
		finally:
			os.close(root_fd)
	except FileNotFoundError:
		raise MissingMeeting from None
	try:
		try:
			updated = datetime.fromtimestamp(os.stat("meeting.json", dir_fd=folder_fd, follow_symlinks=False).st_mtime).astimezone().isoformat(timespec="seconds")
		except FileNotFoundError:
			raise ValueError("회의 상태 파일을 읽지 못했습니다") from None
	finally:
		os.close(folder_fd)
	state = meeting_record.Store().read(identity)
	if state is None:
		raise ValueError("회의 상태를 읽지 못했습니다")
	rules = Rules()
	def clean(value):
		return PATH.sub("[경로 생략]", masked(value, rules))
	def line(value, limit=300):
		return " ".join(clean(value).split())[:limit]
	def person(role):
		return {"role": clean(role), "name": clean(meeting_prompts.ROLES[role]["name"])}
	if state["phase"] not in ("analysis", "round1", "round2", "summary", "done"):
		raise ValueError("회의 단계 형식 오류")
	rounds = {"1": [], "2": []}
	for number in rounds:
		for row in state["rounds"][number]:
			text = line(row["method"]) if number == "1" else "\n".join(
				line(label + ": " + " · ".join(item["text"] for item in row[key]))
				for key, label in (("agree", "동의"), ("disagree", "반대"), ("supplement", "보완")) if row[key])
			rounds[number].append({**person(row["role"]), "text": text})
	summary = state["summary"]
	return {"id": clean(identity), "status": clean(state["status"]), "phase": clean(state["phase"]),
		"agenda": clean(state["agenda"]), "target": clean(state["target"]),
		"participants": [{**person(p["role"]), "reason": clean(p.get("reason", ""))} for p in state["participants"]],
		"absent": [{**person(p["role"]), "phase": clean(p["phase"]), "reason": line(p["reason"])} for p in state["absent"]],
		"rounds": rounds, "summary": None if summary is None else {
			key: [clean(item["text"]) for item in summary[source]]
			for key, source in (("conclusion", "conclusion"), ("todo", "todos"), ("open_questions", "open_questions"), ("decisions", "decisions"))},
		"calls": len(state["calls"]), "calls_max": MAX_CALLS, "active_seconds": state["active_seconds"],
		"seconds_max": MAX_SECONDS, "failure": clean("회의를 마치지 못했습니다. 상태를 확인해 주세요." if state["failure"] else ""),
		"updated": clean(updated)}
