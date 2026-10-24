"""가상 저장소로 공개 저장소 push 전 검사(새 줄·경로·작성자·메시지)를 확인한다. 합성 이름만 쓴다."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

from vaultfilter import verify
from vaultfilter.public import check_public


class PublicCheckTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		root = Path(self.tmp.name)
		patch = mock.patch.dict(os.environ, {"AI_HUB_HOME": str(root / "hub")})
		patch.start()
		self.addCleanup(patch.stop)
		user = mock.patch("getpass.getuser", return_value="xyzuser")
		user.start()
		self.addCleanup(user.stop)
		self.dictionary = root / "hub/redaction/dictionary.json"
		self.dictionary.parent.mkdir(parents=True)
		entries = [("acme", "회사명"), ("esk", "회사명"), ("가나다", "서비스명"),
			("010-1234-5678", "전화번호"), ("127.0.0.1", "IP"), ("p01@acme.test", "이메일")]
		self.dictionary.write_text(json.dumps({"entries": [{"value": v, "kind": k, "token": f"t{i}"}
			for i, (v, k) in enumerate(entries, 1)]}, ensure_ascii=False), encoding="utf-8")
		self.repo = root / "repo"
		self.repo.mkdir()
		self.git("init", "-q")
		self.git("config", "user.name", "테스트")
		self.git("config", "user.email", "p02@example.test")
		self.git("config", "commit.gpgsign", "false")

	def git(self, *args):
		return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True).stdout

	def commit(self, name, text, message="합성 자료"):
		path = self.repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
		self.git("add", "--", name)
		self.git("commit", "-q", "-m", message)
		return self.git("rev-parse", "HEAD").decode().strip()

	def check(self, *revs):
		output = io.StringIO()
		with mock.patch("vaultfilter.verify._git", side_effect=self.git), contextlib.redirect_stdout(output):
			count = check_public(list(revs or ["HEAD"]))
		return count, output.getvalue()

	def test_clean_code_with_common_false_positives_passes(self):
		self.commit("app.py", 'HOST = "127.0.0.1"\nPHONE = "010-1234-5678"\n# Desktop 앱 경로\n')
		self.assertEqual(self.check(), (0, ""))

	def test_names_are_found_by_word_or_hangul_substring_without_printing_values(self):
		previous = self.commit("base.py", "x = 0\n")
		for name, text in (("a.py", "client = 'ACME'\n"), ("b.py", "# ESK 서버\n"), ("c.md", "가나다라 문서\n")):
			with self.subTest(name=name):
				current = self.commit(name, text)
				count, output = self.check(current, "--not", previous)
				previous = current
				self.assertEqual(count, 1)
				self.assertIn("사전 이름", output)
				for value in ("acme", "ACME", "ESK", "가나다"):
					self.assertNotIn(value, output)

	def test_decomposed_hangul_name_is_found(self):
		self.commit("a.md", unicodedata.normalize("NFD", "가나다 설명\n"))
		self.assertEqual(self.check()[0], 1)

	def test_secret_and_home_path_are_found(self):
		token = "ghp_" + "a" * 36
		self.commit("a.py", f"TOKEN = '{token}'\nPATH = '/Users/xyzuser/work'\n")
		count, output = self.check()
		self.assertEqual(count, 1)
		self.assertIn("비밀값", output)
		self.assertIn("홈 경로", output)
		self.assertNotIn(token, output)

	def test_allow_list_passes_only_the_exact_secret_shaped_string(self):
		allowed = "secret = " + '"' + "xyz-" + "synthetic-" + "value" + '"'
		other = "secret = " + '"' + "xyz-" + "synthetic-" + "other" + '"'
		(self.repo / ".githooks").mkdir()
		(self.repo / ".githooks/public-allow.txt").write_text(
			"# 합성 값\n" + hashlib.sha256(verify.SECRET_LITERAL.search(allowed).group(0).encode()).hexdigest() + "\n",
			encoding="utf-8")
		first = self.commit("a.py", allowed + "\n")
		self.commit("b.py", other + "\n")
		self.commit("c.py", "acme = 1\n")
		self.assertEqual(self.check(first)[0], 1)  # 지금 폴더(이 저장소)의 허용 목록에는 없는 값이다
		with contextlib.chdir(self.repo):  # 훅처럼 저장소 최상위에서 돌린다
			self.assertEqual(self.check(first), (0, ""))
			count, output = self.check("HEAD~2..HEAD~1")
			self.assertEqual(count, 1)
			self.assertIn("비밀값", output)
			self.assertEqual(self.check("HEAD^..HEAD")[0], 1)  # 이름 검사는 허용 목록과 관계없다

	def test_only_added_lines_are_checked(self):
		token = "sk-" + "b" * 24
		first = self.commit("t.py", f"FAKE = '{token}'  # 합성 값\nvalue = 1\n")
		self.commit("t.py", f"FAKE = '{token}'  # 합성 값\nvalue = 2\n")
		self.assertEqual(self.check(first + "..HEAD"), (0, ""))
		self.git("mv", "t.py", "moved.py")
		self.git("commit", "-q", "-m", "이름 바꾸기")
		self.assertEqual(self.check("HEAD^..HEAD"), (0, ""))
		self.assertEqual(self.check()[0], 1)

	def test_paths_protected_notes_and_binaries_are_rejected(self):
		self.commit("docs/acme-notes.md", "무해한 본문\n")
		self.commit("🔐private.md", "합성\n")
		self.commit("image.bin", b"\x00\x01\x02")
		count, output = self.check()
		self.assertEqual(count, 3)
		self.assertIn("경로 누출 의심", output)
		self.assertIn("커밋 금지 경로", output)
		self.assertIn("바이너리", output)
		self.assertNotIn("acme", output)

	def test_allow_list_passes_a_binary_only_by_exact_content_hash(self):
		blob = b"\x00synthetic-binary-for-test"
		digest = hashlib.sha256(blob).hexdigest()
		(self.repo / ".githooks").mkdir()
		allow = self.repo / ".githooks/public-allow.txt"
		allow.write_text(f"# 합성 바이너리\nbinary {digest}\n", encoding="utf-8")
		first = self.commit("lib/a.bin", blob)
		self.commit("lib/a.bin", blob + b"\x01")  # 같은 경로라도 내용이 다르면 막는다
		self.commit("other/b.bin", blob)  # 경로가 달라도 내용이 같으면 통과한다
		self.git("mv", "other/b.bin", "other/c.bin")
		self.git("commit", "-q", "-m", "이름 바꾸기")
		self.git("rm", "-q", "lib/a.bin")
		self.git("commit", "-q", "-m", "지우기")
		self.assertEqual(self.check(first)[0], 1)  # 이 폴더(저장소 밖)의 허용 목록에는 없다
		with contextlib.chdir(self.repo):
			self.assertEqual(self.check(first), (0, ""))
			count, output = self.check("HEAD~4..HEAD~3")
			self.assertEqual((count, "바이너리 1개" in output), (1, True))
			self.assertEqual(self.check("HEAD~3..HEAD~2"), (0, ""))
			self.assertEqual(self.check("HEAD~2..HEAD"), (0, ""))  # 이름 바꾸기·지우기
			allow.write_text(f"{digest}\n", encoding="utf-8")  # 값 해시 줄은 바이너리를 통과시키지 않는다
			self.assertEqual(self.check(first)[0], 1)

	def test_quoted_path_lines_are_still_checked(self):
		self.commit('odd"name.py', "x = 'acme'\n")
		count, output = self.check()
		self.assertEqual(count, 1)
		self.assertIn("파일 #", output)

	def test_author_and_message_are_checked(self):
		self.git("config", "user.email", "p01@acme.test")
		self.commit("a.py", "x = 1\n")
		self.git("config", "user.email", "p02@example.test")
		self.git("config", "user.name", "xyzuser")
		self.commit("b.py", "y = 1\n")
		self.git("config", "user.name", "테스트")
		self.commit("c.py", "z = 1\n", "고침\n\nCo-Authored-By: Claude <noreply@example.test>")
		self.commit("d.py", "w = 1\n", "acme 관련 수정")
		count, output = self.check()
		self.assertEqual(count, 4)
		self.assertEqual(output.count("작성자 정보"), 2)
		self.assertIn("AI 서명", output)
		self.assertIn("커밋 메시지 누출 의심", output)

	def test_missing_dictionary_fails_closed_through_command(self):
		self.commit("a.py", "x = 1\n")
		self.dictionary.unlink()
		with self.assertRaises(ValueError):
			self.check()
		with mock.patch("vaultfilter.verify._git", side_effect=self.git), contextlib.redirect_stdout(io.StringIO()):
			self.dictionary.write_text(json.dumps({"entries": [{"value": "acme", "kind": "회사명", "token": "t1"}]}))
			self.assertEqual(verify.main(["public", "HEAD"]), 0)
			self.commit("b.py", "acme\n")
			self.assertEqual(verify.main(["public", "HEAD"]), 1)


if __name__ == "__main__":
	unittest.main()
