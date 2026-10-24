"""합성 신호로 23개 고정 역할과 기계실의 출처별 오류 격리를 확인한다."""

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from office import signals
from office.scene import scene
from test_office import OfficeCase


class RoleCase(OfficeCase):
	def setUp(self):
		super().setUp()
		self.company_paths = [self.user / "work/acme", self.user / "work/xyz"]
		self.blog_path = self.user / "workspace/naver-blog-agent"
		rows = ["vault=" + json.dumps(str(self.user / "vault")),
			"[[classify]]\npath=" + json.dumps(str(self.user)) + '\nclass="personal"']
		for pid, path, cls in (("p01", self.company_paths[0], "company"), ("p02", self.company_paths[1], "company"),
				("p03", self.blog_path, "personal")):
			rows += ['[[classify]]\npath='+json.dumps(str(path))+'\nclass='+json.dumps(cls),
				'[[repo]]\nid='+json.dumps(pid)+'\npath='+json.dumps(str(path))+'\nclass='+json.dumps(cls)]
		(self.hub / "sources.toml").write_text("\n".join(rows))

	def roles(self, state=None):
		return {p["name"]: p for p in (state or self.state())["people"]}

	def presence(self, tool="claude", cls="personal", project="p03", age=60, since=1800):
		path = self.hub / f"run/presence/{tool}.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"cls": cls, "project_id": project, "status": "일하는 중",
			"at": (self.now-timedelta(seconds=age)).isoformat(), "since": (self.now-timedelta(seconds=since)).isoformat(),
			"task": "노출 금지 합성 문장", "cwd": "/w/acme-private"}))
		return path

	def log(self, name, age=60):
		path = self.blog_path / ".blogops/logs" / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("노출 금지 합성 로그")
		os.utime(path, (self.now.timestamp()-age,)*2)
		return path


class OfficeRolesTest(RoleCase):
	def test_23_roles_keep_design_appearance_department_seat_and_coordinates(self):
		state = self.state()
		self.assertEqual((len(state["people"]), len(state["rooms"]), len(state["engines"])), (23, 8, 8))
		for actual, expected in zip(state["people"], scene()["people"]):
			for key in ("id", "name", "department", "seat", "x", "y", "appearance", "hair", "shirt", "identity"):
				self.assertEqual(actual[key], expected[key])
			self.assertEqual(actual.get("facing"), expected.get("facing"))
			self.assertTrue({"status", "engine", "updated"} <= actual.keys())
			self.assertEqual("task" in actual, actual["department"] != "company")
		self.assertEqual([p["name"] for p in state["people"] if p["department"] == "lounge"], ["이웃", "편집", "음악"])
		naver = state["people"][-1]
		self.assertEqual((naver["id"], naver["name"], naver["seat"], naver["department"]), (22, "네이버", 23, "blog"))
		self.assertEqual(naver["description"], "네이버 블로그 편집기 올리기·발행 전 확인·검색 순위 관측")

	def test_naver_signal_is_attached_after_tistory_without_changing_other_roles(self):
		with mock.patch.object(signals, "naver", return_value={"status": "작업 중", "engine": "로컬",
				"task": "네이버 편집기 올리기", "updated": self.now.isoformat()}) as naver:
			people = self.roles()
		naver.assert_called_once_with(self.blog_path, self.now)
		self.assertEqual((people["네이버"]["status"], people["네이버"]["task"], people["네이버"]["engine"]),
			("작업 중", "네이버 편집기 올리기", "로컬"))
		for name in ("작가", "분석", "클립", "티스토리"):
			self.assertEqual(people[name]["status"], "대기")

	def test_presence_company_roles_use_project_order_and_continued_duration(self):
		self.presence(cls="company", project="p01", since=1680)
		self.presence(tool="codex", cls="company", project="p02", since=1740)
		state = self.state()
		people = self.roles(state)
		self.assertEqual(people["PM"]["status"], "대기")
		self.assertEqual((people["회사 개발"]["status"], people["회사 개발"]["engine"], people["회사 개발"]["duration"]), ("작업 중", "Claude", 1680))
		self.assertEqual((people["레거시 분석"]["project"], people["레거시 분석"]["engine"]), ("p02", "Codex"))
		self.assertEqual(people["회사 기록"]["status"], "작업 중")
		self.assertEqual(people["이관 QA"]["status"], "대기")
		for value in ("노출 금지", "/w/acme-private"):
			self.assertNotIn(value, json.dumps(state, ensure_ascii=False))

	def test_personal_pm_and_unreadable_signal_are_isolated(self):
		path = self.presence()
		self.log("xyz-agy-DRAFT.log")
		people = self.roles()
		self.assertEqual((people["PM"]["status"], people["PM"]["engine"]), ("작업 중", "Claude"))
		path.write_text("[]")
		people = self.roles()
		self.assertEqual(people["PM"]["status"], "확인 불가")
		self.assertEqual(people["작가"]["status"], "작업 중")

	def test_stale_and_future_presence_do_not_report_work(self):
		for age in (601, -1):
			self.presence(age=age)
			self.assertEqual(self.roles()["PM"]["status"], "대기")

	def test_role_and_review_bind_to_actual_call_tool_not_character_appearance(self):
		self.hold()
		path = self.hub / "runs/2026-10-23-006/run.json"
		for role, name in (("plan", "기획"), ("backend", "백엔드"), ("frontend", "프론트"), ("design", "디자인")):
			data = json.loads(path.read_text())
			data["role"] = role
			path.write_text(json.dumps(data))
			people = self.roles()
			self.assertEqual((people[name]["status"], people[name]["engine"]), ("작업 중", "Codex"))
			self.assertEqual(sum(p["status"] == "작업 중" for p in people.values() if p["department"] == "dev"), 1)
		self.run_record("2026-10-23-006", "작업 중", "review", "acme-agy")
		self.assertEqual((self.roles()["QA"]["status"], self.roles()["QA"]["engine"]), ("작업 중", "AGY"))

	def test_completed_call_is_not_still_implementing_while_run_is_locked(self):
		self.hold()
		path = self.hub / "runs/2026-10-23-006/run.json"
		data = json.loads(path.read_text())
		data["calls"] = [{"role": "implement", "plan": "acme-codex", "ok": True}]
		path.write_text(json.dumps(data))
		self.assertEqual(self.roles()["백엔드"]["status"], "대기")

	def test_broken_active_run_or_events_only_marks_its_role_unavailable(self):
		self.hold()
		folder = self.hub / "runs/2026-10-23-006"
		data = json.loads((folder / "run.json").read_text())
		data["role"] = "frontend"
		(folder / "run.json").write_text(json.dumps(data))
		(folder / "events.jsonl").write_text("[]")
		people = self.roles()
		self.assertEqual(people["프론트"]["status"], "확인 불가")
		self.assertEqual(people["디자인"]["status"], "대기")
		(folder / "run.json").write_text("[]")
		self.assertEqual(self.roles()["백엔드"]["status"], "확인 불가")

	def test_malformed_sources_do_not_break_healthy_roles(self):
		(self.hub / "sources.toml").write_text('vault="/w/xyz"\nrepo=["acme"]')
		self.presence()
		people = self.roles()
		self.assertEqual(people["PM"]["status"], "작업 중")
		self.assertEqual(people["회사 개발"]["status"], "확인 불가")
		self.assertEqual(people["작가"]["status"], "대기")

	def test_blog_filename_engines_and_recent_boundary_without_reading_logs(self):
		for filename in ("xyz-claude-DRAFT.log", "xyz-codex-CLIP_REPLY.log", "xyz-agy-OUTREACH.log"):
			self.log(filename)
		self.log("xyz-agy-TISTORY.log", age=601)
		before = self.snapshot()
		people = self.roles()
		self.assertEqual((people["작가"]["engine"], people["클립"]["engine"], people["이웃"]["engine"]), ("Claude", "Codex", "AGY"))
		self.assertEqual(people["티스토리"]["status"], "대기")
		self.assertNotIn("노출 금지", json.dumps(people, ensure_ascii=False))
		self.assertEqual(before, self.snapshot())

	def test_backfill_nlm_and_outreach_processes_are_independent_signals(self):
		with mock.patch.object(signals, "processes", return_value={"status": "정상", "commands": [
				"/bin/python3 -m collector backfill --project p01", "/opt/acme/bin/nlm list", "python -m serve_outreach"]}):
			people = self.roles()
		self.assertEqual(people["기록"]["status"], "작업 중")
		self.assertEqual((people["분석"]["status"], people["분석"]["engine"]), ("작업 중", "NotebookLM"))
		self.assertEqual(people["이웃"]["status"], "작업 중")
		with mock.patch.object(signals, "processes", return_value={"status": "확인 불가", "commands": []}):
			people = self.roles()
		self.assertEqual(people["분석"]["status"], "확인 불가")
		self.assertEqual(people["PD"]["status"], "대기")
		self.assertEqual(people["총무"]["status"], "오류")

	def test_collector_processing_company_signal_is_metadata_only(self):
		self.conn.execute("update c4_calls set state='done'")
		self.conn.commit()
		path = self.hub / "run/collector-active.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"pid": os.getpid(), "cls": "company", "project_id": "p02", "at": self.now.isoformat()}))
		people = self.roles()
		self.assertEqual((people["회사 기록"]["status"], people["회사 기록"]["project"]), ("작업 중", "p02"))
		self.assertNotIn("task", people["회사 기록"])

	def test_shorts_only_read_metadata_and_support_same_project_child_threads(self):
		folder = self.user / ".codex/sessions/2026/10/23"
		folder.mkdir(parents=True)
		cwd = str(self.user / ".codex/.chatgpt-projects/acme")
		for name, source, age, location in (("main", "cli", 1200, cwd), ("child", {"subagent": {}}, 60, cwd),
				("unrelated", "cli", 60, "/w/xyz")):
			path = folder / (name + ".jsonl")
			path.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": location, "source": source}})+"\n노출 금지 대화 본문")
			os.utime(path, (self.now.timestamp()-age,)*2)
		people = self.roles()
		self.assertEqual(people["PD"]["status"], "대기")
		for name in ("대본", "편집", "음악", "썸네일"):
			self.assertEqual((people[name]["status"], people[name]["task"], people[name]["engine"]), ("작업 중", "보조 작업 중", "Codex"))
		main = folder / "main.jsonl"
		os.utime(main, (self.now.timestamp(),)*2)
		self.assertEqual(self.roles()["PD"]["status"], "작업 중")

	def test_notebooklm_auth_existence_and_failures_are_not_usage_counts(self):
		folder = self.user / ".notebooklm-mcp-cli/profiles/acme"
		folder.mkdir(parents=True)
		path = folder / "storage_state.json"
		path.write_text("읽으면 안 되는 합성 인증 내용")
		original = Path.open
		def guard(location, *args, **kwargs):
			if location == path:
				self.fail("인증 파일 내용을 읽었습니다")
			return original(location, *args, **kwargs)
		with mock.patch.object(Path, "open", guard):
			state = self.state()
		self.assertEqual(next(e for e in state["engines"] if e["id"] == "notebooklm")["status"], "가동 중")
		self.assertIsNone(state["usage"]["notebooklm"]["count"])
		path.unlink()
		self.assertEqual(next(e for e in self.state()["engines"] if e["id"] == "notebooklm")["status"], "로그인 필요")

	def test_engine_failure_is_recent_and_plans_are_required(self):
		path = self.hub / "runs/2026-10-23-006/events.jsonl"
		for age, expected in ((60, "오류"), (601, "가동 중")):
			path.write_text(json.dumps({"at": (self.now-timedelta(seconds=age)).isoformat(),
				"event": "call_failed", "plan": "acme-codex"})+"\n")
			engines = {e["id"]: e for e in self.state()["engines"]}
			self.assertEqual(engines["codex"]["status"], expected)
			self.assertEqual(engines["agy"]["status"], "가동 중")
			self.assertEqual(engines["backup"]["status"], "오류")

	def test_daemon_running_without_an_explicit_tistory_signal_stays_idle(self):
		path = self.blog_path / ".blogops/daemon.json"
		path.parent.mkdir(parents=True)
		path.write_text(json.dumps({"pid": os.getpid(), "status": "running", "updated": self.now.isoformat()}))
		self.assertEqual(self.roles()["티스토리"]["status"], "대기")
