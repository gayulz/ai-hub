package dev.aihub.search.index;

import java.io.IOException;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.Field.Store;
import org.apache.lucene.document.KnnFloatVectorField;
import org.apache.lucene.document.StoredField;
import org.apache.lucene.document.StringField;
import org.apache.lucene.document.TextField;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.Term;
import org.apache.lucene.index.VectorSimilarityFunction;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.Directory;
import org.apache.lucene.store.FSDirectory;
import org.apache.lucene.store.LockObtainFailedException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.AnalyzerFactory;
import dev.aihub.search.chunk.Chunk;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.OllamaEmbedder;

@Service
public class NoteIndexer {

	public static final String COMMIT_TIME_KEY = "commit_time";

	// 빈 노트는 조각 문서가 없으므로 커밋 메타데이터에 해시만 보관한다.
	private static final String EMPTY_PREFIX = "empty:";
	private final VaultReader vaultReader;
	private final Chunker chunker;
	private final AnalyzerFactory analyzerFactory;
	private final SearchSettings settings;
	private final Embedder embedder;

	public NoteIndexer(VaultReader vaultReader, Chunker chunker, AnalyzerFactory analyzerFactory,
			SearchSettings settings) {
		this(vaultReader, chunker, analyzerFactory, settings, new OllamaEmbedder(settings));
	}

	@Autowired
	public NoteIndexer(VaultReader vaultReader, Chunker chunker, AnalyzerFactory analyzerFactory,
			SearchSettings settings, Embedder embedder) {
		this.vaultReader = vaultReader;
		this.chunker = chunker;
		this.analyzerFactory = analyzerFactory;
		this.settings = settings;
		this.embedder = embedder;
	}

	public IndexReport index(Path vaultRoot) throws IOException {
		return index(vaultRoot, true);
	}

	public IndexReport index(Path vaultRoot, boolean withVector) throws IOException {
		var configuration = IndexConfiguration.capture(analyzerFactory, settings);
		try (Directory directory = FSDirectory.open(settings.getIndexDir());
				Analyzer analyzer = analyzerFactory.analyzer();
				IndexWriter writer = new IndexWriter(directory, new IndexWriterConfig(analyzer)
						.setSimilarity(new BM25Similarity(settings.getBm25K1(), settings.getBm25B()))
						.setCommitOnClose(false))) {
			return update(vaultRoot, directory, writer, withVector, configuration);
		} catch (LockObtainFailedException exception) {
			throw new IOException("다른 색인 작업이 실행 중입니다. 끝난 뒤 다시 index 를 실행하세요", exception);
		}
	}

	private IndexReport update(Path root, Directory directory, IndexWriter writer, boolean withVector,
			IndexConfiguration configuration) throws IOException {
		Map<String, String> emptyNotes = new HashMap<>();
		Map<String, String> commitData = new HashMap<>();
		Map<String, String> previous = readHashes(directory, emptyNotes, commitData);
		var reasons = configuration.rebuildReasons(commitData, withVector);
		boolean rebuild = !reasons.isEmpty();
		var vault = vaultReader.read(root);
		if (rebuild && vault.unreadablePaths().stream().anyMatch(previous::containsKey)) {
			throw new IOException("기존 노트를 읽지 못해 색인 설정·스키마 전환을 중단합니다");
		}
		if (rebuild) {
			// 벡터 차원도 바꿀 수 있도록 이전 필드 정의를 함께 비운다.
			writer.deleteAll();
		}
		var present = new HashSet<>(vault.unreadablePaths());
		List<Chunk> pending = new ArrayList<>();
		int added = 0;
		int updated = 0;
		int unchanged = 0;
		for (var note : vault.notes()) {
			if (!present.add(note.path())) {
				throw new IOException("정규화한 노트 경로가 중복됩니다");
			}
			if (!rebuild && note.hash().equals(previous.get(note.path()))) {
				unchanged++;
				continue;
			}
			var chunks = chunker.chunk(note);
			if (!rebuild && previous.containsKey(note.path())) {
				writer.deleteDocuments(new Term("path", note.path()));
			}
			pending.addAll(chunks);
			emptyNotes.remove(note.path());
			if (chunks.isEmpty()) {
				emptyNotes.put(note.path(), note.hash());
			}
			if (previous.containsKey(note.path())) {
				updated++;
			} else {
				added++;
			}
		}
		List<float[]> vectors = withVector && !pending.isEmpty()
				? embedder.embed(pending.stream().map(chunk -> chunk.noteTitle() + "\n" + chunk.heading()
						+ "\n" + chunk.body()).toList()) : List.of();
		if (withVector && vectors.size() != pending.size()) {
			throw new EmbeddingException("임베딩 응답 개수가 조각 개수와 다릅니다");
		}
		for (int i = 0; i < pending.size(); i++) {
			float[] vector = withVector ? vectors.get(i) : null;
			if (withVector && (vector == null || vector.length != settings.getEmbeddingDimension())) {
				throw new EmbeddingException("임베딩 벡터 차원이 설정과 다릅니다");
			}
			writer.addDocument(document(pending.get(i), vector));
		}
		int deleted = deleteMissing(writer, previous, present, emptyNotes);
		if (!configuration.equals(IndexConfiguration.capture(analyzerFactory, settings))) {
			throw new IOException("색인 중 설정이 바뀌었습니다. index 를 다시 실행하세요");
		}
		if (added + updated + deleted > 0 || rebuild) {
			Map<String, String> metadata = new HashMap<>(commitData);
			metadata.keySet().removeIf(key -> key.startsWith(EMPTY_PREFIX));
			configuration.writeTo(metadata, withVector);
			if (rebuild) {
				metadata.put("reindex", String.join(",", reasons));
			}
			emptyNotes.forEach((path, hash) -> metadata.put(EMPTY_PREFIX + path, hash));
			metadata.put(COMMIT_TIME_KEY, Instant.now().toString());
			writer.setLiveCommitData(metadata.entrySet());
			writer.commit();
		}
		return new IndexReport(added, updated, deleted, unchanged, writer.getDocStats().numDocs,
				vault.skippedFiles(), vectors.size());
	}

	private int deleteMissing(IndexWriter writer, Map<String, String> previous, java.util.Set<String> present,
			Map<String, String> emptyNotes) throws IOException {
		int deleted = 0;
		for (String path : previous.keySet()) {
			if (!present.contains(path)) {
				writer.deleteDocuments(new Term("path", path));
				emptyNotes.remove(path);
				deleted++;
			}
		}
		return deleted;
	}

	private Map<String, String> readHashes(Directory directory, Map<String, String> emptyNotes,
			Map<String, String> commitData) throws IOException {
		Map<String, String> hashes = new HashMap<>();
		if (!DirectoryReader.indexExists(directory)) {
			return hashes;
		}
		try (var reader = DirectoryReader.open(directory)) {
			commitData.putAll(reader.getIndexCommit().getUserData());
			commitData.forEach((key, hash) -> {
				if (key.startsWith(EMPTY_PREFIX)) {
					emptyNotes.put(key.substring(EMPTY_PREFIX.length()), hash);
				}
			});
			hashes.putAll(emptyNotes);
			for (var leaf : reader.leaves()) {
				var liveDocs = leaf.reader().getLiveDocs();
				var fields = leaf.reader().storedFields();
				for (int docId = 0; docId < leaf.reader().maxDoc(); docId++) {
					if (liveDocs == null || liveDocs.get(docId)) {
						Document document = fields.document(docId);
						hashes.put(document.get("path"), document.get("hash"));
					}
				}
			}
		}
		return hashes;
	}

	private Document document(Chunk chunk, float[] vector) {
		Document document = new Document();
		document.add(new StringField("path", chunk.path(), Store.YES));
		document.add(new TextField("noteTitle", chunk.noteTitle(), Store.YES));
		document.add(new TextField("heading", chunk.heading(), Store.YES));
		document.add(new TextField("body", chunk.body(), Store.YES));
		addOptional(document, "type", chunk.type());
		addOptional(document, "sensitivity", chunk.sensitivity());
		addOptional(document, "created", chunk.created());
		for (String tag : chunk.tags()) {
			document.add(new StringField("tags", tag, Store.YES));
		}
		document.add(new StoredField("ordinal", chunk.ordinal()));
		document.add(new StringField("hash", chunk.hash(), Store.YES));
		if (vector != null) {
			document.add(new KnnFloatVectorField("vector", vector, VectorSimilarityFunction.COSINE));
		}
		return document;
	}

	private void addOptional(Document document, String name, String value) {
		if (value != null) {
			document.add(new StringField(name, value, Store.YES));
		}
	}
}
