"""기존 쓰기 창구로 합성 TODO의 HTTP 보안 경계를 확인한다."""

import json
from types import SimpleNamespace
from unittest import mock

import test_office_actions_http as action_http
import test_office_server as server_tests
from office import actions, server
from test_office import OfficeCase
from test_office_tasks import change_payload, create_payload


class OfficeTasksHTTPTest(OfficeCase):
	request = action_http.OfficeActionHTTPTest.request
	raw_request = server_tests.OfficeServerTest.raw_request

	def setUp(self):
		super().setUp()
		self.httpd = SimpleNamespace(server_port=8790, office_token=server.secrets.token_urlsafe(32))

	def test_lifecycle_via_json_actions_and_static_script(self):
		with mock.patch.object(actions.subprocess, "run") as command:
			code, _, body = self.request(create_payload())
			self.assertEqual(code, 200)
			row = json.loads(body)["task"]
			for action in ("task_update", "task_complete", "task_reopen"):
				code, _, body = self.request(change_payload(row, action))
				self.assertEqual(code, 200)
				row = json.loads(body)["task"]
			command.assert_not_called()
		code, headers, body = self.request(create_payload(), method="GET", path="/tasks.js")
		self.assertEqual(code, 200)
		self.assertIn("text/javascript", headers["Content-Type"])
		self.assertEqual(body, (server.STATIC / "tasks.js").read_bytes())

	def test_every_task_action_reuses_host_origin_session_and_size_guards(self):
		row = {"id": create_payload()["id"], "kind": "task", "title": "합성 제목", "memo": "", "version": 1}
		payloads = [create_payload(), *[change_payload(row, action) for action in actions.tasks.ACTIONS[1:]]]
		with mock.patch.object(actions.tasks, "execute") as execute:
			for payload in payloads:
				for expected, headers in ((403, {"Host": "acme.invalid"}), (403, {"Origin": None}),
						(403, {"Origin": "http://acme.invalid"}), (403, {"X-Office-Token": None}),
						(403, {"X-Office-Token": "xyz"}), (415, {"Content-Type": "text/plain"})):
					with self.subTest(action=payload["action"], headers=headers):
						self.assertEqual(self.request(payload, headers=headers)[0], expected)
				self.assertEqual(self.request(payload, body=b" " * 16385)[0], 413)
				self.assertEqual(self.request({**payload, "extra": "xyz"})[0], 400)
			execute.assert_not_called()
		self.assertFalse((self.hub / "office-tasks").exists())
