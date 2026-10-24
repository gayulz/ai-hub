"""개인 대화의 질문 답·요약 확인과 읽기 전용 알림만 연결한다."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from collector.config import home
from collector.redact import redact
from collector.redact_pii import redact_pii
from orchestrator.meeting import Engine
from orchestrator.meeting_record import Store as Meetings
from orchestrator import question_cards
from vaultfilter.public import Rules

from .agents import read_runs
from .telegram_client import Client, TelegramError, configuration
from .telegram_state import CARD, Store, message_id

STATUSES = {"participants_pending": "참석자 확인 대기", "questions_pending": "질문 답변 대기",
	"answers_pending": "답변 요약 확인 대기", "running": "진행 중", "completed": "완료", "failed": "실패",
	"budget_exhausted": "예산 소진", "questions_exhausted": "질문 한도 소진", "record_pending": "기록 대기"}
PATH = re.compile(r"https?://\S+|(?:~?/|[A-Za-z]:[\\/])[^\s<>\"']+|(?:\b[\w.-]+[\\/])+[\w.*-]+")
_runtime = None


def failure(exc):
	logging.getLogger(__name__).warning("텔레그램 오류: %s", exc.kind if isinstance(exc, TelegramError) else type(exc).__name__)


def button(label, data):
	return {"text": label, "callback_data": data}


class Bridge:
	def __init__(self, client, *, engine=None, records=None, state=None):
		self.client, self.engine = client, engine or Engine()
		self.records, self.state = records or Meetings(), state or Store()
		self.root = home()
		self.stop = threading.Event()
		self.health = {"status": client.status, "last_poll": None}
		self.ignored_updates = 0
		self._queue, self._queued = deque(), set()
		self._jobs_lock = threading.Lock()
		self._job = None
		self._next_sync, self._backoff = 0, 1
		self._next_work, self._work_backoff = 0, 1

	def safe(self, value):
		return PATH.sub("[경로 생략]", redact_pii(redact(self.client.clean(value))))

	def send(self, text, buttons=None):
		result = self.client.send_message(self.safe(text), buttons)
		if not isinstance(result, dict) or not message_id(result.get("message_id")):
			raise TelegramError("ResponseError")
		return result["message_id"]

	def question(self, card, number):
		q = card["questions"][number - 1]
		lines, buttons = [q["text"]], []
		for index, option in enumerate(q["options"], 1):
			lines.append(option["label"] + (" (추천)" if option["id"] == q["recommended"] else "") + ": " + option["difference"])
			buttons.append([button(self.safe(option["label"]), f'a|{card["id"]}|{number}|{index}')])
		lines.append("추천 이유: " + q["reason"])
		if q["allow_delegate"]:
			buttons.append([button("알아서 정해 줘", f'a|{card["id"]}|{number}|d')])
		return "\n".join(lines), buttons

	def ready(self, card, local):
		if all(q["id"] in local["pending"] for q in card["questions"]) and local["submit_message"] is None:
			local["submit_message"] = self.send("모든 질문에 답했습니다. 제출하시겠습니까?", [[
				button("제출", "s|" + card["id"]), button("다시 고르기", "r|" + card["id"])]])

	def summary(self, card, local):
		text = self.safe("답변 요약\n" + card["summary"])
		for start in range(0, len(text), 4096):
			buttons = [[button("확인", "c|" + card["id"])]] if start + 4096 >= len(text) else None
			mid = self.send(text[start:start + 4096], buttons)
		local["submit_message"] = mid

	def events(self, meetings):
		events = []
		for meeting in meetings:
			status, mid = meeting["status"], meeting["id"]
			if status == "participants_pending":
				text = "참석자 제안이 있습니다. 사무실에서 확인하고 시작하세요\n" + self.safe(meeting["agenda"])[:40]
			elif status in ("completed", "failed", "budget_exhausted", "questions_exhausted"):
				text = "회의 " + mid + ": " + STATUSES[status]
				conclusion = (meeting.get("summary") or {}).get("conclusion", [])
				if isinstance(conclusion, list):
					text += "\n" + self.safe("\n".join(row["text"] for row in conclusion if isinstance(row, dict) and isinstance(row.get("text"), str)))[:300]
			else:
				continue
			events.append((meeting.get("created", ""), f"meeting:{mid}:{status}", text))
		for run in read_runs(self.root)["items"]:
			if run["status"] == "승인 필요":
				text = "실행 승인 필요: " + run["id"]
				if run.get("public"):
					text += "\n" + self.safe(run.get("goal", ""))[:40]
				events.append((run.get("at") or "", "run:" + run["id"], text))
		return [(key, text) for _, key, text in sorted(events)[-500:]]

	def sync(self):
		meetings = self.records.list_states()
		with self.state.locked() as state:
			events = self.events(meetings)
			if not state["baseline_done"]:
				state["seen"] = [key for key, _ in events]
				state["baseline_done"] = True
			else:
				for key, text in events:
					if key not in state["seen"]:
						self.send(text)
						state["seen"].append(key)
				current_keys = {key for key, _ in events}
				state["seen"] = [key for key in state["seen"] if key not in current_keys] + [key for key, _ in events]
			for meeting in meetings:
				if not meeting["cards"]:
					continue
				card = meeting["cards"][-1]
				local = state["cards"].get(card["id"])
				if meeting["status"] == "questions_pending" and card["status"] == "waiting":
					try:
						question_cards.validate_questions(card["questions"], Rules(), minimum=1)
					except (ValueError, TypeError, KeyError):
						continue
					if local is None:
						local = state["cards"][card["id"]] = {"meeting": meeting["id"], "questions": {},
							"pending": {}, "submit_message": None, "created": datetime.now().astimezone().isoformat()}
					for number, q in enumerate(card["questions"], 1):
						if q["id"] not in local["questions"]:
							local["questions"][q["id"]] = self.send(*self.question(card, number))
					self.ready(card, local)
				elif local and meeting["status"] == "answers_pending" and card["status"] == "answered" and self.telegram(card):
					if local["submit_message"] is None:
						self.summary(card, local)

	@staticmethod
	def telegram(card):
		return bool(card["answers"]) and all(answer.get("source") == "telegram" for answer in card["answers"])

	def already(self, card):
		return "이미 답했습니다(" + ("텔레그램에서" if self.telegram(card) else "사무실·명령줄에서") + ")"

	def current(self, local, card_id):
		meeting = self.records.read(local["meeting"])
		card = next((c for c in meeting["cards"] if c["id"] == card_id), None) if meeting else None
		return meeting, card

	def choose(self, card, local, number, kind, value, label):
		q = card["questions"][number - 1]
		answer = {"kind": kind, "value": value}
		changed = local["pending"].get(q["id"]) != answer
		local["pending"][q["id"]] = answer
		text, buttons = self.question(card, number)
		selected = "\n\n선택됨: " + self.safe(label)
		if changed:
			self.client.edit_message(local["questions"][q["id"]], self.safe(text)[:4096 - len(selected)] + selected, buttons)
		self.ready(card, local)

	def submit(self, card, local):
		if not all(q["id"] in local["pending"] for q in card["questions"]):
			return "모든 질문에 답해 주세요"
		answers = [{"question_id": q["id"], **local["pending"][q["id"]]} for q in card["questions"]]
		try:
			result = self.engine.answer(local["meeting"], card["id"], answers, source="telegram")
		except Exception as exc:
			_, latest = self.current(local, card["id"])
			if latest and latest["status"] != "waiting":
				return self.already(latest)
			reason = "구체적인 답변 또는 허용된 선택지가 필요합니다" if isinstance(exc, ValueError) else "잠시 뒤 다시 제출하세요"
			self.send("제출하지 못했습니다: " + reason)
			return reason
		local["submit_message"] = None
		self.summary(result["cards"][-1], local)
		return "제출했습니다. 요약을 확인해 주세요"

	def callback(self, callback, state):
		data = callback.get("data", "")
		parts = data.split("|") if isinstance(data, str) and len(data.encode("utf-8")) <= 64 else []
		if len(parts) not in (2, 4) or parts[0] not in ("a", "s", "r", "c") or not CARD.fullmatch(parts[1]):
			return "허용하지 않는 응답입니다"
		action, card_id = parts[:2]
		local = state["cards"].get(card_id)
		if local is None:
			return "카드를 확인할 수 없습니다"
		meeting, card = self.current(local, card_id)
		if card is None:
			return "카드를 확인할 수 없습니다"
		mid = callback["message"].get("message_id")
		if action == "c" and len(parts) == 2 and card["status"] == "answered" and self.telegram(card):
			if meeting["status"] != "answers_pending" or meeting["cards"][-1]["id"] != card_id or mid != local["submit_message"]:
				return "현재 요약의 확인 버튼을 눌러 주세요"
			job = (local["meeting"], card_id)
			with self._jobs_lock:
				if job not in self._queued:
					self._queued.add(job)
					self._queue.append(job)
			return "확인했습니다. 회의를 이어갑니다"
		if card["status"] != "waiting":
			return self.already(card)
		if meeting["status"] != "questions_pending" or meeting["cards"][-1]["id"] != card_id:
			return "현재 답변 대기 카드가 아닙니다"
		if action == "a" and len(parts) == 4 and parts[2] in ("1", "2", "3"):
			number = int(parts[2])
			if number > len(card["questions"]):
				return "질문을 확인할 수 없습니다"
			q = card["questions"][number - 1]
			if mid != local["questions"].get(q["id"]):
				return "현재 질문의 버튼을 눌러 주세요"
			if parts[3] == "d" and q["allow_delegate"]:
				self.choose(card, local, number, "delegate", "알아서", "알아서 정해 줘")
			elif parts[3] in ("1", "2", "3") and int(parts[3]) <= len(q["options"]):
				option = q["options"][int(parts[3]) - 1]
				self.choose(card, local, number, "choice", option["id"], option["label"])
			else:
				return "허용하지 않는 선택입니다"
			return "선택했습니다"
		if len(parts) != 2 or mid != local["submit_message"]:
			return "현재 제출 메시지의 버튼을 눌러 주세요"
		if action == "s":
			return self.submit(card, local)
		if action == "r":
			local["pending"], local["submit_message"] = {}, None
			self.client.edit_message(mid, "다시 골라 주세요")
			for number, q in enumerate(card["questions"], 1):
				text, buttons = self.question(card, number)
				self.client.edit_message(local["questions"][q["id"]], self.safe(text), buttons)
			return "다시 골라 주세요"
		return "허용하지 않는 응답입니다"

	def reply(self, message, state):
		mid = message.get("reply_to_message", {}).get("message_id")
		value = message.get("text")
		if not message_id(mid) or not isinstance(value, str):
			return
		for card_id, local in state["cards"].items():
			qid = next((qid for qid, sent in local["questions"].items() if sent == mid), None)
			if qid is None:
				continue
			meeting, card = self.current(local, card_id)
			if card is None:
				return
			if card["status"] != "waiting":
				self.send(self.already(card))
				return
			if meeting["status"] != "questions_pending" or meeting["cards"][-1]["id"] != card_id:
				return
			try:
				if self.client.sensitive(value):
					raise ValueError
				value = question_cards.text(value, "답변", rules=Rules())
			except (ValueError, TypeError):
				self.send("답을 담지 못했습니다: 비밀값·경로가 없는 구체적인 답변이 필요합니다")
				return
			number = next(i for i, q in enumerate(card["questions"], 1) if q["id"] == qid)
			self.choose(card, local, number, "text", value, "직접 답변")
			return

	def handle(self, update, state):
		callback = update.get("callback_query")
		message = callback.get("message", {}) if isinstance(callback, dict) else update.get("message", {})
		sender = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {}) if isinstance(message, dict) else {}
		if not self.client.private(message, sender):
			self.ignored_updates += 1
			return
		if callback is not None:
			if isinstance(callback.get("id"), str):
				self.client.answer_callback(callback["id"], self.safe(self.callback(callback, state)))
		else:
			self.reply(message, state)

	def poll_once(self):
		with self.state.locked() as state:
			offset = state["offset"]
		updates = self.client.get_updates(offset)
		for update in updates:
			if not isinstance(update, dict) or type(update.get("update_id")) is not int or update["update_id"] < offset:
				continue
			with self.state.locked() as state:
				try:
					self.handle(update, state)
				finally:
					state["offset"] = max(state["offset"], update["update_id"] + 1)
		self.health.update(status="켜짐", last_poll=datetime.now().astimezone().isoformat())

	def work_once(self):
		if self._job is None:
			with self._jobs_lock:
				job = self._queue.popleft() if self._queue else None
			if job:
				mid, cid = job
				meeting = self.records.read(mid)
				card = meeting["cards"][-1] if meeting and meeting["cards"] else None
				if not card or card["id"] != cid or card["status"] != "answered" or not self.telegram(card):
					with self._jobs_lock:
						self._queued.discard(job)
					return
				try:
					process = subprocess.Popen([sys.executable, "-m", "orchestrator", "meeting", "resume", mid,
						"--confirm-answers", cid, "--source", "telegram"], shell=False,
						stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
						cwd=Path(__file__).resolve().parents[1], env={**os.environ, "AI_HUB_HOME": str(self.root)})
				except Exception:
					with self._jobs_lock:
						self._queued.discard(job)
					self.send("회의 이어가기: 실행하지 못했습니다. 확인 버튼으로 다시 시도하세요")
				else:
					self._job = {"process": process, "key": job, "deadline": time.monotonic() + 1500, "timeout": False}
		if self._job:
			job, process = self._job, self._job["process"]
			if process.poll() is None:
				if time.monotonic() < job["deadline"]:
					return
				process.kill()
				process.wait(timeout=1)
				job["timeout"] = True
			meeting = self.records.read(job["key"][0])
			status = "시간 초과" if job["timeout"] else STATUSES.get(meeting["status"] if meeting else "", "확인 불가")
			self.send("회의 " + job["key"][0] + ": " + status)
			with self._jobs_lock:
				self._queued.discard(job["key"])
			self._job = None

	def poll(self):
		delay = 1
		while not self.stop.is_set():
			try:
				self.poll_once()
				delay = 1
			except Exception as exc:
				self.health["status"] = "확인 불가"
				failure(exc)
				self.stop.wait(delay)
				delay = min(delay * 2, 300)

	def worker(self):
		while not self.stop.is_set():
			if time.monotonic() >= self._next_work:
				try:
					self.work_once()
					self._work_backoff = 1
				except Exception as exc:
					self._next_work = time.monotonic() + self._work_backoff
					self._work_backoff = min(self._work_backoff * 2, 300)
					self.health["status"] = "확인 불가"
					failure(exc)
			if time.monotonic() >= self._next_sync:
				try:
					self.sync()
					self._next_sync, self._backoff = time.monotonic() + 10, 1
				except Exception as exc:
					self._next_sync = time.monotonic() + self._backoff
					self._backoff = min(self._backoff * 2, 300)
					self.health["status"] = "확인 불가"
					failure(exc)
			self.stop.wait(1)


def status():
	if _runtime is not None and _runtime.root == home():
		return dict(_runtime.health)
	try:
		_, state = configuration()
		return {"status": "꺼짐" if state == "켜짐" else state, "last_poll": None}
	except Exception:
		return {"status": "확인 불가", "last_poll": None}


def start():
	global _runtime
	try:
		if _runtime is not None and _runtime.root == home() and _runtime.client.status == "켜짐" and not _runtime.stop.is_set():
			return _runtime
		client = Client()
		_runtime = Bridge(client)
		if client.status == "켜짐":
			with _runtime.state.locked():
				pass
			for name, target in (("telegram-poll", _runtime.poll), ("telegram-worker", _runtime.worker)):
				threading.Thread(name=name, target=target, daemon=True).start()
		return _runtime
	except Exception as exc:
		if _runtime:
			_runtime.stop.set()
			_runtime.health["status"] = "확인 불가"
		failure(exc)
		return None
