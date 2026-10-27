package dev.aihub.search.index;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.query.SearchMode;
import dev.aihub.search.query.SearchService;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class IndexConfigurationTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private DefaultAnalyzerFactory factory;

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		Files.writeString(vault.resolve("p01.md"), "임시저장 본문");
		Files.writeString(vault.resolve("p02.md"), "");
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(4);
		factory = new DefaultAnalyzerFactory(settings);
	}

	@ParameterizedTest
	@ValueSource(strings = {"mode", "dictionary", "model", "dimension"})
	void rebuildsSameHashNotesForSettingsChangesAndRefusesSearchUntilThen(String change) throws Exception {
		indexer(fake()).index(vault);
		Snapshot before = snapshot();
		switch (change) {
			case "mode" -> settings.setDecompoundMode("MIXED");
			case "dictionary" -> settings.setUserDictionary(Files.writeString(temp.resolve("dictionary.txt"), ""));
			case "model" -> settings.setEmbeddingModel("xyz");
			case "dimension" -> settings.setEmbeddingDimension(8);
		}
		var service = new SearchService(factory, settings, texts -> {
			throw new AssertionError("설정 검사 전에 임베더를 호출했습니다");
		});
		for (SearchMode mode : SearchMode.values()) {
			assertThatThrownBy(() -> mode.search(service, "임시저장", 5, false)).isInstanceOf(IOException.class)
					.hasMessageContaining("재색인");
		}
		assertThat(indexer(fake()).index(vault)).isEqualTo(new IndexReport(0, 2, 0, 0, 1, 0, 1));
		Snapshot after = snapshot();
		assertThat(after.generation()).isGreaterThan(before.generation());
		assertThat(after.metadata()).containsEntry("schema", "2")
				.containsEntry("empty:p02.md", before.metadata().get("empty:p02.md"))
				.containsEntry("vector-model", settings.getEmbeddingModel())
				.containsEntry("vector-dimension", Integer.toString(settings.getEmbeddingDimension()));
		String reason = change.equals("mode") || change.equals("dictionary") ? "analysis" : "vector-" + change;
		assertThat(after.metadata().get("reindex")).contains(reason);
		assertThat(new SearchService(factory, settings, fake()).searchHybrid("임시저장", 5, false)).hasSize(1);
		assertThat(indexer(fake()).index(vault)).isEqualTo(new IndexReport(0, 0, 0, 2, 1, 0, 0));
		assertThat(snapshot()).isEqualTo(after);
	}

	@Test
	void hashesDictionaryContentRatherThanPathAndDetectsInPlaceEdits() throws Exception {
		Path dictionary = Files.writeString(temp.resolve("dictionary.txt"), "임시저장\n");
		settings.setUserDictionary(dictionary);
		indexer(fake()).index(vault);
		Snapshot before = snapshot();
		Path same = Files.writeString(temp.resolve("same.txt"), Files.readString(dictionary));
		settings.setUserDictionary(same);
		assertThat(indexer(fake()).index(vault).unchangedNotes()).isEqualTo(2);
		assertThat(snapshot()).isEqualTo(before);
		Files.writeString(same, "임시저장 임시 저장\n");
		assertThat(indexer(fake()).index(vault).updatedNotes()).isEqualTo(2);
		assertThat(snapshot().metadata().get("analysis")).isNotEqualTo(before.metadata().get("analysis"));
	}

	@Test
	void bm25SimilarityAndBoostChangesDoNotRequireReindexing() throws Exception {
		indexer(fake()).index(vault);
		Snapshot before = snapshot();
		settings.setBm25K1(0.9f);
		settings.setBm25B(0.2f);
		settings.setHeadingBoost(3);
		assertThat(indexer(texts -> {
			throw new AssertionError("BM25 조정으로 다시 임베딩했습니다");
		}).index(vault).unchangedNotes()).isEqualTo(2);
		assertThat(snapshot()).isEqualTo(before);
		assertThat(new SearchService(factory, settings, fake()).searchNotes("임시저장", 5, false)).hasSize(1);
	}

	@ParameterizedTest
	@ValueSource(booleans = {false, true})
	void migratesMissingMetadataInBothSchemasAndKeepsEmptyHashesAndOtherUserData(boolean vector) throws Exception {
		indexer(fake()).index(vault, vector);
		Map<String, String> old = new HashMap<>(snapshot().metadata());
		old.keySet().removeIf(key -> key.equals("analysis") || key.startsWith("vector-"));
		old.put("custom", "acme");
		rewriteMetadata(old);
		assertThatThrownBy(() -> new SearchService(factory, settings, fake()).searchNotes("임시저장", 5, false))
				.isInstanceOf(IOException.class).hasMessageContaining("재색인");
		assertThat(indexer(fake()).index(vault, vector).updatedNotes()).isEqualTo(2);
		assertThat(snapshot().metadata()).containsEntry("schema", vector ? "2" : "1")
				.containsEntry("custom", "acme").containsEntry("empty:p02.md", old.get("empty:p02.md"));
	}

	@Test
	void failedDimensionRebuildAndUnreadableMigrationPreservePreviousCommit() throws Exception {
		indexer(fake()).index(vault);
		Snapshot before = snapshot();
		settings.setEmbeddingDimension(8);
		assertThatThrownBy(() -> indexer(texts -> {
			throw new EmbeddingException("합성 실패");
		}).index(vault)).isInstanceOf(EmbeddingException.class);
		assertThat(snapshot()).isEqualTo(before);
		Files.write(vault.resolve("p01.md"), new byte[] {(byte) 0xc3, 0x28});
		assertThatThrownBy(() -> indexer(fake()).index(vault)).isInstanceOf(IOException.class)
				.hasMessageContaining("전환");
		assertThat(snapshot()).isEqualTo(before);
	}

	@Test
	void detectsSettingsChangesDuringEmbeddingBeforeCommitOrReturningSearchResults() throws Exception {
		indexer(fake()).index(vault);
		Snapshot before = snapshot();
		var originalFake = fake();
		var changed = new SearchService(factory, settings, texts -> {
			settings.setEmbeddingModel("xyz");
			return originalFake.embed(texts);
		});
		assertThatThrownBy(() -> changed.searchVector("임시저장", 5, false)).isInstanceOf(IOException.class)
				.hasMessageContaining("검색 중 설정");
		assertThat(snapshot()).isEqualTo(before);
		settings.setEmbeddingModel("bge-m3");
		Files.writeString(vault.resolve("p01.md"), "임시저장 다음 본문");
		assertThatThrownBy(() -> indexer(texts -> {
			settings.setEmbeddingModel("xyz");
			return originalFake.embed(texts);
		}).index(vault)).isInstanceOf(IOException.class).hasMessageContaining("색인 중 설정");
		assertThat(snapshot()).isEqualTo(before);
	}

	@ParameterizedTest
	@ValueSource(strings = {"vector-model", "vector-dimension"})
	void missingVectorMetadataAloneRequiresReindex(String key) throws Exception {
		indexer(fake()).index(vault);
		var metadata = new HashMap<>(snapshot().metadata());
		metadata.remove(key);
		rewriteMetadata(metadata);
		assertThatThrownBy(() -> new SearchService(factory, settings, fake()).searchVector("임시저장", 5, false))
				.isInstanceOf(IOException.class).hasMessageContaining("재색인");
		assertThat(indexer(fake()).index(vault).updatedNotes()).isEqualTo(2);
		assertThat(snapshot().metadata().get("reindex")).isEqualTo(key);
	}

	private Embedder fake() {
		return new FakeEmbedder(settings.getEmbeddingDimension());
	}

	private NoteIndexer indexer(Embedder embedder) {
		return new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, embedder);
	}

	private Snapshot snapshot() throws IOException {
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			var documents = new ArrayList<String>();
			for (var leaf : reader.leaves()) {
				var live = leaf.reader().getLiveDocs();
				for (int id = 0; id < leaf.reader().maxDoc(); id++) {
					if (live == null || live.get(id)) {
						documents.add(leaf.reader().storedFields().document(id).toString());
					}
				}
			}
			return new Snapshot(reader.getIndexCommit().getGeneration(), reader.getIndexCommit().getUserData(),
					List.copyOf(documents));
		}
	}

	private void rewriteMetadata(Map<String, String> metadata) throws IOException {
		try (var directory = FSDirectory.open(settings.getIndexDir()); var analyzer = factory.analyzer();
				var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
			writer.setLiveCommitData(metadata.entrySet());
			writer.commit();
		}
	}

	private record Snapshot(long generation, Map<String, String> metadata, List<String> documents) {
	}
}
