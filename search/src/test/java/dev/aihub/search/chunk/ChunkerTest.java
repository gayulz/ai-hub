package dev.aihub.search.chunk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.List;
import java.util.stream.Collectors;

import org.junit.jupiter.api.Test;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.vault.Note;

class ChunkerTest {

	private final Chunker chunker = new Chunker(new SearchSettings());

	@Test
	void keepsIntroductionAndNestedHeadingsInsideSecondLevelSections() {
		var chunks = chunker.chunk(note("# p01\n도입부\n### 도입 제목\n도입 본문\n\n## 상위\n본문\n### 이전\n이전 본문\n"
				+ "### 하위\n하위 본문\n#### 깊이\n깊은 본문\n## 다음\n마지막"));
		assertThat(chunks).extracting(Chunk::heading).containsExactly("", "상위 > 하위 > 깊이", "다음");
		assertThat(chunks).extracting(Chunk::ordinal).containsExactly(0, 1, 2);
		assertThat(chunks.get(0).body()).contains("도입부");
		assertThat(chunks.get(1).body()).contains("### 이전", "### 하위", "#### 깊이", "깊은 본문");
	}

	@Test
	void ignoresHeadingsAndShorterOrDifferentClosingFencesInsideCode() {
		String body = "## 상위\n````java\n## 가짜\n```\n### 가짜\n~~~\n\n코드\n````\n"
				+ "~~~\n## 가짜\n### 가짜\n~~~\n### 하위\n본문\n## 다음\n마지막";
		var chunks = chunker.chunk(note(body));
		assertThat(chunks).extracting(Chunk::heading).containsExactly("상위 > 하위", "다음");
		assertThat(chunks.getFirst().body()).contains("## 가짜", "### 가짜", "\n\n코드");
	}

	@Test
	void groupsParagraphsNearSoftLimit() {
		String paragraph = "가".repeat(500);
		var chunks = chunker.chunk(note(String.join("\n\n", paragraph, paragraph, paragraph)));
		assertThat(chunks).extracting(chunk -> length(chunk.body())).containsExactly(1002, 500);
		assertThat(chunks.stream().map(Chunk::body).collect(Collectors.joining("\n\n")))
				.isEqualTo(String.join("\n\n", paragraph, paragraph, paragraph));
	}

	@Test
	void allowsOneParagraphAboveSoftLimitButCutsAtHardLimit() {
		assertThat(chunker.chunk(note("가".repeat(1500)))).hasSize(1);
		assertThat(chunker.chunk(note("가".repeat(4500)))).extracting(chunk -> length(chunk.body()))
				.containsExactly(2000, 2000, 500);
	}

	@Test
	void cutsByCodePointWithoutSplittingSurrogatePairs() {
		String symbol = new String(Character.toChars(0x1d11e));
		String body = symbol.repeat(2501);
		var chunks = chunker.chunk(note(body));
		assertThat(chunks).extracting(chunk -> length(chunk.body())).containsExactly(2000, 501);
		assertThat(chunks.stream().map(Chunk::body).collect(Collectors.joining())).isEqualTo(body);
		for (Chunk chunk : chunks) {
			assertThat(chunk.body().codePoints().allMatch(cp -> cp < 0xd800 || cp > 0xdfff)).isTrue();
		}
	}

	@Test
	void emitsNoEmptyChunksAndCarriesAllMetadata() {
		for (String body : List.of("", " ", "\n\n\t\n")) {
			assertThat(chunker.chunk(note(body))).isEmpty();
		}
		Chunk chunk = chunker.chunk(note("\n\n본문\n\n")).getFirst();
		assertThat(chunk.path()).isEqualTo("acme/p01.md");
		assertThat(chunk.noteTitle()).isEqualTo("p01");
		assertThat(chunk.type()).isEqualTo("note");
		assertThat(chunk.sensitivity()).isEqualTo("company");
		assertThat(chunk.created()).isEqualTo("2026-01-01");
		assertThat(chunk.tags()).containsExactly("acme", "xyz");
		assertThat(chunk.hash()).isEqualTo("a".repeat(64));
		assertThat(chunk.body()).isEqualTo("본문");
	}

	@Test
	void readsBothLimitsFromSettingsAndRejectsInvalidLimits() {
		var settings = new SearchSettings();
		settings.setChunkSoftLimit(10);
		settings.setChunkHardLimit(20);
		var configured = new Chunker(settings);
		assertThat(configured.chunk(note("가".repeat(8) + "\n\n" + "나".repeat(8))))
				.extracting(chunk -> length(chunk.body())).containsExactly(8, 8);
		assertThat(configured.chunk(note("가".repeat(25)))).extracting(chunk -> length(chunk.body()))
				.containsExactly(20, 5);
		settings.setChunkSoftLimit(0);
		assertThatThrownBy(() -> new Chunker(settings)).isInstanceOf(IllegalArgumentException.class);
		settings.setChunkSoftLimit(21);
		assertThatThrownBy(() -> new Chunker(settings)).isInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void usesHeadingAtEachSplitAndClearsDeeperLevels() {
		var settings = new SearchSettings();
		settings.setChunkSoftLimit(20);
		settings.setChunkHardLimit(40);
		var chunks = new Chunker(settings).chunk(note("## 상위\n" + "가".repeat(15)
				+ "\n\n### 하위\n" + "나".repeat(15) + "\n\n### 새하위\n다음"));
		assertThat(chunks).extracting(Chunk::heading).containsExactly("상위", "상위 > 하위", "상위 > 새하위");
	}

	@Test
	void neverAssignsAFutureHeadingToEarlierHardSplitPieces() {
		String body = "## 상위\n" + "가".repeat(3000) + "\n### 하위\n마지막";
		var chunks = chunker.chunk(note(body));
		assertThat(chunks).extracting(Chunk::heading).containsExactly("상위", "상위 > 하위");
		assertThat(chunks.getFirst().body()).doesNotContain("### 하위");
		assertThat(chunks.get(1).body()).contains("### 하위");
	}

	private Note note(String body) {
		return new Note("acme/p01.md", "p01", body, "note", "company", "2026-01-01",
				List.of("acme", "xyz"), "a".repeat(64));
	}

	private int length(String text) {
		return text.codePointCount(0, text.length());
	}
}
