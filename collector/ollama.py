"""Ollama의 본문만 읽는다. 설치·모델 다운로드는 사용자가 수행한다."""

from __future__ import annotations

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaError(Exception):
	"""응답 원문이나 접속정보를 오류에 포함하지 않는다."""


def request(plan, endpoint, payload=None):
	data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
	req = Request(plan.url.rstrip("/") + endpoint, data=data,
		headers={"Content-Type": "application/json"})
	try:
		with urlopen(req, timeout=plan.timeout) as response:
			result = json.load(response)
	except HTTPError as exc:
		if exc.code == 404:
			raise OllamaError("Ollama 모델을 찾지 못했습니다. plans.toml의 model을 확인하고 "
				"`ollama pull <설정한 모델>`로 준비하세요") from exc
		raise OllamaError("Ollama 요청이 거부됐습니다. 실행 상태와 모델 설정을 확인하세요") from exc
	except (URLError, OSError, ValueError, HTTPException) as exc:
		raise OllamaError("Ollama 연결·응답 실패. Ollama를 설치한 뒤 `ollama serve`를 실행하고 "
			"plans.toml의 url·timeout을 확인하세요. 모델은 `ollama pull <설정한 모델>`로 준비하세요") from exc
	if not isinstance(result, dict) or result.get("error"):
		raise OllamaError("Ollama 오류 응답입니다. `ollama serve`와 `ollama list`로 실행·모델 상태를 확인하세요")
	return result


def check(plan):
	models = request(plan, "/api/tags").get("models", [])
	names = [m.get("name") or m.get("model") for m in models if isinstance(m, dict)] if isinstance(models, list) else []
	if plan.model not in names and plan.model + ":latest" not in names:
		raise OllamaError("Ollama에 설정한 모델이 없습니다. "
			"`ollama pull <설정한 모델>`을 실행하고 plans.toml의 model을 확인하세요")


def call(plan, prompt, *, metadata=None):
	result = request(plan, "/api/generate", {"model": plan.model, "prompt": prompt, "stream": False})
	text = result.get("response")
	if not isinstance(text, str) or not text.strip():
		raise OllamaError("Ollama 본문 응답이 비었습니다. 모델의 본문 출력 설정을 확인하세요")
	if metadata is not None and isinstance(result.get("model"), str):
		metadata["model"] = result["model"]
	return text.strip()
