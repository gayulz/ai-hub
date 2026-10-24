"""본문 바이트와 기존 키·태그 표기의 회귀 검사."""

import unittest

from vaultreorg import frontmatter as fm


class FrontmatterTest(unittest.TestCase):
	def test_body_bytes_bom_and_line_endings(self):
		for newline in ("\n", "\r\n"):
			for bom in (b"", fm.BOM):
				for has_header in (False, True):
					with self.subTest(newline=newline, bom=bool(bom), has_header=has_header):
						body = ("\n# 본문\r\n혼합 줄 끝\n---\n끝").encode()
						prefix = (f"---{newline}aliases: [p01]{newline}---{newline}").encode() if has_header else b""
						# 머리말이 없을 때도 처음 만나는 줄 끝을 따른다.
						if not has_header:
							body = ("# 본문" + newline).encode() + body
						data = bom + prefix + body
						after, _, _ = fm.render(data, {"type": "study", "sensitivity": "personal", "created": "2026-01-02"}, {})
						self.assertEqual(fm.split(after).body, body)
						self.assertEqual(fm.split(after).bom, bom)
						self.assertEqual(after.count(fm.BOM), int(bool(bom)))
						self.assertIn((f"type: study{newline}sensitivity: personal{newline}created: 2026-01-02{newline}---{newline}").encode(), after)

	def test_existing_keys_and_other_metadata_are_exact(self):
		original = ("---\n'created': '2001-01-01' # 그대로\ntype: custom\nsensitivity: company\n"
			"aliases:\n  - p01\nsource: 'xyz'\ntistory-id: 8\ntistory_url: p01\nother:\n  type: inner\n---\n본문\n").encode()
		after, _, _ = fm.render(original, {"type": "study", "sensitivity": "personal", "created": "2026-01-01"}, {})
		self.assertEqual(after, original)

	def test_missing_keys_are_added_in_order_before_closing(self):
		original = b"---\naliases: p01\nsensitivity:\n---"
		after, _, _ = fm.render(original, {"type": "study", "sensitivity": "personal", "created": "2026-01-01"}, {})
		self.assertEqual(after, b"---\naliases: p01\nsensitivity:\ntype: study\ncreated: 2026-01-01\n---")

	def test_unclosed_or_non_utf8_is_refused(self):
		for data in (b"---\ntags: [p01]\n", b"\xff\n"):
			with self.subTest(data=data), self.assertRaises(ValueError):
				fm.render(data, {"type": "study"}, {})

	def test_inline_normalization_quote_style_map_and_duplicates(self):
		data = "---\ntags: [Spring_Boot, 'JAVA  17', \"java-17\", 한글, '기술', C++, 'Spring__Boot'] # 메모\n---\n본문".encode()
		after, changes, unsupported = fm.render(data, {}, {"기술": "tech-name"})
		self.assertFalse(unsupported)
		self.assertIn("tags: [spring-boot, 'java-17', 한글, 'tech-name', C++] # 메모\n".encode(), after)
		self.assertIn(("JAVA  17", "java-17"), changes)
		self.assertIn(("기술", "tech-name"), changes)
		self.assertEqual(fm.split(after).body, "본문".encode())

	def test_block_normalization_preserves_comments_quotes_and_crlf(self):
		data = "---\r\ntags:\r\n  - \"Spring Boot\"\r\n  # 태그 설명\r\n  - spring_boot\r\n\r\n  - 'JAVA_17'\r\n  - 한글\r\nsource: xyz\r\n---\r\n본문".encode()
		after, _, unsupported = fm.render(data, {}, {})
		self.assertFalse(unsupported)
		self.assertEqual(after, "---\r\ntags:\r\n  - \"spring-boot\"\r\n  # 태그 설명\r\n\r\n  - 'java-17'\r\n  - 한글\r\nsource: xyz\r\n---\r\n본문".encode())

	def test_unchanged_tags_and_escaped_quotes_are_byte_identical(self):
		for text in ("tags: [spring-boot, 한글]  # 그대로\n", "tags: [\"p01,xyz\", '한''글']\n",
			"tags:\n  - spring-boot\n  - '한글'\n", "tags: []\n", "tags: [  ]\n", "tags: [spring-boot,  ]\n"):
			data = ("---\n" + text + "---\n본문").encode()
			with self.subTest(text=text):
				after, changes, unsupported = fm.render(data, {}, {})
				self.assertFalse(unsupported)
				self.assertEqual(changes, [])
				self.assertEqual(after, data)

	def test_unsupported_tags_are_untouched(self):
		for text in ("tags: p01\n", "tags: {p01: xyz}\n", "tags: |\n  p01\n", "tags:\n",
			"tags:\n  - {p01: xyz}\n", "tags: [p01, [xyz]]\n", "tags: [*p01]\n",
			"tags: [p01]\ntags: [xyz]\n", "tags:\n - p01\n"):
			with self.subTest(text=text):
				data = ("---\n" + text + "---\n본문").encode()
				after, changes, unsupported = fm.render(data, {}, {})
				self.assertTrue(unsupported)
				self.assertEqual(changes, [])
				self.assertEqual(after, data)

	def test_map_precedes_automatic_normalization(self):
		data = b"---\ntags: [JAVA_17, java-17, 'JAVA_17']\n---\n"
		after, _, unsupported = fm.render(data, {}, {"JAVA_17": "jvm"})
		self.assertFalse(unsupported)
		self.assertEqual(after, b"---\ntags: [jvm, java-17]\n---\n")

	def test_inline_trailing_comma_is_kept_when_normalizing(self):
		data = b"---\ntags: [JAVA_17, java-17, ]\n---\n"
		after, _, unsupported = fm.render(data, {}, {})
		self.assertFalse(unsupported)
		self.assertEqual(after, b"---\ntags: [java-17, ]\n---\n")
