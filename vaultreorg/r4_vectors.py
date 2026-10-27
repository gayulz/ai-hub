"""R4 개인 노트 선별·로컬 의미 벡터·유사도 묶음. 제외 본문은 읽지 않는다."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from collector.backfill_common import private_write, sha
from collector.config import ConfigError

from . import frontmatter, r3_select
from .r3 import state_file

TYPES = {"study", "reference", "project", "career", "decision"}
MODEL = "bge-m3"


def load_cache(name: str) -> dict:
	path = state_file(name)
	if not path.exists():
		return {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		if data["version"] != 4 or not isinstance(data["entries"], dict):
			raise ValueError
		return data["entries"]
	except (ValueError, KeyError, TypeError) as exc:
		raise ConfigError("R4 캐시 형식 오류") from exc


def save_cache(name: str, entries: dict) -> None:
	private_write(state_file(name), json.dumps({"version": 4, "entries": entries}, ensure_ascii=False) + "\n")


def read_note(vault, value: str) -> tuple[dict | None, str]:
	path = r3_select.target(vault, value)
	with path.open("rb", buffering=0) as source:
		opening = source.readline(r3_select.MAX_HEADER + 1)
		if opening.removeprefix(frontmatter.BOM).rstrip(b"\r\n") != b"---":
			return None, "머리말 없음"
		lines, size = [opening], len(opening)
		while size <= r3_select.MAX_HEADER:
			line = source.readline(r3_select.MAX_HEADER - size + 1)
			if not line:
				break
			lines.append(line)
			size += len(line)
			if size > r3_select.MAX_HEADER:
				break
			if line.rstrip(b"\r\n") != b"---":
				continue
			header = b"".join(lines)
			parsed = frontmatter.split(header)
			fields = r3_select.fields(parsed)
			if fields.get("sensitivity") != "personal":
				return None, "회사 노트" if fields.get("sensitivity") == "company" else "personal 분류 없음"
			if fields.get("type") not in TYPES:
				return None, "제외 종류"
			body = source.read()
			text = body.decode("utf-8")
			titles = [frontmatter.scalar(row.split(":", 1)[1].rstrip("\r\n"))[0]
				for row in parsed.lines if frontmatter.key(row) == "title"]
			if len(titles) > 1:
				raise frontmatter.NoteError("제목 키 중복")
			heading = re.search(r"(?m)^#\s+([^\r\n]+)", text)
			title = titles[0] if titles else heading[1].strip() if heading else path.stem
			return {"path": value, "type": fields["type"], "title": title, "data": header + body,
				"body": text[:3000], "before": sha(header + body), "body_hash": sha(body),
				"input_hash": sha(title + "\n" + text[:3000])}, ""
	return None, "머리말 형식 오류"


def collect(vault, skipped: Counter) -> list[dict]:
	notes = []
	for path in r3_select.inventory(vault, skipped):
		try:
			note, reason = read_note(vault, path.relative_to(vault).as_posix())
		except (OSError, ValueError, ConfigError):
			note, reason = None, "읽기 또는 형식 오류"
		if note is None:
			skipped[reason] += 1
		else:
			notes.append(note)
	return notes


class NoRedirect(HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		raise ConfigError("R4 임베딩의 리디렉션은 허용하지 않습니다")


def embed(text: str) -> list:
	# 프록시 환경 변수와 리디렉션을 막아 노트가 루프백 밖으로 나가지 않게 한다.
	request = Request("http://127.0.0.1:11434/api/embed",
		data=json.dumps({"model": MODEL, "input": text}, ensure_ascii=False).encode("utf-8"),
		headers={"Content-Type": "application/json"})
	try:
		with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=300) as response:
			data = json.load(response)
		if not isinstance(data, dict) or not isinstance(data.get("embeddings"), list) or len(data["embeddings"]) != 1:
			raise ValueError
		return data["embeddings"][0]
	except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError) as exc:
		raise ConfigError("R4 로컬 Ollama bge-m3 임베딩 응답 실패") from exc


def unit(vector) -> list[float]:
	if not isinstance(vector, list) or not vector or any(type(v) not in (float, int) for v in vector):
		raise ConfigError("R4 벡터 형식 오류")
	try:
		norm = math.hypot(*vector)
		if not math.isfinite(norm) or norm == 0:
			raise ValueError
		return [value / norm for value in vector]
	except (ValueError, OverflowError) as exc:
		raise ConfigError("R4 벡터 크기 오류") from exc


def vectors(notes: list[dict]) -> list[list[float]]:
	cache, result = load_cache("r4-embed-cache.json"), []
	for note in notes:
		# 본문이 같아도 제목이 바뀌면 입력이 달라지므로 제목을 포함한 해시를 함께 확인한다.
		entries = cache.setdefault(note["body_hash"], {})
		if not isinstance(entries, dict):
			raise ConfigError("R4 벡터 캐시 항목 형식 오류")
		key = MODEL + ":" + note["input_hash"]
		if key not in entries:
			entries[key] = unit(embed(note["title"] + "\n" + note["body"]))
			save_cache("r4-embed-cache.json", cache)
		vector = unit(entries[key])
		if result and len(vector) != len(result[0]):
			raise ConfigError("R4 벡터 차원이 서로 다릅니다")
		result.append(vector)
	return result


def group(notes: list[dict], embeddings: list[list[float]], threshold: float):
	pairs, bins = [], [0] * 40
	for left in range(len(notes)):
		for right in range(left + 1, len(notes)):
			score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(embeddings[left], embeddings[right]))))
			bins[min(39, int(math.floor((score + 1) * 20 + 1e-9)))] += 1
			pairs.append((score, left, right))
	parts = [{i} for i in range(len(notes))]
	owners = list(range(len(notes)))
	for score, left, right in sorted(pairs, reverse=True):
		if score < threshold:
			break
		a, b = owners[left], owners[right]
		if a == b or len(parts[a]) + len(parts[b]) > 6:
			continue
		parts[a].update(parts[b])
		for index in parts[b]:
			owners[index] = a
		parts[b].clear()
	groups = []
	for indices in parts:
		if len(indices) < 2:
			continue
		scores = [{"left": notes[left]["path"], "right": notes[right]["path"], "similarity": score}
			for score, left, right in sorted(pairs, reverse=True) if left in indices and right in indices]
		groups.append({"notes": [notes[index] for index in sorted(indices)], "pairs": scores,
			"similarity": scores[0]["similarity"]})
	groups.sort(key=lambda item: (-item["similarity"], tuple(note["path"] for note in item["notes"])))
	distribution = [{"lower": round(-1 + i * .05, 2), "upper": round(-1 + (i + 1) * .05, 2), "pairs": count}
		for i, count in enumerate(bins)]
	return groups, distribution
