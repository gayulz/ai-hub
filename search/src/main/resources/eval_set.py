import hashlib
import re
import struct
import sys
import unicodedata


def nfc(value):
	return unicodedata.normalize("NFC", value)


def text(value, limit):
	if not isinstance(value, str) or not value.strip() or len(value) > limit:
		raise ValueError()
	value = nfc(value)
	value.encode("utf-8", errors="strict")
	return value


def answer(value):
	value = text(value, 4096)
	if "\\" in value or ":" in value or any(ord(char) < 32 for char in value):
		raise ValueError()
	parts = value.split("/")
	if not value.endswith(".md") or any(
		part in ("", ".", "..", ".obsidian", ".git", ".trash")
		or part.startswith("🔐") for part in parts
	):
		raise ValueError()
	return value


def write_string(output, value):
	encoded = value.encode("utf-8")
	output.write(struct.pack(">I", len(encoded)))
	output.write(encoded)


def main():
	if sys.version_info < (3, 11):
		return 3
	import tomllib

	with open(sys.argv[1], "rb") as source:
		raw = source.read(4 * 1024 * 1024 + 1)
	if len(raw) > 4 * 1024 * 1024:
		raise ValueError()
	data = tomllib.loads(raw.decode("utf-8"))
	if set(data) - {"query"}:
		raise ValueError()
	queries = data.get("query", [])
	if not isinstance(queries, list) or len(queries) > 10000:
		raise ValueError()
	rows = []
	ids = set()
	required = {"id", "text", "company", "answers"}
	metadata = {"category", "source", "answer_candidates"}
	for query in queries:
		if not isinstance(query, dict) or not required <= query.keys() or query.keys() - required - metadata:
			raise ValueError()
		identifier = text(query["id"], 64)
		if not re.fullmatch(r"[\w-]+", identifier) or identifier in ids:
			raise ValueError()
		ids.add(identifier)
		question = text(query["text"], 8192)
		company = query["company"]
		answers = query["answers"]
		if type(company) is not bool or not isinstance(answers, list) or not 1 <= len(answers) <= 1000:
			raise ValueError()
		rows.append((identifier, question, company, [answer(value) for value in answers]))
	with open(sys.argv[2], "wb") as output:
		write_string(output, hashlib.sha256(raw).hexdigest()[:12])
		output.write(struct.pack(">I", len(rows)))
		for identifier, question, company, answers in rows:
			write_string(output, identifier)
			write_string(output, question)
			output.write(struct.pack(">?I", company, len(answers)))
			for value in answers:
				write_string(output, value)
	return 0


if __name__ == "__main__":
	try:
		sys.exit(main())
	except OSError:
		sys.exit(4)
	except Exception:
		# 예외 원문에는 질의나 경로가 들어갈 수 있어 출력하지 않는다.
		sys.exit(2)
