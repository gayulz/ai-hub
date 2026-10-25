"""실제 홈·볼트·소스 없이 숏폼 접수와 원본 보호 경계를 검사한다."""

import copy
import fcntl
import json
import os
import stat
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from office import actions, shorts
from office.common import action_id
from office.shorts_steps import STEPS

SID = "s-" + "a" * 32


class ShortsCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.user = self.root / "p01"
		self.hub = self.user / ".ai-hub"
		(self.hub / "redaction").mkdir(parents=True)
		(self.hub / "redaction/dictionary.json").write_text(json.dumps({"entries": [
			{"value": "acme", "kind": "회사명", "token": "company01"},
			{"value": "xyz", "kind": "서비스명", "token": "service01"}]}), encoding="utf-8")
		self.vault = self.user / "p01"
		self.vault.mkdir()
		(self.hub / "sources.toml").write_text('vault = ' + json.dumps(str(self.vault)), encoding="utf-8")
		self.source, self.save = self.user / "Pictures", self.user / "Movies"
		self.source.mkdir()
		self.save.mkdir()
		self.payload = {"action": "shorts_intake", "id": SID, "story": "합성 이야기 원문입니다. 다음 장면입니다.",
			"source": {"kind": "preset", "id": "pictures"}, "save": {"kind": "path", "value": str(self.save / "p01")},
			"conditions": [{"id": step["id"], "value": "합성 조건 답변 " + str(i)} for i, step in enumerate(STEPS)]}
		for patch in (mock.patch.dict(os.environ, {"HOME": str(self.user), "AI_HUB_HOME": str(self.hub)}),
				mock.patch("vaultfilter.public.getpass.getuser", return_value="p01"),
				mock.patch("socket.create_connection", side_effect=AssertionError("실제 연결 금지")),
				mock.patch("orchestrator.providers.call", side_effect=AssertionError("AI 호출 금지")),
				mock.patch("orchestrator.providers.chat_call", side_effect=AssertionError("AI 호출 금지")),
				mock.patch("subprocess.Popen", side_effect=AssertionError("외부 명령 금지"))):
			patch.start()
			self.addCleanup(patch.stop)


class OfficeShortsTest(ShortsCase):
	def case_alias(self, folder):
		alias = folder.with_name(folder.name.upper())
		if not alias.exists() or not alias.samefile(folder):
			self.skipTest("대소문자를 구분하는 파일시스템입니다.")
		return alias

	def test_exact_keys_identifier_conditions_and_lengths(self):
		bad = [None, [], {}, {**self.payload, "extra": "p01"}]
		bad += [{k: v for k, v in self.payload.items() if k != key} for key in self.payload]
		bad += [{**self.payload, "id": value} for value in (None, 1, [], SID.upper(), SID + "/", "../p01", "s-" + "a" * 31)]
		bad += [{**self.payload, "story": value} for value in (None, [], "", " ", "가" * 8001, "합성\x01", "\ud800")]
		conditions = self.payload["conditions"]
		bad += [{**self.payload, "conditions": value} for value in (None, {}, [], conditions[:-1], conditions + conditions[:1],
			[conditions[0]] * len(STEPS), [{"id": "p01", "value": "합성"}] + conditions[1:],
			[{**conditions[0], "extra": "p01"}] + conditions[1:], [{"id": [], "value": "합성"}] + conditions[1:])]
		for value in ("", " ", "가" * 301, None, "합성\x7f", "\ud800"):
			bad.append({**self.payload, "conditions": [{"id": conditions[0]["id"], "value": value}] + conditions[1:]})
		for payload in bad:
			with self.subTest(payload=str(payload)[:70]):
				self.assertEqual(actions.apply(payload)[0], 400)
		self.assertEqual(action_id("shorts_intake", SID), SID)
		self.assertFalse((self.hub / "shorts").exists())
		for key, value in (("source", {"kind": "preset", "id": "p01"}), ("save", {"kind": "preset", "id": "pictures"}),
				("save", {"kind": "preset", "id": "documents"}),
				("source", {"kind": "path", "value": str(self.source), "extra": "p01"}), ("save", {"kind": "p01", "value": str(self.save)})):
			self.assertEqual(actions.apply({**self.payload, key: value})[0], 400)

	def test_story_8000_code_point_limit_preserves_original_and_state_reads_same_limit(self):
		family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
		combined = "e\u0301\u0302\u0303"
		for number, story in enumerate(("가" * 2001, "가" * 8000, family * 1000 + "가" * 1000,
				family * 1142, family * 1142 + "가" * 6, combined * 2000, " \t\u1100\u1161\n합성 \n")):
			with self.subTest(number=number):
				identity = "s-" + f"{number:032x}"
				code, result = actions.apply({**self.payload, "id": identity, "story": story})
				self.assertEqual(code, 200, result)
				path = self.hub / "shorts" / identity / "intake.json"
				self.assertEqual(json.loads(path.read_text())["story"], story)
				group = shorts.read_state(self.hub)
				self.assertEqual(group["status"], "정상")
				self.assertIn(identity, [row["id"] for row in group["items"]])
		self.assertEqual(shorts.STORY_MAX, 8000)
		for story in ("가" * 8001, family * 1143, family * 1500, family * 2000, combined * 2000 + "\u0304"):
			with self.subTest(code_points=len(story)):
				code, result = actions.apply({**self.payload, "story": story})
				self.assertEqual((code, result["field"]), (400, "story"))
				self.assertFalse((self.hub / "shorts" / SID).exists())
		path.write_text(json.dumps({**json.loads(path.read_text()), "story": "가" * 8001}), encoding="utf-8")
		group = shorts.read_state(self.hub)
		self.assertEqual(group["status"], "확인 불가")
		self.assertNotIn(identity, [row["id"] for row in group["items"]])

	def test_sensitive_story_condition_and_path_refused_without_ai_or_raw_errors(self):
		values = ("acme 정보", "xyz 정보", "토큰 " + "ghp_" + "a" * 36, "p01" + "@" + "acme.invalid",
			"/" + "Users/p01/p01", "/" + "home/p01/p01", "~" + "/p01", "C:" + "\\Users\\p01\\p01", str(self.user / "p01"))
		for value in values:
			for field in ("story", "purpose"):
				payload = copy.deepcopy(self.payload)
				if field == "story":
					payload["story"] = value
				else:
					payload["conditions"][0]["value"] = value
				code, result = actions.apply(payload)
				self.assertEqual(code, 400, result)
				self.assertEqual(result["field"], field)
				self.assertNotIn(value, json.dumps(result, ensure_ascii=False))
		for part in ("acme", "xyz", "ghp_" + "a" * 36):
			path = self.user / part
			path.mkdir()
			self.assertEqual(actions.apply({**self.payload, "source": {"kind": "path", "value": str(path)}})[0], 400)
		self.assertFalse((self.hub / "shorts").exists())
		(self.hub / "redaction/dictionary.json").unlink()
		self.assertEqual(actions.apply(self.payload)[0], 400)

	def test_all_path_boundaries_and_nonexistent_save_parent(self):
		protected = self.user / ("\U0001f510" + "p01")
		protected.mkdir()
		git = self.user / "Documents"
		(git / "p01").mkdir(parents=True)
		(git / ".git").write_text("p01")
		file = self.user / "p01.txt"
		file.write_text("합성 원본")
		link = self.user / "Desktop"
		link.symlink_to(self.source, target_is_directory=True)
		for path, reason in ((self.root, "홈"), (protected, "보호"), (self.hub, "작업 기록"), (self.vault, "볼트"),
				(git / "p01", "Git"), (file, "폴더"), (link, "링크"), (self.source / "p01", "존재")):
			code, result = actions.apply({**self.payload, "source": {"kind": "path", "value": str(path)}})
			self.assertEqual(code, 400, result)
			self.assertIn(reason, result["message"])
			self.assertNotIn(str(path), result["message"])
		for path in (self.save / "p01/p01", file, self.source, self.source / "p01", self.user):
			code, result = actions.apply({**self.payload, "save": {"kind": "path", "value": str(path)}})
			self.assertEqual(code, 400, result)
		# 소스 바깥의 상위 폴더도 저장 목적지로 허용하지 않는다.
		(self.source / "p01").mkdir()
		payload = {**self.payload, "source": {"kind": "path", "value": str(self.source / "p01")},
			"save": {"kind": "path", "value": str(self.source)}}
		self.assertIn("원본 보호", actions.apply(payload)[1]["message"])
		(self.hub / "sources.toml").unlink()
		self.assertIn("볼트 위치", actions.apply(self.payload)[1]["message"])

	def test_realpath_aliases_cannot_escape_into_excluded_folders(self):
		alias = self.user / "Desktop"
		for destination in (self.root, self.hub, self.vault):
			alias.symlink_to(destination, target_is_directory=True)
			payload = {**self.payload, "source": {"kind": "path", "value": str(alias / "p01/..")}}
			self.assertEqual(actions.apply(payload)[0], 400)
			alias.unlink()

	def test_case_aliases_cannot_enter_excluded_folders(self):
		number = 0
		for folder, reason in ((self.hub, "작업 기록"), (self.vault, "볼트")):
			alias = self.case_alias(folder)
			(folder / "p01").mkdir()
			for path in (alias, alias / "p01", alias / "p01/p01"):
				for field in ("source", "save"):
					with self.subTest(folder=folder.name, field=field, path=path.name):
						number += 1
						payload = {**self.payload, "id": "s-" + f"{number:032x}",
							field: {"kind": "path", "value": str(path)}}
						code, result = actions.apply(payload)
						self.assertEqual(code, 400, result)
						self.assertEqual(result["field"], field)
						self.assertIn(reason, result["message"])
						self.assertNotIn(str(path), result["message"])
		self.assertFalse((self.hub / "shorts").exists())

	def test_case_aliases_cannot_overlap_source_and_save(self):
		alias = self.case_alias(self.source)
		(self.source / "p01").mkdir()
		for number, (source, save) in enumerate(((self.source, alias), (self.source, alias / "p01"),
				(self.source, alias / "p01/p01"), (self.source / "p01", alias),
				(alias, self.source), (alias, self.source / "p01"), (alias / "p01", self.source))):
			with self.subTest(source=source.name, save=save.name):
				payload = {**self.payload, "id": "s-" + f"{number:032x}", "source": {"kind": "path", "value": str(source)},
					"save": {"kind": "path", "value": str(save)}}
				code, result = actions.apply(payload)
				self.assertEqual(code, 400, result)
				self.assertEqual(result["field"], "save")
				self.assertIn("원본 보호", result["message"])
		self.assertFalse((self.hub / "shorts").exists())

	def test_home_expansion_nfc_presets_and_normalized_saved_paths(self):
		folder = self.user / "합성"
		folder.mkdir()
		payload = {**self.payload, "source": {"kind": "path", "value": "~/" + unicodedata.normalize("NFD", "합성")},
			"save": {"kind": "path", "value": "~/Movies/p01"}}
		self.assertEqual(actions.apply(payload)[0], 200)
		row = json.loads((self.hub / "shorts" / SID / "intake.json").read_text())
		self.assertEqual(row["source_dir"], str(folder))
		self.assertEqual(row["save_dir"], str(self.save / "p01"))
		self.assertFalse((self.save / "p01").exists())
		data = shorts.steps()
		self.assertEqual([p["id"] for p in data["source_presets"]], ["pictures", "movies"])
		self.assertEqual(data["save_presets"], [{"id": "movies", "label": "영상"}])
		icloud = self.user / shorts.PRESETS["icloud"][1]
		icloud.mkdir(parents=True)
		self.assertIn("icloud", [p["id"] for p in shorts.steps()["source_presets"]])
		(self.hub / "sources.toml").write_text('vault = ' + json.dumps(unicodedata.normalize("NFD", str(folder))), encoding="utf-8")
		self.assertIn("볼트", actions.apply({**payload, "id": "s-" + "b" * 32})[1]["message"])

	def test_success_private_atomic_record_safe_audit_and_duplicate_unchanged(self):
		original = self.source / "p01"
		original.write_text("합성 원본")
		before = (original.read_bytes(), original.stat().st_mtime_ns, self.source.stat().st_mtime_ns, self.save.stat().st_mtime_ns)
		path = self.hub / "shorts" / SID / "intake.json"
		rename, stages = os.rename, []
		def check_rename(src, dst, **kwargs):
			self.assertFalse(path.exists())
			if dst == SID:
				stage = self.hub / "shorts" / src
				self.assertEqual(json.loads((stage / "intake.json").read_text())["id"], SID)
				stages.append(src)
			return rename(src, dst, **kwargs)
		with mock.patch.object(shorts.os, "rename", side_effect=check_rename):
			code, response = actions.apply(self.payload)
		self.assertEqual(code, 200, response)
		self.assertEqual(set(response), {"ok", "message", "draft"})
		self.assertEqual(set(response["draft"]), {"id", "status", "created"})
		self.assertEqual(response["message"], shorts.CONFIRMED)
		self.assertEqual(len(stages), 1)
		data = json.loads(path.read_text())
		self.assertEqual(set(data), {"version", "id", "created", "status", "story", "source_dir", "save_dir", "conditions", "confirmed_by"})
		self.assertEqual((data["version"], data["confirmed_by"], data["status"]), (1, "office", "intake_confirmed"))
		self.assertEqual(data["conditions"], [{**row, "label": step["label"]} for row, step in zip(self.payload["conditions"], STEPS)])
		for folder in (path.parent, path.parent.parent):
			self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
		self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
		saved = (path.read_bytes(), path.stat().st_mtime_ns)
		self.assertEqual(actions.apply(self.payload), (409, {"ok": False, "message": "이미 접수한 요청입니다", "field": "summary"}))
		self.assertEqual(saved, (path.read_bytes(), path.stat().st_mtime_ns))
		self.assertEqual(before, (original.read_bytes(), original.stat().st_mtime_ns, self.source.stat().st_mtime_ns, self.save.stat().st_mtime_ns))
		audit = (self.hub / "logs/office-actions.jsonl").read_text()
		for raw in (self.payload["story"], str(self.source), str(self.save), *(r["value"] for r in self.payload["conditions"])):
			self.assertNotIn(raw, audit)
		for row in map(json.loads, audit.splitlines()):
			self.assertEqual(set(row), {"at", "action", "id", "exit_code", "seconds", "ok", "message"})
		self.assertEqual(list(path.parent.iterdir()), [path])

	def test_failed_atomic_write_cleans_temporary_files_and_can_retry(self):
		with mock.patch.object(shorts.os, "rename", side_effect=OSError("합성 오류")):
			self.assertEqual(actions.apply(self.payload)[0], 409)
		self.assertEqual(list((self.hub / "shorts").iterdir()), [])
		self.assertEqual(actions.apply(self.payload)[0], 200)

	def test_publication_race_preserves_existing_record(self):
		rename = os.rename
		path = self.hub / "shorts" / SID / "intake.json"
		def race(src, dst, **kwargs):
			if dst == SID:
				path.parent.mkdir()
				path.write_text("합성 기존 기록", encoding="utf-8")
			return rename(src, dst, **kwargs)
		with mock.patch.object(shorts.os, "rename", side_effect=race):
			code, result = actions.apply(self.payload)
		self.assertEqual((code, result["message"]), (409, "이미 접수한 요청입니다"))
		self.assertEqual(path.read_text(), "합성 기존 기록")
		self.assertEqual(list(path.parent.parent.iterdir()), [path.parent])

	def test_state_ignores_hidden_temporary_entries_but_keeps_unknown_entries_unavailable(self):
		self.assertEqual(actions.apply(self.payload)[0], 200)
		parent = self.hub / "shorts"
		expected = shorts.read_state(self.hub)
		(parent / ".intake-writing").mkdir()
		(parent / ".intake-writing/intake.tmp").write_text("{", encoding="utf-8")
		(parent / ".intake-abandoned").mkdir()
		(parent / ".intake-abandoned/intake.json").write_text("{", encoding="utf-8")
		(parent / ".DS_Store").write_bytes(b"p01")
		(parent / ".intake-linked").symlink_to(self.save, target_is_directory=True)
		before = {p: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in parent.rglob("*")}
		self.assertEqual(shorts.read_state(self.hub), expected)
		self.assertEqual(before, {p: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in parent.rglob("*")})
		for name, directory in (("p01", True), ("p01.txt", False)):
			path = parent / name
			path.mkdir() if directory else path.write_text("p01", encoding="utf-8")
			group = shorts.read_state(self.hub)
			self.assertEqual(group["status"], "확인 불가")
			self.assertEqual(group["items"], expected["items"])
			path.rmdir() if directory else path.unlink()

	def test_state_skips_wrong_schema_and_hard_links_without_modifying_files(self):
		self.assertEqual(actions.apply(self.payload)[0], 200)
		path = self.hub / "shorts" / SID / "intake.json"
		row = json.loads(path.read_text())
		for change in ({"version": True}, {"created": "p01"}, {"created": "2026-10-25"}, {"story": ""},
				{"status": "p01"}, {"source_dir": "p01"}, {"confirmed_by": "p01"}, {"conditions": []}, {"extra": "p01"}):
			path.write_text(json.dumps({**row, **change}), encoding="utf-8")
			before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
			group = shorts.read_state(self.hub)
			self.assertEqual((group["status"], group["items"]), ("확인 불가", []))
			self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode))
		path.write_text(json.dumps(row), encoding="utf-8")
		other = path.parent.parent / ("s-" + "b" * 32)
		other.mkdir()
		os.link(path, other / "intake.json")
		self.assertEqual(shorts.read_state(self.hub)["items"], [])
		self.assertEqual(shorts.read_state(self.hub)["status"], "확인 불가")

	def test_symlinks_existing_ids_and_global_lock_never_overwrite(self):
		parent = self.hub / "shorts"
		parent.symlink_to(self.save, target_is_directory=True)
		self.assertEqual(actions.apply(self.payload)[0], 409)
		self.assertEqual(list(self.save.iterdir()), [])
		parent.unlink()
		parent.mkdir()
		(parent / SID).symlink_to(self.save, target_is_directory=True)
		self.assertEqual(actions.apply(self.payload)[0], 409)
		self.assertEqual(list(self.save.iterdir()), [])
		with actions.private_file(self.hub, "run", "office-action.lock") as lock:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			self.assertEqual(actions.apply(self.payload)[0], 409)
		self.assertEqual(shorts.read_state(self.hub)["status"], "확인 불가")

	def test_read_state_is_masked_limited_read_only_and_skips_corrupt_or_linked_records(self):
		self.assertEqual(shorts.read_state(self.hub), {"status": "정상", "items": []})
		self.assertEqual(actions.apply(self.payload)[0], 200)
		parent = self.hub / "shorts"
		row = json.loads((parent / SID / "intake.json").read_text())
		for i in range(23):
			identity = "s-" + f"{i:032x}"
			folder = parent / identity
			folder.mkdir()
			(folder / "intake.json").write_text(json.dumps({**row, "id": identity,
				"story": "acme xyz " + ("p01" + "@" + "acme.invalid") + " "+ "ghp_" + "a" * 36 + " 합성 " * 20}), encoding="utf-8")
		before = {str(p): p.stat().st_mtime_ns for p in parent.rglob("*")}
		group = shorts.read_state(self.hub)
		self.assertEqual(group["status"], "정상")
		self.assertEqual(len(group["items"]), 20)
		for value in group["items"]:
			self.assertEqual(set(value), {"id", "created", "status", "story_preview"})
			self.assertLessEqual(len(value["story_preview"]), 40)
			self.assertNotRegex(value["story_preview"], "acme|xyz|@|ghp_")
		self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in parent.rglob("*")})
		bad = parent / "p01"
		bad.mkdir()
		(bad / "intake.json").write_text("{")
		linked = parent / ("s-" + "f" * 32)
		linked.mkdir()
		(linked / "intake.json").symlink_to(parent / SID / "intake.json")
		broken = parent / ("s-" + "e" * 32)
		broken.mkdir()
		(broken / "intake.json").write_text("{")
		group = shorts.read_state(self.hub)
		self.assertEqual(group["status"], "확인 불가")
		self.assertEqual(len(group["items"]), 20)
