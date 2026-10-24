"""실제 설정·망 대신 합성 홈과 메모리 전송만 사용하는 클라이언트 검사."""

import io
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from office.telegram_client import Client, NoRedirect, TelegramError, configuration, transport


class FakeTransport:
	def __init__(self):
		self.calls, self.updates = [], []
		self.message_id = 100
		self.error = None
		self.response = None

	def __call__(self, request, *, timeout):
		address = urlsplit(request.full_url)
		assert address.scheme == "https" and address.netloc == "api.telegram.org"
		assert timeout == 35 and request.get_header("Content-type") == "application/json"
		assert request.get_method() == "POST"
		method = address.path.rsplit("/", 1)[-1]
		body = json.loads(request.data)
		self.calls.append((method, body, timeout))
		if self.error:
			raise self.error
		if self.response is not None:
			return self.response
		if method == "sendMessage":
			self.message_id += 1
			result = {"message_id": self.message_id}
		elif method == "editMessageText":
			result = {"message_id": body["message_id"]}
		elif method == "getUpdates":
			result, self.updates = self.updates, []
		else:
			result = True
		return json.dumps({"ok": True, "result": result}).encode()

	def sent(self):
		return [body for method, body, _ in self.calls if method == "sendMessage"]


class TelegramCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name).resolve()
		self.hub = self.root / "hub"
		self.hub.mkdir(mode=0o700)
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(self.hub)})
		patch.start()
		self.addCleanup(patch.stop)
		self.token = "804" + "2001" + ":" + "aB_9-" * 7
		self.chat = str(846000 + 271)
		self.transport = FakeTransport()

	def config(self, **values):
		path = self.hub / "telegram.toml"
		data = {"token": self.token, "chat_id": self.chat, **values}
		path.write_text("\n".join(key + "=" + json.dumps(value) for key, value in data.items()))
		path.chmod(0o600)
		return path


class TelegramClientTest(TelegramCase):
	def test_missing_permissions_links_hardlinks_and_invalid_values_disable(self):
		self.assertEqual(configuration(), (None, "설정 없음"))
		path = self.config()
		for mode in (0o644, 0o400, 0o700):
			path.chmod(mode)
			self.assertEqual(configuration(), (None, "권한 확인 필요"))
		path.chmod(0o600)
		self.assertEqual(configuration()[1], "켜짐")
		outside = self.root / "other.toml"
		path.rename(outside)
		path.symlink_to(outside)
		self.assertEqual(configuration(), (None, "설정 없음"))
		path.unlink()
		os.link(outside, path)
		self.assertEqual(configuration(), (None, "설정 없음"))
		path.unlink()
		for values in ({"token": ""}, {"chat_id": ""}, {"token": 42}, {"chat_id": 42},
				{"token": "abc:def"}, {"token": "123" + ":" + "bad/value"},
				{"chat_id": "1.5"}, {"chat_id": "42 extra"}, {"other": "extra"}):
			with self.subTest(fields=list(values)):
				self.config(**values)
				self.assertEqual(configuration(), (None, "설정 없음"))
		path.write_text("not toml")
		self.assertEqual(configuration(), (None, "설정 없음"))
		client = Client(transport=self.transport)
		with self.assertRaises(TelegramError):
			client.send_message("합성 글")
		self.assertEqual(self.transport.calls, [])

	def test_parent_links_are_refused_without_changing_file_modes(self):
		path = self.config()
		alias = self.root / "alias"
		alias.symlink_to(self.hub, target_is_directory=True)
		before = path.stat().st_mode
		with mock.patch.dict(os.environ, {"AI_HUB_HOME": str(alias)}):
			self.assertEqual(configuration(), (None, "설정 없음"))
		self.assertEqual(path.stat().st_mode, before)

	def test_json_endpoint_limits_keyboard_and_polling(self):
		self.config()
		client = Client(transport=self.transport)
		buttons = [[{"text": "작게", "callback_data": "a|card|1|1"}]]
		client.send_message("가" * 5000, buttons)
		client.edit_message(11, "선택됨: 작게", buttons)
		client.edit_message(11, "선택 초기화")
		client.answer_callback("callback-1", "선택했습니다")
		self.transport.updates = [{"update_id": 5}]
		self.assertEqual(client.get_updates(4), [{"update_id": 5}])
		calls = self.transport.calls
		self.assertEqual(len(calls[0][1]["text"]), 4096)
		self.assertEqual(calls[0][1]["reply_markup"], {"inline_keyboard": buttons})
		self.assertEqual(calls[1][1]["message_id"], 11)
		self.assertEqual(calls[2][1]["reply_markup"], {"inline_keyboard": []})
		self.assertEqual(calls[-1][1], {"offset": 4, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
		with self.assertRaises(TelegramError):
			client.send_message("합성 글", [[{"text": "선택", "callback_data": "가" * 22}]])
		self.assertFalse(client.sensitive("14:30에 진행해 주세요"))
		self.assertEqual(client.clean("14:30에 진행해 주세요"), "14:30에 진행해 주세요")

	def test_provider_exception_body_and_token_url_never_escape(self):
		self.config()
		client = Client(transport=self.transport)
		unsafe = "https://api.telegram.org/bot" + self.token + "/sendMessage"
		errors = [RuntimeError(unsafe), URLError(unsafe), HTTPError(unsafe, 401, unsafe, {}, io.BytesIO(unsafe.encode()))]
		for error in errors:
			self.transport.error = error
			try:
				client.send_message("합성 글")
			except TelegramError as exc:
				formatted = "".join(traceback.format_exception(exc))
				self.assertIsNone(exc.__context__)
				self.assertNotIn(self.token, formatted)
				self.assertNotIn("api.telegram.org", formatted)
				self.assertIn(type(error).__name__, str(exc))
				if isinstance(error, HTTPError):
					self.assertEqual(str(exc), "HTTPError HTTP 401")
					self.assertTrue(error.fp.closed)
			else:
				self.fail("정제된 전송 예외가 필요합니다")
		self.transport.error = None
		for response in (unsafe.encode(), json.dumps({"ok": False, "error_code": 400, "description": unsafe}).encode()):
			self.transport.response = response
			with self.assertRaises(TelegramError) as caught:
				client.send_message("합성 글")
			self.assertNotIn(self.token, str(caught.exception))
		client._transport = self.transport = FakeTransport()
		client.send_message(self.token + " " + self.chat)
		self.assertNotIn(self.token, self.transport.sent()[0]["text"])
		self.assertNotIn(self.chat, self.transport.sent()[0]["text"])

	def test_default_transport_blocks_redirects_and_proxy_settings(self):
		self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))
		with mock.patch("office.telegram_client.build_opener") as opener:
			opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"{}"
			self.assertEqual(transport(object(), timeout=35), b"{}")
			self.assertEqual(opener.call_args.args[0].proxies, {})
			self.assertIsInstance(opener.call_args.args[1], NoRedirect)
			self.assertEqual(opener.return_value.open.call_args.kwargs, {"timeout": 35})
