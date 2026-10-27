package dev.aihub.search.query;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class VectorSearchTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private NoteIndexer indexer;
	private SearchService service;
	private final DefaultAnalyzerFactory factory = new DefaultAnalyzerFactory();

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(16);
		var fake = new FakeEmbedder(settings.getEmbeddingDimension());
		indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, fake);
		service = new SearchService(factory, settings, fake);
	}

	@Test
	void ranksMatchingVectorFirstAndReturnsOnlyHighestScoringChunkOfEachNote() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "# acme\n## h01\nxyz\n## h02\nacme");
		Files.writeString(vault.resolve("p02.md"), "# xyz\nacme");
		indexer.index(vault);
		List<SearchHit> hits = service.searchVector("acme\nh01\n## h01\nxyz", 5, false);
		assertThat(hits).hasSize(2).extracting(SearchHit::path).containsExactly("p01.md", "p02.md");
		assertThat(hits).extracting(SearchHit::path).doesNotHaveDuplicates();
		assertThat(hits).extracting(SearchHit::score).isSortedAccordingTo(Comparator.reverseOrder());
		SearchHit best = hits.getFirst();
		assertThat(best.score()).isCloseTo(1.0f, within(1e-6f));
		assertThat(best.noteTitle()).isEqualTo("acme");
		assertThat(best.heading()).isEqualTo("h01");
		assertThat(best.snippet()).isEqualTo("## h01 xyz");
		assertThat(best.sensitivity()).isNull();
		assertThat(service.searchVector("acme\nh01\n## h01\nxyz", 1, false)).containsExactly(best);
	}

	@Test
	void excludesCompanyChunksInsideKnnFilterBeforeCandidateLimit() throws Exception {
		Path company = Files.createDirectory(vault.resolve("acme"));
		for (int i = 0; i < 120; i++) {
			Files.writeString(company.resolve("p" + i + ".md"), "---\nsensitivity: company\n---\n# acme\nxyz");
		}
		Files.writeString(vault.resolve("p01.md"), "일반 본문");
		Files.writeString(vault.resolve("p02.md"), "---\nsensitivity: personal\n---\n다음 본문");
		indexer.index(vault);
		String query = "acme\n\n# acme\nxyz";
		assertThat(service.searchVector(query, 2, false)).hasSize(2)
				.allSatisfy(hit -> assertThat(hit.sensitivity()).isNotEqualTo("company"))
				.extracting(SearchHit::path).containsExactlyInAnyOrder("p01.md", "p02.md");
		assertThat(service.searchVector(query, 2, true)).hasSize(2)
				.allSatisfy(hit -> {
					assertThat(hit.sensitivity()).isEqualTo("company");
					assertThat(hit.score()).isCloseTo(1, within(1e-6f));
				});
	}

	@Test
	void capsCandidatesAtOneHundredAndAvoidsIntegerOverflow() throws Exception {
		for (int i = 0; i < 105; i++) {
			Files.writeString(vault.resolve("p" + i + ".md"), "본문 " + i);
		}
		indexer.index(vault);
		assertThat(service.searchVector("xyz", Integer.MAX_VALUE, false)).hasSize(100)
				.extracting(SearchHit::path).doesNotHaveDuplicates();
	}

	@Test
	void propagatesEmbeddingFailureEvenWhenBm25CanFindTheNote() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "임시저장 본문");
		indexer.index(vault);
		service = new SearchService(factory, settings, texts -> {
			throw new EmbeddingException("합성 질의 실패");
		});
		assertThat(service.searchNotes("임시저장", 5, false)).hasSize(1);
		assertThatThrownBy(() -> service.searchVector("임시저장", 5, false))
				.isInstanceOf(EmbeddingException.class).hasMessage("합성 질의 실패");
	}

	@Test
	void reportsMissingAndVectorlessIndexesAndHandlesEmptyVectorIndex() throws Exception {
		assertThatThrownBy(() -> service.searchVector("xyz", 5, false)).isInstanceOf(IOException.class)
				.hasMessageContaining("색인이 없습니다");
		assertThat(settings.getIndexDir()).doesNotExist();
		indexer.index(vault, false);
		assertThatThrownBy(() -> service.searchVector("xyz", 5, false)).isInstanceOf(IOException.class)
				.hasMessageContaining("--no-vector 없이 index");
		indexer.index(vault);
		assertThat(service.searchVector("xyz", 5, false)).isEmpty();
		assertThatThrownBy(() -> service.searchVector("xyz", 0, false)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> service.searchVector(null, 5, false)).isInstanceOf(IllegalArgumentException.class);
	}
}
