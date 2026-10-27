package dev.aihub.search.query;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.IntStream;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class HybridSearchTest {

	@TempDir
	Path temp;

	@Test
	void addsHandCalculatedReciprocalRanksDeduplicatesAndBreaksTiesByPath() {
		var bm25 = List.of(hit("p02.md"), hit("p02.md"), hit("p01.md"), hit("p03.md"));
		var vector = List.of(hit("p01.md"), hit("p02.md"), hit("p04.md"), hit("p04.md"));
		var fused = SearchService.fuse(bm25, vector, 5);
		assertThat(fused).extracting(SearchHit::path).containsExactly("p01.md", "p02.md", "p03.md", "p04.md");
		assertThat(fused.getFirst().score()).isCloseTo((float) (1.0 / 61 + 1.0 / 62), within(1e-8f));
		assertThat(fused.get(1).score()).isEqualTo(fused.getFirst().score());
		assertThat(fused.get(2).score()).isCloseTo(1f / 63, within(1e-8f));
		assertThat(fused.get(3).score()).isEqualTo(fused.get(2).score());
		assertThat(fused.getFirst().heading()).isEqualTo("h01");
		assertThat(fused.getFirst().snippet()).isEqualTo("본문");
		assertThat(SearchService.fuse(bm25, vector, 1)).containsExactly(fused.getFirst());
		assertThat(SearchService.fuse(List.of(), List.of(), 5)).isEmpty();
	}

	@Test
	void usesAtMostOneHundredNotesFromEachRanking() {
		var bm25 = IntStream.range(0, 120).mapToObj(i -> hit("acme/p%03d.md".formatted(i))).toList();
		var vector = IntStream.range(0, 120).mapToObj(i -> hit("xyz/p%03d.md".formatted(i))).toList();
		var fused = SearchService.fuse(bm25, vector, Integer.MAX_VALUE);
		assertThat(fused).hasSize(200).extracting(SearchHit::path).doesNotHaveDuplicates();
		assertThat(fused).extracting(SearchHit::path).doesNotContain("acme/p100.md", "xyz/p100.md");
	}

	@Test
	void requestsCappedNoteRankingsAndPropagatesVectorFailureWithoutFallback() throws IOException {
		var settings = new SearchSettings();
		var service = new SearchService(new DefaultAnalyzerFactory(settings), settings, texts -> List.of()) {
			@Override
			public List<SearchHit> searchNotes(String query, int k, boolean company) {
				assertThat(query).isEqualTo("임시저장");
				assertThat(k).isEqualTo(100);
				assertThat(company).isFalse();
				return List.of(hit("p01.md"));
			}

			@Override
			public List<SearchHit> searchVector(String query, int k, boolean company) throws IOException {
				assertThat(k).isEqualTo(100);
				assertThat(company).isFalse();
				throw new EmbeddingException("합성 벡터 실패");
			}
		};
		assertThatThrownBy(() -> service.searchHybrid("임시저장", 5, false)).isInstanceOf(EmbeddingException.class)
				.hasMessage("합성 벡터 실패");
		assertThatThrownBy(() -> service.searchHybrid("임시저장", 0, false)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> service.searchHybrid(null, 5, false)).isInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void filtersCompanyInsideBothQueriesAndReturnsDistinctNotes() throws IOException {
		Path vault = Files.createDirectory(temp.resolve("vault"));
		for (int i = 0; i < 105; i++) {
			Files.writeString(vault.resolve("acme" + i + ".md"),
					"---\nsensitivity: company\n---\n# 임시저장\n## 임시저장\n임시저장");
		}
		Files.writeString(vault.resolve("p01.md"), "임시저장\n## h01\n임시저장\n## h02\n임시저장");
		Files.writeString(vault.resolve("p02.md"), "임시저장 일반 본문");
		var settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(4);
		var factory = new DefaultAnalyzerFactory(settings);
		var fake = new FakeEmbedder(4);
		new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, fake).index(vault);
		var service = new SearchService(factory, settings, fake);
		assertThat(service.searchHybrid("임시저장", 2, false)).hasSize(2)
				.extracting(SearchHit::path).containsExactlyInAnyOrder("p01.md", "p02.md");
		assertThat(service.searchHybrid("임시저장", 5, true)).hasSize(5)
				.anySatisfy(hit -> assertThat(hit.sensitivity()).isEqualTo("company"))
				.extracting(SearchHit::path).doesNotHaveDuplicates();
	}

	private SearchHit hit(String path) {
		return new SearchHit(path, "p01", "h01", 5, "본문", null);
	}
}
