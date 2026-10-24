"""읽기 전용 회의 엔진. 답변 출처를 기록하고 요약을 명시 확인한다.

analyze → start(참석자 확인) → answer → resume(답변 요약 확인).
분석·상태 조회·무응답만으로 토론을 시작하지 않는다. 실행과 반영 기능은 없다.
"""

from __future__ import annotations

import time

from vaultfilter.public import Rules

from . import meeting_prompts as prompts, meeting_record as records, providers, targets
from . import question_cards as cards

MAX_CALLS, MAX_SECONDS = 10, 1200
# 공급자의 최소 제한 시간이 30초여서 정리 1회와 저장 여유를 함께 남긴다.
SUMMARY_SECONDS = 35
TERMINAL = ("completed", "budget_exhausted", "questions_exhausted")


class Engine:
	def __init__(self):
		self.store = records.Store()

	def analyze(self, agenda: str, target_id: str = "ai-hub") -> dict:
		rules = Rules()
		agenda = cards.text(agenda, "안건", rules=rules)
		selected = targets.target(target_id)
		cards.text(selected.id, "대상 ID", 31, rules=rules)
		config = prompts.configuration(rules)
		rules_text = prompts.canonical_rules(selected, rules)
		state = records.new(agenda, selected.id)
		with self.store.locked(state["id"], create=True) as record:
			session = Session(record, state, rules)
			session.prepared = (selected, config, rules_text)
			session.save()
			session.analyze()
			return session.state

	def status(self, meeting_id: str) -> dict:
		with self.store.locked(meeting_id) as record:
			state = record.load()
			if any(call["ok"] is None for call in state["calls"]):
				Session(record, state, None).settle()
			return state

	def start(self, meeting_id: str, participants: list[str]) -> dict:
		with self.store.locked(meeting_id) as record:
			state = record.load()
			if state["status"] != "participants_pending" or state["confirmed"]:
				raise ValueError("참석자 명시 확인 대기 중인 회의가 아닙니다")
			if (not isinstance(participants, list) or not 2 <= len(participants) <= 4
					or any(not isinstance(role, str) or role not in prompts.ROLES for role in participants)
					or len(set(participants)) != len(participants)):
				raise ValueError("서로 다른 개인 역할 2~4명의 명시 확인이 필요합니다")
			proposed = state["participants"]
			by_role = {p["role"]: p for p in proposed}
			state["participants"] = [by_role.get(role, {"role": role, "reason": "사용자가 확인 명단에 추가"})
				for role in participants]
			session = Session(record, state, Rules())
			session.prepare()
			state["confirmed"] = True
			adjustments = [{"role": role, "action": "added", "reason": "사용자가 확인 명단에 추가", "source": "cli"}
				for role in participants if role not in by_role]
			adjustments += [{"role": p["role"], "action": "excluded", "reason": "사용자가 확인 명단에서 제외", "source": "cli"}
				for p in proposed if p["role"] not in participants]
			state["participant_confirmation"] = {"roles": participants, "proposed": proposed,
				"adjustments": adjustments, "source": "cli"}
			state["status"] = "running"
			session.save()
			session.run()
			return state

	def answer(self, meeting_id: str, card_id: str, answers: list, *, source: str) -> dict:
		with self.store.locked(meeting_id) as record:
			state = record.load()
			if state["status"] != "questions_pending" or not state["cards"]:
				raise ValueError("현재 답변 대기 중인 회의가 아닙니다")
			cards.accept(state["cards"][-1], card_id, answers, source, Rules())
			state["status"] = "answers_pending"
			record.save(state)
			return state

	def resume(self, meeting_id: str, *, confirm_card: str | None = None, source: str = "cli") -> dict:
		if source not in ("cli", "office", "telegram"):
			raise ValueError("답변 확인 출처가 잘못됐습니다")
		with self.store.locked(meeting_id) as record:
			state = record.load()
			if state["status"] in TERMINAL:
				if confirm_card:
					raise ValueError("종료된 회의에는 답변 확인을 적용할 수 없습니다")
				return state
			session = Session(record, state, Rules())
			session.settle()
			if state["status"] in TERMINAL:
				return state
			if confirm_card is not None:
				if state["status"] != "answers_pending" or not state["cards"]:
					raise ValueError("답변 요약 확인 대기 중이 아닙니다")
				cards.confirm(state["cards"][-1], confirm_card)
				state["cards"][-1]["confirmed_source"] = source
				state["status"] = "running" if state["confirmed"] else "participants_pending"
				if session.questions():
					return state
			if state["status"] in ("questions_pending", "answers_pending", "participants_pending"):
				session.save()
				return state
			session.run()
			return state


class Session:
	def __init__(self, record, state, rules):
		self.record, self.state, self.rules = record, state, rules
		self.started = time.monotonic()
		self.prepared = None

	def save(self):
		self.tick()
		self.record.save(self.state)

	def tick(self):
		moment = time.monotonic()
		self.state["active_seconds"] += max(0, moment - self.started)
		self.started = moment

	def prepare(self):
		if self.prepared is None:
			s = self.state
			cards.text(s["agenda"], "안건", rules=self.rules)
			selected = targets.target(s["target"])
			config = prompts.configuration(self.rules)
			if any(p["role"] not in config or p["role"] not in prompts.ROLES for p in s["participants"]):
				raise ValueError("참석자 담당 요금제 설정이 없습니다")
			self.prepared = (selected, config, prompts.canonical_rules(selected, self.rules))
		return self.prepared

	def settle(self):
		for call in self.state["calls"]:
			if call["ok"] is None:
				# 응답 저장 전 중단은 성공 여부를 모르므로 예약한 시간까지 보수적으로 차감한다.
				self.state["active_seconds"] += min(call["timeout"], max(0, MAX_SECONDS - self.state["active_seconds"]))
				call.update(ok=False, error="interrupted")
				self.state.update(status="failed", failure="호출 중단: 결과 미확인")
		if self.state["active_seconds"] >= MAX_SECONDS:
			self.state.update(status="budget_exhausted", partial=True)
		self.save()

	def ask(self, phase, role=None):
		s = self.state
		selected, config, rules_text = self.prepare()
		candidates = [p["role"] for p in s["participants"]] if s["participants"] else [r for r in config if r != "moderator"]
		contract = prompts.schema(phase, candidates, role)
		prompt = prompts.prompt(s, phase, role, rules_text, candidates)
		cards.text(prompt, "회의 입력", prompts.MAX_PROMPT_CHARS, rules=self.rules)
		self.save()
		reserve = 0 if phase == "summary" else 1
		remaining = MAX_SECONDS - s["active_seconds"]
		if len(s["calls"]) >= MAX_CALLS - reserve or remaining < 30 + reserve * SUMMARY_SECONDS:
			s.update(status="budget_exhausted", partial=True)
			self.save()
			return None
		plan = config[role or "moderator"]
		entry = {"phase": phase, "role": role or "moderator", "plan": plan.id, "model": plan.model,
			"ok": None, "timeout": remaining - reserve * SUMMARY_SECONDS}
		s["calls"].append(entry)
		self.save()
		self.tick()
		timeout = min(entry["timeout"], MAX_SECONDS - s["active_seconds"] - reserve * SUMMARY_SECONDS)
		if timeout < 30:
			entry.update(ok=False, error="budget_before_call")
			s.update(status="budget_exhausted", partial=True)
			self.save()
			return None
		try:
			reply = providers.call(plan, writer=False, prompt=prompt, cwd=self.record.folder,
				schema=contract, timeout=timeout)
			value = prompts.validate_reply(reply.data, phase, candidates, role, s, self.rules)
			entry.update(ok=True)
			self.tick()
			if s["active_seconds"] >= MAX_SECONDS:
				entry["response"] = value
				s.update(status="budget_exhausted", partial=True)
				self.save()
				return None
			return value
		except Exception as exc:
			kind = exc.kind if isinstance(exc, providers.ProviderError) and exc.kind in ("timeout", "exit", "empty", "json", "missing") else type(exc).__name__
			entry.update(ok=False, error=kind)
			s.update(status="failed", failure=phase + ": " + kind)
			self.save()
			return None

	def queue(self, questions, ref):
		self.state["question_queue"].extend({"question": q, "ref": ref} for q in questions)

	def questions(self):
		s = self.state
		if not s["question_queue"]:
			return False
		if len(s["cards"]) >= 5:
			s["status"] = "questions_exhausted"
			s["open_questions"].extend(s["question_queue"])
			self.save()
			return True
		batch = s["question_queue"][:3]
		questions, origins, used = [], {}, set()
		for item in batch:
			q = dict(item["question"])
			base, suffix = q["id"][:32], 1
			while q["id"] in used:
				q["id"] = base + "-" + str(suffix)
				suffix += 1
			used.add(q["id"])
			questions.append(q)
			origins[q["id"]] = item["ref"]
		source = "analysis" if batch[0]["ref"] == "analysis" else "round1"
		card = cards.create(questions, len(s["cards"]) + 1, source, self.rules)
		card["origins"] = origins
		s["cards"].append(card)
		del s["question_queue"][:3]
		s["status"] = "questions_pending"
		self.save()
		return True

	def analyze(self):
		value = self.ask("analysis")
		if value is not None:
			s = self.state
			s.update(analysis=value, participants=value["participants"], phase="round1", status="participants_pending", failure="")
			self.queue(value["questions"], "analysis")
			self.questions()
			self.save()

	def budget_for_round(self, pending):
		self.save()
		if MAX_CALLS - len(self.state["calls"]) < pending + 1 or MAX_SECONDS - self.state["active_seconds"] < 30 + SUMMARY_SECONDS:
			self.state.update(phase="summary", partial=True)
			self.save()
			return False
		return True

	def round(self, number):
		s = self.state
		phase, rows = "round" + number, s["rounds"][number]
		pending = [p["role"] for p in s["participants"] if p["role"] not in {r["role"] for r in rows}]
		if not self.budget_for_round(len(pending)):
			return
		for role in pending:
			value = self.ask(phase, role)
			if value is None:
				if s["status"] == "budget_exhausted":
					s.update(phase="summary", status="running", partial=True)
				return
			rows.append(value)
			self.save()
		s["phase"] = "round2" if number == "1" else "summary"
		if number == "1":
			for row in rows:
				self.queue(row["questions"], "r1:" + row["role"])
			self.questions()
		self.save()

	def summarize(self):
		s = self.state
		if not prompts.sources(s):
			s.update(status="budget_exhausted", partial=True)
			self.save()
			return
		value = self.ask("summary")
		if value is None:
			return
		s.update(summary=value, phase="done", failure="")
		if s["partial"]:
			s["status"] = "budget_exhausted"
		else:
			s["status"] = "record_pending"
		self.save()

	def journal(self):
		s = self.state
		try:
			records.journal(s)
		except Exception as exc:
			s.update(status="record_pending", journal="failed", failure="개인 일지: " + type(exc).__name__)
		else:
			s.update(status="completed", journal="written", failure="")
		self.save()

	def run(self):
		s = self.state
		self.settle()
		if s["status"] in TERMINAL:
			return
		try:
			self.prepare()
			if s["phase"] == "analysis":
				self.analyze()
				return
			if not s["confirmed"]:
				s["status"] = "participants_pending"
				self.save()
				return
			s["status"] = "running"
			while s["status"] == "running":
				if s["phase"] in ("round1", "round2"):
					self.round(s["phase"][-1])
				elif s["phase"] == "summary":
					self.summarize()
				elif s["phase"] == "done":
					s["status"] = "record_pending"
				else:
					raise ValueError("회의 단계가 잘못됐습니다")
			if s["status"] == "record_pending":
				self.journal()
		except Exception as exc:
			s.update(status="failed", failure="회의 처리: " + type(exc).__name__)
			self.save()
