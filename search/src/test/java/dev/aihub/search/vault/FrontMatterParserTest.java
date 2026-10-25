package dev.aihub.search.vault;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;

import org.junit.jupiter.api.Test;

class FrontMatterParserTest {

	private final FrontMatterParser parser = new FrontMatterParser();

	@Test
	void readsScalarsAndQuotedInlineTags() {
		var parsed = parser.parse("---\ntype: 'note'\nsensitivity: \"company\"\ncreated: '2026-01-01'\n"
				+ "tags: [acme, 'xyz', \"p01,p02\"]\n---\n# p01\n본문");
		assertThat(parsed.type()).isEqualTo("note");
		assertThat(parsed.sensitivity()).isEqualTo("company");
		assertThat(parsed.created()).isEqualTo("2026-01-01");
		assertThat(parsed.tags()).containsExactly("acme", "xyz", "p01,p02");
		assertThat(parsed.body()).isEqualTo("# p01\n본문");
	}

	@Test
	void readsBlockTagsAndWindowsLineEndings() {
		var parsed = parser.parse("---\r\ntags:\r\n  - 'acme'\r\n  - \"xyz\"\r\n"
				+ "type: note\r\n---\r\n# p01\r\n본문");
		assertThat(parsed.tags()).containsExactly("acme", "xyz");
		assertThat(parsed.type()).isEqualTo("note");
		assertThat(parsed.body()).isEqualTo("# p01\r\n본문");
	}

	@Test
	void missingHeaderLeavesBodyAndEmptyMetadata() {
		for (String body : List.of("# p01\n본문", "\n---\ntype: note\n---\n본문", "---")) {
			var parsed = parser.parse(body);
			assertEmpty(parsed);
			assertThat(parsed.body()).isEqualTo(body);
		}
	}

	@Test
	void unknownPropertiesAndContinuationLinesPreserveMetadata() {
		for (String extra : List.of("aliases:\n  - acme\n  - 'xyz", "작성자: acme",
				"description: >\n  문장: 설명\n  다음 줄", "깨진 줄",
				"description: |\n  sensitivity: public\n  tags: [acme")) {
			var parsed = parser.parse("---\ntype: note\nsensitivity: company\n" + extra
					+ "\ncreated: 2026-01-01\ntags:\n  - xyz\n---\n# p01");
			assertThat(parsed.type()).isEqualTo("note");
			assertThat(parsed.sensitivity()).isEqualTo("company");
			assertThat(parsed.created()).isEqualTo("2026-01-01");
			assertThat(parsed.tags()).containsExactly("xyz");
			assertThat(parsed.body()).isEqualTo("# p01");
		}
	}

	@Test
	void malformedKnownValuesLeaveFullTextAndTreatTheNoteAsCompanyWithoutThrowing() {
		for (String header : List.of("type: note\ntags: [acme",
				"sensitivity: company\ntags: [acme, 'xyz]", "type: 'note", "type: [note]",
				"type: note\ntype: xyz", "tags:\n - acme\n -", "tags: [acme,,xyz]")) {
			String text = "---\n" + header + "\n---\n# p01";
			var parsed = parser.parse(text);
			assertThat(parsed.type()).isNull();
			assertThat(parsed.sensitivity()).isEqualTo("company");
			assertThat(parsed.created()).isNull();
			assertThat(parsed.tags()).isEmpty();
			assertThat(parsed.body()).isEqualTo(text);
		}
		assertEmpty(parser.parse("---\ntype: note\n# p01"));
	}

	@Test
	void sensitivityValueIsComparedInLowerCase() {
		assertThat(parser.parse("---\nsensitivity: Company\n---\n# p01").sensitivity()).isEqualTo("company");
	}

	@Test
	void missingValuesAndEmptyTagsAreAllowed() {
		var parsed = parser.parse("---\n# 설명\ntype: null\nsensitivity: ~\ncreated:\ntags: []\n"
				+ "extra: xyz\n---\n본문");
		assertEmpty(parsed);
		assertThat(parsed.body()).isEqualTo("본문");
	}

	private void assertEmpty(FrontMatterParser.Parsed parsed) {
		assertThat(parsed.type()).isNull();
		assertThat(parsed.sensitivity()).isNull();
		assertThat(parsed.created()).isNull();
		assertThat(parsed.tags()).isEmpty();
	}
}
