"""부서 설정의 경계와 오류 메시지 비공개성을 검사한다."""

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency import departments
from collector.config import ConfigError


class DepartmentsTest(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.hub = Path(temporary.name).resolve()
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		(self.hub / "plans.toml").write_text('''[[plans]]
id = "xyz"
tool = "codex"
owner = "personal"
allow = ["personal", "career"]
[[plans]]
id = "local"
tool = "ollama"
model = "xyz"
owner = "personal"
allow = ["personal", "company"]
''', encoding="utf-8")
		self.row = {"id": "xyz", "name": "합성 부서", "scope": "합성 자료만",
			"repos": ["ai-hub"], "allow_plans": ["xyz"], "playbook": "xyz",
			"skills": [], "review_skill": "", "outward": [], "required_questions": [],
			"budget": {"calls": 10, "minutes": 30}}

	def scalar(self, value):
		if isinstance(value, bool):
			return str(value).lower()
		if isinstance(value, str):
			import json
			return json.dumps(value, ensure_ascii=False)
		if isinstance(value, list):
			return "[" + ", ".join(self.scalar(item) for item in value) + "]"
		if isinstance(value, dict):
			return "{ " + ", ".join(key + " = " + self.scalar(item) for key, item in value.items()) + " }"
		return str(value)

	def write(self, *rows):
		text = "\n".join("[[department]]\n" + "\n".join(key + " = " + self.scalar(value)
			for key, value in row.items()) for row in rows)
		(self.hub / "departments.toml").write_text(text, encoding="utf-8")

	def invalid(self, row, label="xyz"):
		self.write(row)
		with self.assertRaises(ConfigError) as caught:
			departments.load_departments()
		message = str(caught.exception)
		self.assertTrue(message.startswith("departments.toml: " + label + ": "), message)
		self.assertNotIn(str(self.hub), message)
		self.assertNotIn("/xyz/private", message)
		return message

	def test_missing_empty_and_frozen_department_with_defaults(self):
		self.assertEqual(departments.load_departments(), {})
		self.write()
		self.assertEqual(departments.load_departments(), {})
		row = dict(self.row)
		for key in ("skills", "review_skill", "outward", "required_questions"):
			row.pop(key)
		self.write(row)
		found = departments.load_departments()
		self.assertEqual(list(found), ["xyz"])
		department = found["xyz"]
		self.assertTrue(department.external_ai)
		self.assertEqual(department.skills, ())
		self.assertEqual(department.review_skill, "")
		self.assertEqual(department.repos, ("ai-hub",))
		self.assertEqual(department.budget, {"calls": 10, "minutes": 30})
		with self.assertRaises(dataclasses.FrozenInstanceError):
			department.name = "변경"

	def test_id_validation_and_duplicates(self):
		for invalid in ("", "Upper", "a_1", "a" * 32, "/xyz/private", 12):
			with self.subTest(invalid_type=type(invalid).__name__):
				self.invalid({**self.row, "id": invalid}, "1")
		self.write(self.row, self.row)
		with self.assertRaisesRegex(ConfigError, "departments.toml: xyz: .*중복"):
			departments.load_departments()

	def test_unknown_keys_and_required_fields(self):
		self.invalid({**self.row, "unknown": "/xyz/private"})
		for key in ("id", "name", "scope", "repos", "allow_plans", "playbook", "budget"):
			with self.subTest(key=key):
				row = dict(self.row)
				row.pop(key)
				self.invalid(row, "1" if key == "id" else "xyz")
		self.invalid({**self.row, "budget": {"calls": 1, "minutes": 2, "extra": 3}})

	def test_every_field_type_length_range_and_reference(self):
		cases = {
			"name": ["", " " * 3, "가" * 41, 1],
			"scope": ["", "가" * 301, False],
			"repos": [[], "ai-hub", ["missing"], [1]],
			"allow_plans": [[], "xyz", ["missing"], [False]],
			"playbook": ["", "Upper", "../xyz", "a" * 65, 1],
			"skills": ["xyz", ["../xyz"], [1]],
			"review_skill": ["../xyz", 1],
			"outward": ["push", ["delete"], [1]],
			"required_questions": ["질문", [""], ["가" * 201], ["질문"] * 11, [1]],
			"external_ai": [0, "false", []],
			"budget": [[], {"calls": 0, "minutes": 1}, {"calls": 201, "minutes": 1},
				{"calls": True, "minutes": 1}, {"calls": 1.5, "minutes": 1},
				{"calls": 1, "minutes": 0}, {"calls": 1, "minutes": 1441},
				{"calls": 1, "minutes": False}, {"calls": 1}, {"minutes": 1}]
		}
		for key, values in cases.items():
			for value in values:
				with self.subTest(key=key, value_type=type(value).__name__):
					self.invalid({**self.row, key: value})

	def test_valid_boundaries_outward_subset_and_local_only_policy(self):
		for calls, minutes in ((1, 1), (200, 1440)):
			self.write({**self.row, "id": "a" + "-1" * 15, "name": "가" * 40, "scope": "가" * 300,
				"external_ai": False, "allow_plans": ["local"], "playbook": "a" * 64,
				"skills": ["xyz"], "review_skill": "xyz", "outward": ["merge", "push", "restart", "publish"],
				"required_questions": ["가" * 200] * 10, "budget": {"calls": calls, "minutes": minutes}})
			self.assertEqual(len(departments.load_departments()), 1)
		self.invalid({**self.row, "external_ai": False})
		self.invalid({**self.row, "external_ai": False, "allow_plans": ["local", "xyz"]})

	def test_document_without_repos_needs_no_target_and_code_remains_default(self):
		self.write(self.row)
		self.assertEqual(departments.load_departments()["xyz"].deliverable, "code")
		for remove_repos in (False, True):
			row = {**self.row, "deliverable": "document", "repos": []}
			if remove_repos:
				row.pop("repos")
			self.write(row)
			with mock.patch.object(departments, "targets", side_effect=AssertionError("저장소 조회 금지")):
				loaded = departments.load_departments()["xyz"]
			self.assertEqual((loaded.deliverable, loaded.repos), ("document", ()))
		for value in ("", "other", 1, False, []):
			self.invalid({**self.row, "deliverable": value})
		self.invalid({**self.row, "deliverable": "code", "repos": []})
		self.invalid({**self.row, "deliverable": "document", "repos": ["missing"]})

	def test_prepare_inputs_and_schedule_validation(self):
		valid = {**self.row, "deliverable": "document", "prepare": [["python3", "-c", "print(1)"]],
			"prepare_timeout": 3600, "inputs": ["jobs-allowed"],
			"schedule": [{"repo": "ai-hub", "every_days": 1, "title": "점검", "request": "합성 요청"}]}
		self.write(valid)
		loaded = departments.load_departments()["xyz"]
		self.assertEqual((loaded.prepare, loaded.prepare_timeout, loaded.inputs, loaded.schedule[0]),
			((("python3", "-c", "print(1)"),), 3600, ("jobs-allowed",), valid["schedule"][0]))
		for key, values in {
			"prepare": ["echo", ["echo"], [[]], [[""]], [[1]], [["a", 1]], [["a", "x\x00"]]],
			"prepare_timeout": [0, 3601, True, 1.5, "1"],
			"inputs": ["jobs-allowed", ["unknown"], [["jobs-allowed"]], ["jobs-allowed"] * 2],
			"schedule": ["bad", [1], [{}], [{**valid["schedule"][0], "repo": "missing"}],
				[{**valid["schedule"][0], "every_days": 0}], [{**valid["schedule"][0], "every_days": True}],
				[{**valid["schedule"][0], "title": ""}], [{**valid["schedule"][0], "request": ""}],
				[{**valid["schedule"][0], "extra": "x"}]]}.items():
			for value in values:
				with self.subTest(key=key, value_type=type(value).__name__):
					self.invalid({**valid, key: value})
		self.write({**valid, "external_ai": False, "allow_plans": ["local"]})
		self.assertEqual(departments.load_departments()["xyz"].prepare, loaded.prepare)

	def test_bad_document_and_dependency_errors_are_sanitized(self):
		path = self.hub / "departments.toml"
		for text in ('other = "/xyz/private"', 'department = "bad"', 'department = [1]',
				'[[department]\nsecret = "/xyz/private"'):
			path.write_text(text, encoding="utf-8")
			with self.assertRaises(ConfigError) as caught:
				departments.load_departments()
			self.assertNotIn("/xyz/private", str(caught.exception))
			self.assertTrue(str(caught.exception).startswith("departments.toml: 1:"))
		path.write_bytes(b"\xff")
		with self.assertRaises(ConfigError):
			departments.load_departments()
		self.write(self.row)
		for dependency in ("load_plans", "targets"):
			with mock.patch.object(departments, dependency, side_effect=ConfigError("/xyz/private")):
				with self.assertRaises(ConfigError) as caught:
					departments.load_departments()
				self.assertNotIn("/xyz/private", str(caught.exception))
