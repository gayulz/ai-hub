"""소유자 전용 설정과 주소·오류 원문을 노출하지 않는 Bot API 전송."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from collector.config import home
from orchestrator.meeting_record import directory

TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
BOT_SECRET = re.compile(r"(?<![\w-])[0-9]{5,}:[A-Za-z0-9_-]{20,}(?![\w-])")


def configuration():
	parent = None
	try:
		parent = directory(home())
		fd = os.open("telegram.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
		with os.fdopen(fd, "rb") as stream:
			info = os.fstat(stream.fileno())
			if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
				return None, "설정 없음"
			if stat.S_IMODE(info.st_mode) != 0o600:
				return None, "권한 확인 필요"
			if info.st_size > 16384:
				return None, "설정 없음"
			data = tomllib.load(stream)
		if (set(data) != {"token", "chat_id"} or not isinstance(data["token"], str)
				or not TOKEN.fullmatch(data["token"]) or not isinstance(data["chat_id"], str)
				or not re.fullmatch(r"-?[0-9]+", data["chat_id"])):
			return None, "설정 없음"
		return data, "켜짐"
	except PermissionError:
		return None, "권한 확인 필요"
	except (OSError, ValueError, TypeError):
		return None, "설정 없음"
	finally:
		if parent is not None:
			os.close(parent)


class TelegramError(RuntimeError):
	def __init__(self, kind, code=None):
		self.kind = kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", kind) else "Error"
		self.code = code if type(code) is int and 100 <= code <= 599 else None
		super().__init__(self.kind + (f" HTTP {self.code}" if self.code else ""))


class NoRedirect(HTTPRedirectHandler):
	def redirect_request(self, request, fp, code, msg, headers, newurl):
		return None


def transport(request, *, timeout):
	with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
		return response.read()


class Client:
	def __init__(self, transport=transport):
		config, self.status = configuration()
		self._token = config["token"] if config else ""
		self._chat = config["chat_id"] if config else ""
		self._transport = transport

	def private(self, message, sender):
		if not isinstance(message, dict) or not isinstance(sender, dict) or not isinstance(message.get("chat"), dict):
			return False
		chat = message.get("chat", {})
		return (chat.get("type") == "private" and type(chat.get("id")) is int
			and type(sender.get("id")) is int and str(chat["id"]) == self._chat == str(sender["id"]))

	def sensitive(self, value):
		return bool((self._token and self._token in value) or BOT_SECRET.search(value) or (self._chat and self._chat in value))

	def clean(self, value):
		value = str(value)
		if self._token:
			value = value.replace(self._token, "[보안값]")
		value = BOT_SECRET.sub("[보안값]", value)
		return value.replace(self._chat, "[보안값]") if self._chat else value

	def _call(self, method, payload):
		if self.status != "켜짐":
			raise TelegramError("ConfigurationError")
		try:
			request = Request("https://api.telegram.org/bot" + self._token + "/" + method,
				data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
				headers={"Content-Type": "application/json"}, method="POST")
			data = json.loads(self._transport(request, timeout=35))
			if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
				error = TelegramError("APIError", data.get("error_code") if isinstance(data, dict) else None)
			else:
				return data["result"]
		except Exception as exc:
			error = TelegramError(type(exc).__name__, getattr(exc, "code", None))
			if isinstance(exc, HTTPError):
				try:
					exc.close()
				except Exception:
					pass
		# except 블록 밖에서 올려 공급자 예외의 주소·본문이 연결되지 않게 한다.
		raise error

	def _message(self, method, text, buttons, **values):
		payload = {"chat_id": self._chat, "text": self.clean(text)[:4096], **values}
		if buttons is not None or method == "editMessageText":
			keyboard = []
			for row in buttons or []:
				items = []
				for button in row:
					data = button["callback_data"]
					if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
						raise TelegramError("CallbackDataError")
					items.append({"text": self.clean(button["text"])[:4096], "callback_data": data})
				keyboard.append(items)
			payload["reply_markup"] = {"inline_keyboard": keyboard}
		return self._call(method, payload)

	def send_message(self, text, buttons=None):
		return self._message("sendMessage", text, buttons)

	def edit_message(self, message_id, text, buttons=None):
		return self._message("editMessageText", text, buttons, message_id=message_id)

	def answer_callback(self, callback_id, text=""):
		return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": self.clean(text)[:4096]})

	def get_updates(self, offset, timeout=25):
		result = self._call("getUpdates", {"offset": offset, "timeout": timeout,
			"allowed_updates": ["message", "callback_query"]})
		if not isinstance(result, list):
			raise TelegramError("ResponseError")
		return result
