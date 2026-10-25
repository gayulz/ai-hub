"""접수는 읽기만 하고 최신 제작 회차만 잠금 안에서 변경한다."""

import copy
import os
import re
from contextlib import contextmanager

from collector.config import home

from . import files, schema, states
from .errors import Conflict, StoreError

ROUND = re.compile(r"round-([0-9]{3})\Z")


def round_name(number):
	schema.integer(number, 1, 999)
	return f"round-{number:03d}"


def latest(parent):
	numbers = []
	for name in os.listdir(parent):
		match = ROUND.fullmatch(name)
		schema.require(match is not None and int(match[1]) > 0)
		with files.child_directory(parent, name):
			numbers.append(int(match[1]))
	return max(numbers, default=0)


class Record:
	def __init__(self, folder, run, *, writable):
		self.folder, self.run, self.writable = folder, run, writable
		self.identity = (run["intake_id"], run["round"], run["created"])
		self.revision = run["revision"]

	def save(self):
		if not self.writable:
			raise StoreError("이전 회차와 읽기 전용 기록은 변경할 수 없습니다")
		schema.validate(self.run)
		schema.require((self.run["intake_id"], self.run["round"], self.run["created"]) == self.identity
			and self.run["revision"] == self.revision)
		candidate = copy.deepcopy(self.run)
		candidate["revision"] += 1
		candidate["updated"] = schema.now()
		schema.validate(candidate)
		files.write_atomic(self.folder, "run.json", candidate)
		self.run = candidate
		self.revision = candidate["revision"]
		return copy.deepcopy(candidate)


class Store:
	@contextmanager
	def base(self):
		with files.directory(home() / "shorts") as parent:
			files.private(parent, folder=True)
			yield parent

	@contextmanager
	def intake(self, intake_id):
		schema.matching(intake_id, schema.ID)
		with self.base() as parent, files.child_directory(parent, intake_id) as folder:
			row = files.read_json(folder, "intake.json", maximum=65536)
			schema.require(type(row) is dict and type(row.get("version")) is int and row["version"] == 1
				and row.get("id") == intake_id and row.get("status") == "intake_confirmed"
				and row.get("confirmed_by") == "office")
			schema.timestamp(row.get("created"), kst=False)
			yield folder

	def new(self, intake_id):
		try:
			with self.intake(intake_id) as intake, files.locked(intake, ".rounds.lock", create=True):
				if "rounds" not in os.listdir(intake):
					files.mkdir(intake, "rounds")
				with files.child_directory(intake, "rounds") as rounds:
					previous = latest(rounds)
					if previous:
						with files.child_directory(rounds, round_name(previous)) as folder:
							try:
								old = schema.validate(files.read_json(folder, "run.json"))
							except FileNotFoundError:
								old = None
							if old is not None:
								schema.require(old["intake_id"] == intake_id and old["round"] == previous)
							if old is not None and (states.unsettled(old) or old["status"] in ("needs_check", "cancelling")):
								raise Conflict("이전 회차의 실행 결과를 먼저 확인해야 합니다")
					with self.base() as base:
						if ".slot" in os.listdir(base):
							owner = files.read_json(base, ".slot")
							schema.require(type(owner) is dict)
							if owner.get("intake_id") == intake_id:
								raise Conflict("이 접수의 실행 슬롯을 먼저 확인해야 합니다")
					name = round_name(previous + 1)
					# 폴더 생성부터 번호를 예약한다. 중단된 번호도 다시 쓰지 않는다.
					files.mkdir(rounds, name)
					with files.child_directory(rounds, name) as folder:
						for child in ("artifacts", "logs"):
							files.mkdir(folder, child)
						with files.locked(folder, ".lock", create=True):
							stamp = schema.now()
							run = {"version": 1, "intake_id": intake_id, "round": previous + 1, "revision": 1,
								"stage": "intake", "status": "waiting", "created": stamp, "updated": stamp,
								"approvals": {"script": None, "preview": None}, "review": None, "artifacts": [],
								"attempts": [], "review_items": {}, "blocked_reason": ""}
							files.write_atomic(folder, "run.json", schema.validate(run))
						return run
		except OSError:
			raise StoreError("제작 회차를 안전하게 만들 수 없습니다") from None

	@contextmanager
	def locked(self, intake_id, number, *, writable=True):
		name = round_name(number)
		try:
			with self.intake(intake_id) as intake, files.locked(intake, ".rounds.lock", shared=True):
				with files.child_directory(intake, "rounds") as rounds:
					if writable and number != latest(rounds):
						raise StoreError("이전 회차는 읽기만 할 수 있습니다")
					with files.child_directory(rounds, name) as folder, files.locked(folder, ".lock", shared=not writable):
						run = schema.validate(files.read_json(folder, "run.json"))
						schema.require(run["intake_id"] == intake_id and run["round"] == number)
						yield Record(folder, run, writable=writable)
		except OSError:
			raise StoreError("제작 회차의 저장·잠금을 확인할 수 없습니다") from None

	def read(self, intake_id, number):
		with self.locked(intake_id, number, writable=False) as record:
			return copy.deepcopy(record.run)
