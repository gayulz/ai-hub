package dev.aihub.search.index;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.VectorSimilarityFunction;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.KnnFloatVectorQuery;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class VectorIndexTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private FakeEmbedder fake;
	private final DefaultAnalyzerFactory factory = new DefaultAnalyzerFactory();

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(16);
		fake = new FakeEmbedder(settings.getEmbeddingDimension());
	}

	@Test
	void embedsTitleHeadingAndBodyAndStoresCosineVectorsForEveryChunk() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "# xyz\n## h01\n본문");
		List<String> inputs = new ArrayList<>();
		var indexer = indexer(texts -> {
			inputs.addAll(texts);
			return fake.embed(texts);
		});
		assertThat(indexer.index(vault)).isEqualTo(new IndexReport(1, 0, 0, 0, 2, 0, 2));
		assertThat(inputs).containsExactly("xyz\n\n# xyz", "xyz\nh01\n## h01\n본문");
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			assertThat(reader.getIndexCommit().getUserData()).containsEntry("schema", "2");
			int vectors = 0;
			for (var leaf : reader.leaves()) {
				var info = leaf.reader().getFieldInfos().fieldInfo("vector");
				assertThat(info.getVectorSimilarityFunction()).isEqualTo(VectorSimilarityFunction.COSINE);
				assertThat(info.getVectorDimension()).isEqualTo(16);
				vectors += leaf.reader().getFloatVectorValues("vector").size();
			}
			assertThat(vectors).isEqualTo(reader.numDocs());
		}
	}

	@ParameterizedTest
	@ValueSource(strings = {"1", "", "0"})
	void rebuildsEveryNoteForOldSchemaThenDoesNoWorkOnNextRun(String schema) throws Exception {
		Files.writeString(vault.resolve("p01.md"), "본문");
		Files.writeString(vault.resolve("p02.md"), "다음 본문");
		Files.writeString(vault.resolve("p03.md"), "");
		var indexer = indexer(fake);
		indexer.index(vault, false);
		setSchema(schema);
		assertThat(indexer.index(vault)).isEqualTo(new IndexReport(0, 3, 0, 0, 2, 0, 2));
		Snapshot before = snapshot();
		assertThat(before.schema()).isEqualTo("2");
		assertThat(indexer(texts -> {
			throw new AssertionError("변경 없는 조각을 다시 임베딩했습니다");
		}).index(vault)).isEqualTo(new IndexReport(0, 0, 0, 3, 2, 0, 0));
		assertThat(snapshot()).isEqualTo(before);
	}

	@Test
	void embeddingFailureAfterSuccessfulBatchPreservesPreviousCommitAndDocuments() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "이전 본문");
		Files.writeString(vault.resolve("p02.md"), "삭제 전 본문");
		indexer(fake).index(vault);
		Snapshot before = snapshot();
		Files.writeString(vault.resolve("p01.md"), "바뀐 본문");
		Files.delete(vault.resolve("p02.md"));
		Files.writeString(vault.resolve("p03.md"), "추가 본문");
		var failing = indexer(texts -> {
			assertThat(texts).hasSize(2);
			fake.embed(texts.subList(0, 1));
			throw new EmbeddingException("합성 배치 실패");
		});
		assertThatThrownBy(() -> failing.index(vault)).isInstanceOf(EmbeddingException.class);
		assertThat(snapshot()).isEqualTo(before);
		assertThat(indexer(fake).index(vault)).isEqualTo(new IndexReport(1, 1, 1, 0, 2, 0, 2));
	}

	@Test
	void failedFirstEmbeddingLeavesNoCommittedIndex() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "본문");
		assertThatThrownBy(() -> indexer(texts -> {
			throw new EmbeddingException("합성 실패");
		}).index(vault)).isInstanceOf(EmbeddingException.class);
		try (var directory = FSDirectory.open(settings.getIndexDir())) {
			assertThat(DirectoryReader.indexExists(directory)).isFalse();
		}
		assertThat(indexer(fake).index(vault).vectorChunks()).isEqualTo(1);
	}

	@Test
	void noVectorNeverCallsEmbedderAndWritesSchemaOneEvenAfterVectorIndex() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "본문");
		var noEmbedding = indexer(texts -> {
			throw new AssertionError("벡터 없는 색인에서 임베더를 호출했습니다");
		});
		assertThat(noEmbedding.index(vault, false)).isEqualTo(new IndexReport(1, 0, 0, 0, 1, 0, 0));
		assertThat(snapshot().schema()).isEqualTo("1");
		indexer(fake).index(vault);
		assertThat(noEmbedding.index(vault, false)).isEqualTo(new IndexReport(0, 1, 0, 0, 1, 0, 0));
		assertThat(snapshot().schema()).isEqualTo("1");
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			var vector = fake.embed(List.of("본문")).getFirst();
			assertThat(new IndexSearcher(reader).search(new KnnFloatVectorQuery("vector", vector, 5), 5).scoreDocs)
					.isEmpty();
		}
	}

	@Test
	void refusesSchemaUpgradeWhenAnExistingNoteCannotBeRead() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "본문");
		indexer(fake).index(vault, false);
		Snapshot before = snapshot();
		Files.write(vault.resolve("p01.md"), new byte[] {(byte) 0xc3, 0x28});
		assertThatThrownBy(() -> indexer(fake).index(vault)).isInstanceOf(IOException.class)
				.hasMessageContaining("스키마 전환");
		assertThat(snapshot()).isEqualTo(before);
	}

	@Test
	void badVectorCountOrDimensionDoesNotCommitPendingChanges() throws Exception {
		Files.writeString(vault.resolve("p01.md"), "본문");
		indexer(fake).index(vault);
		Snapshot before = snapshot();
		Files.writeString(vault.resolve("p01.md"), "다음 본문");
		for (List<float[]> vectors : List.of(List.<float[]>of(), List.of(new float[] {1, 0}))) {
			assertThatThrownBy(() -> indexer(texts -> vectors).index(vault)).isInstanceOf(EmbeddingException.class);
			assertThat(snapshot()).isEqualTo(before);
		}
	}

	private NoteIndexer indexer(Embedder embedder) {
		return new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, embedder);
	}

	private Snapshot snapshot() throws IOException {
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			List<String> documents = new ArrayList<>();
			for (var leaf : reader.leaves()) {
				var live = leaf.reader().getLiveDocs();
				for (int id = 0; id < leaf.reader().maxDoc(); id++) {
					if (live == null || live.get(id)) {
						documents.add(leaf.reader().storedFields().document(id).toString());
					}
				}
			}
			return new Snapshot(reader.getIndexCommit().getGeneration(), reader.getVersion(),
					reader.getIndexCommit().getUserData().get("schema"), List.copyOf(documents));
		}
	}

	private void setSchema(String schema) throws IOException {
		try (var directory = FSDirectory.open(settings.getIndexDir()); var analyzer = factory.analyzer()) {
			var metadata = new HashMap<String, String>();
			try (var reader = DirectoryReader.open(directory)) {
				metadata.putAll(reader.getIndexCommit().getUserData());
			}
			if (schema.isEmpty()) {
				metadata.remove("schema");
			} else {
				metadata.put("schema", schema);
			}
			try (var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
				writer.setLiveCommitData(metadata.entrySet());
				writer.commit();
			}
		}
	}

	private record Snapshot(long generation, long version, String schema, List<String> documents) {
	}
}
