"""출처별 오류 격리·실제 집계·읽기 전용 경계를 확인한다."""

import json
import os
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest import mock

from collector.backfill_store import Store
from office import agents, ledger
from office.common import text
from test_office import OfficeCase


class OfficeStateTest(OfficeCase):
	def test_r1_groups_only_pending_notes_without_reading_the_vault(self):
		path = self.hub / "vaultreorg/r1-plan.json"
		path.parent.mkdir()
		path.write_text(json.dumps({"notes": [{"folder": "Career", "status": "pending"},
			{"folder": "Career", "status": "pending"}, {"folder": "Career", "status": "applied"},
			{"folder": "Study", "status": "unchanged"}, {"folder": "AI & Prompts", "status": "pending"},
			{"folder": "root", "status": "pending"}]}))
		before = self.snapshot()
		group = self.state()["approvals"]["r1"]
		self.assertEqual(group["status"], "정상")
		self.assertEqual(group["items"], [{"id": folder, "pending": count, "reply": f"R1 {folder} 반영해"}
			for folder, count in (("AI & Prompts", 1), ("Career", 2), ("root", 1))])
		self.assertEqual(before, self.snapshot())

	def test_missing_malformed_and_linked_r1_plan_only_disable_r1_section(self):
		path = self.hub / "vaultreorg/r1-plan.json"
		path.parent.mkdir()
		cases = [None, "[", "[" * 1500, "[]", "{}", '{"notes":{}}', '{"notes":[null]}',
			'{"notes":[{"folder":"Career","status":"xyz"}]}', '{"notes":[{"folder":"../xyz","status":"pending"}]}']
		for value in cases:
			with self.subTest(value=value):
				if value is not None:
					path.write_text(value)
				state = self.state()
				self.assertEqual(state["approvals"]["r1"]["status"], "확인 불가")
				self.assertEqual(state["approvals"]["r1"]["items"], [])
				self.assertEqual(state["approvals"]["c4"]["status"], "정상")
				self.assertEqual(state["approvals"]["runs"]["status"], "정상")
		path.unlink()
		path.symlink_to(self.hub / "plans.toml")
		self.assertEqual(self.state()["approvals"]["r1"]["status"], "확인 불가")

	def test_action_activity_keeps_latest_twenty_and_skips_broken_records(self):
		path = self.hub / "logs/office-actions.jsonl"
		with path.open("w") as out:
			out.write("{\n")
			for n in range(25):
				out.write(json.dumps({"at": (self.now + timedelta(minutes=n)).isoformat(), "action": "r1_apply",
					"id": "Career", "message": "R1 폴더 반영을 마쳤습니다.", "output": "acme-private-material"}) + "\n")
		activity = self.state()["activity"]
		self.assertEqual(len(activity), 20)
		self.assertTrue(all(row["source"] == "office-action" for row in activity))
		self.assertNotIn("acme-private-material", json.dumps(activity))

	def test_deeply_nested_action_log_does_not_hide_healthy_state(self):
		(self.hub / "logs/office-actions.jsonl").write_text("[" * 1500 + "\n")
		state = self.state()
		self.assertEqual(state["approvals"]["c4"]["status"], "정상")
		self.assertTrue(state["activity"])

	def test_json_reader_recursion_errors_are_isolated_to_their_source(self):
		plan = self.hub / "vaultreorg/r1-plan.json"
		plan.parent.mkdir()
		plan.write_text('{"notes":[]}')
		(self.hub / "logs/office-actions.jsonl").write_text("{}\n")
		for method in ("load", "loads"):
			with self.subTest(method=method), mock.patch.object(ledger, "json", wraps=json) as reader:
				getattr(reader, method).side_effect = RecursionError
				state = self.state()
				self.assertEqual(state["approvals"]["c4"]["status"], "정상")
				self.assertEqual(state["approvals"]["runs"]["status"], "정상")
				self.assertEqual(state["approvals"]["r1"]["status"], "확인 불가" if method == "load" else "정상")

	def test_metadata_preserves_dates_and_counts_but_omits_absolute_paths(self):
		self.assertEqual(text("10/23 xyz 7/17건"), "10/23 xyz 7/17건")
		self.assertEqual(text("xyz /acme/xyz"), "xyz [경로 생략]")

	def test_counts_locked_agent_approvals_and_private_fields(self):
		self.hold()
		before = self.snapshot()
		state = self.state()
		people = self.people(state)
		self.assertEqual(people["codex"]["status"], "구현 중")
		self.assertEqual(people["codex"]["calls"], 3)
		self.assertEqual(people["agy"]["status"], "대기 중")
		self.assertEqual(people["claude"]["status"], "대기")
		self.assertEqual(people["gemini"]["status"], "대기")
		self.assertEqual(people["gemma"]["status"], "꺼짐")
		self.assertEqual(people["blog"]["status"], "일하는 중")
		self.assertEqual(people["blog"]["dashboard"]["url"], "http://127.0.0.1:9000/dashboard")
		batch = state["approvals"]["c4"]["items"][0]
		self.assertEqual((batch["prepared"], batch["held"], batch["reply"]), (1, 1, "C4 c4-acme 반영해"))
		self.assertEqual(state["approvals"]["runs"]["items"][0]["reply"], "python3 -m orchestrator apply 2026-10-23-005")
		self.assertEqual(len(state["approvals"]["decisions"]["items"]), 1)
		self.assertEqual(state["board"]["progress"]["items"], [{"project": "p01", "kind": "sessions", "applied": 1, "total": 3}])
		self.assertEqual(state["board"]["usage"]["c4"], {"status": "정상", "local": 1, "subscription": 1})
		self.assertEqual(state["board"]["usage"]["limit"]["daily_limit_calls"], 20)
		self.assertEqual(state["jobs"][2]["status"], "확인 필요")
		self.assertEqual(state["jobs"][2]["last_exit_code"], 5)
		self.assertEqual(before, self.snapshot())
		payload = json.dumps(state, ensure_ascii=False)
		for private in (str(self.user), "acme-private-material", "/acme/xyz", "summary", "request"):
			self.assertNotIn(private, payload)
		self.opener.open.assert_called_once_with("http://127.0.0.1:11434/api/ps", timeout=1)
		commands = [json.loads(line) for line in (self.root / "commands.jsonl").read_text().splitlines()]
		self.assertTrue(all((line[0] == "launchctl" and line[1] == "print") or line[0] == "pgrep" for line in commands))

	def test_lock_not_just_file_presence_and_plan_selects_reviewer(self):
		self.assertEqual(self.people(self.state())["codex"]["status"], "대기")
		self.run_record("2026-10-23-006", "작업 중", "review", "acme-agy")
		lock = self.hold()
		people = self.people(self.state())
		self.assertEqual(people["agy"]["status"], "검토 중")
		self.assertEqual(people["agy"]["room"], "검토실")
		lock.close()
		with (self.hub / "runs/2026-10-23-006/.lock").open("rb") as source:
			import fcntl
			fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
		self.run_record("2026-10-23-006", "작업 중", "implement", "acme-claude")
		self.hold()
		self.assertEqual(self.people(self.state())["claude"]["status"], "구현 중")

	def test_local_model_reserved_call_and_unavailable_ledger_are_distinct(self):
		self.loaded_ollama()
		person = self.people(self.state())["gemma"]
		self.assertEqual((person["status"], person["room"]), ("집중 중", "집중 업무 존"))
		self.assertIn("p01 · 2/3건째", person["task"])
		self.conn.execute("update c4_calls set state='done'")
		self.conn.commit()
		self.assertEqual(self.people(self.state())["gemma"]["status"], "대기 중")
		self.conn.execute("drop table c4_calls")
		self.conn.commit()
		state = self.state()
		self.assertEqual(self.people(state)["gemma"]["status"], "확인 불가")
		self.assertEqual(state["board"]["usage"]["plans"]["status"], "정상")
		self.assertEqual(state["approvals"]["c4"]["status"], "정상")

	def test_malformed_ollama_response_is_not_reported_as_working(self):
		import io
		self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b'[]')
		self.assertEqual(self.people(self.state())["gemma"]["status"], "확인 불가")

	def test_engine_uses_local_agent_id_when_agent_positions_change(self):
		for local, expected in (({"id": "gemma", "status": "대기 중", "models": ["acme"]}, "가동 중"),
				({"id": "gemma", "status": "확인 불가"}, "확인 불가"), ({"id": "gemma", "status": "꺼짐"}, "꺼짐")):
			with self.subTest(status=expected), \
					mock.patch.object(agents, "ollama", return_value={"id": "blog", "status": "일하는 중"}), \
					mock.patch.object(agents, "blog", return_value=local):
				state = self.state()
				self.assertEqual(next(engine for engine in state["engines"] if engine["id"] == "ollama")["status"], expected)
				self.assertIn("dashboard", self.people(state)["blog"])

	def test_missing_and_broken_sources_do_not_hide_healthy_sections(self):
		for name in ("plans.toml", "decisions.toml", "ledger.db"):
			with self.subTest(name=name):
				path = self.hub / name
				before = path.read_bytes()
				path.unlink()
				state = self.state()
				self.assertEqual(self.people(state)["blog"]["status"], "일하는 중")
				self.assertEqual(state["jobs"][0]["status"], "대기 중")
				self.assertFalse(path.exists())
				path.write_bytes(b"[xyz")
				state = self.state()
				self.assertTrue(state["activity"])
				path.write_bytes(before)

	def test_corrupt_run_and_events_only_mark_affected_records(self):
		self.hold()
		folder = self.hub / "runs/2026-10-23-006"
		(folder / "events.jsonl").write_text("[]\n")
		state = self.state()
		self.assertEqual(self.people(state)["codex"]["status"], "확인 불가")
		self.assertEqual(len(state["approvals"]["runs"]["items"]), 1)
		(folder / "run.json").write_text("[]")
		state = self.state()
		self.assertEqual(state["approvals"]["runs"]["status"], "확인 불가")
		self.assertEqual(state["approvals"]["c4"]["status"], "정상")

	def test_old_runs_do_not_hide_current_agent_states(self):
		self.run_record("2026-10-22-002", "예산 소진")
		broken = self.run_record("2026-10-22-001", "완료")
		(broken / "run.json").write_text("[]")
		people = self.people(self.state())
		self.assertEqual(people["codex"]["status"], "대기")
		self.assertEqual(people["agy"]["status"], "대기 중")

	def test_gemini_without_hook_does_not_infer_work_from_transcripts(self):
		original = Path.open
		def guard(path, *args, **kwargs):
			if "chats" in path.parts:
				self.fail("대화 내용을 읽었습니다")
			return original(path, *args, **kwargs)
		with mock.patch.object(Path, "open", guard):
			self.assertEqual(self.people(self.state())["gemini"]["status"], "대기")
		path = self.user / ".gemini/tmp/xyz/chats/acme.json"
		os.utime(path, (self.now.timestamp() - 601,) * 2)
		self.assertEqual(self.people(self.state())["gemini"]["status"], "대기")
		(path.parent / "🔐xyz.json").write_text("읽기 금지 합성 자료")
		self.assertEqual(self.people(self.state())["gemini"]["status"], "대기")

	def test_decisions_keep_good_items_and_only_three_future_schedules(self):
		path = self.hub / "decisions.toml"
		with path.open("a") as out:
			out.write('\n[[item]]\nid="broken"\nstatus="open"\n')
			for n in range(-1, 5):
				due = (self.now.date() + timedelta(days=n)).isoformat()
				out.write(f'\n[[item]]\nid="s{n+2}"\nkind="schedule"\nstatus="open"\ntitle="xyz 일정"\nreply=""\ndue="{due}"\n')
		state = self.state()
		self.assertEqual(state["approvals"]["decisions"]["status"], "확인 불가")
		self.assertEqual(len(state["approvals"]["decisions"]["items"]), 1)
		items = state["board"]["schedule"]["decisions"]["items"]
		self.assertEqual([row["due"] for row in items], [(self.now.date() + timedelta(days=n)).isoformat() for n in range(3)])

	def test_sql_never_selects_body_columns_or_writes(self):
		class GuardedStore(Store):
			def __init__(inner, *args, **kwargs):
				super().__init__(*args, **kwargs)
				def allow(action, arg1, arg2, *rest):
					if action == sqlite3.SQLITE_READ and arg2 in ("data", "report", "result", "note", "summary"):
						return sqlite3.SQLITE_DENY
					if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
						return sqlite3.SQLITE_DENY
					return sqlite3.SQLITE_OK
				inner.conn.set_authorizer(allow)
		with mock.patch("office.ledger.Store", GuardedStore):
			state = self.state()
		self.assertEqual(state["board"]["progress"]["status"], "정상")
		self.assertEqual(state["approvals"]["c4"]["status"], "정상")

	def test_wal_reader_does_not_create_shm_in_source(self):
		self.conn.execute("pragma journal_mode=wal")
		self.conn.execute("update calls set n=4")
		self.conn.commit()
		copy = self.root / "xyz"
		copy.mkdir()
		for name in ("ledger.db", "ledger.db-wal"):
			shutil.copyfile(self.hub / name, copy / name)
		with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(copy)}):
			state = self.state()
		self.assertEqual(state["board"]["usage"]["plans"]["items"][0]["count"], 4)
		self.assertFalse((copy / "ledger.db-shm").exists())

	def test_wal_database_without_sidecars_stays_unchanged(self):
		self.conn.execute("pragma journal_mode=wal")
		self.conn.close()
		before = self.snapshot()
		self.assertEqual(self.state()["board"]["progress"]["status"], "정상")
		self.assertEqual(before, self.snapshot())

	def test_only_twenty_local_timestamped_activities_and_no_raw_messages(self):
		path = self.hub / "runs/2026-10-23-006/events.jsonl"
		with path.open("w") as out:
			for n in range(25):
				out.write(json.dumps({"at": (self.now-timedelta(minutes=n)).isoformat(), "event": "implemented", "summary": "acme-private-material"})+'\n')
		activity = self.state()["activity"]
		self.assertEqual(len(activity), 20)
		self.assertEqual([row["at"] for row in activity], sorted((row["at"] for row in activity), reverse=True))
		self.assertTrue(all(row["at"].endswith(self.now.strftime('%z')[:3]+':'+self.now.strftime('%z')[3:]) for row in activity))

	def test_links_and_symlinks_cannot_expose_arbitrary_files(self):
		path = self.hub / "office.toml"
		for url in ("javascript:xyz", "file:///acme/xyz", "http://acme:xyz@127.0.0.1:9000", "http://127.0.0.1:9000/?token=xyz"):
			path.write_text('[blog]\ndashboard_url="'+url+'"\n')
			self.assertIsNone(self.people(self.state())["blog"]["dashboard"]["url"])
		path.unlink()
		path.symlink_to(self.hub / "plans.toml")
		self.assertEqual(self.people(self.state())["blog"]["dashboard"]["status"], "확인 불가")
		self.assertIsNone(agents.NoRedirect().redirect_request(None, None, 302, "", {}, "http://acme.invalid"))
