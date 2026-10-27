package dev.aihub.search.index;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import org.apache.lucene.document.Document;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.Term;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunk;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.vault.Note;
import dev.aihub.search.vault.VaultReader;

class NoteIndexerTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private NoteIndexer indexer;
	private final DefaultAnalyzerFactory factory = new DefaultAnalyzerFactory();

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings);
	}

	@Test
	void addsChunksAndLeavesSameHashDocumentsAndCommitUntouched() throws Exception {
		write("acme/p01.md", "# p01\n도입\n## 소제목\n본문");
		write("p02.md", "본문");
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(2, 0, 0, 0, 3, 0));
		long generation;
		long version;
		List<Document> before;
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			generation = reader.getIndexCommit().getGeneration();
			version = reader.getVersion();
			before = List.of(reader.storedFields().document(0), reader.storedFields().document(1),
					reader.storedFields().document(2));
		}
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 0, 2, 3, 0));
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			assertThat(reader.getIndexCommit().getGeneration()).isEqualTo(generation);
			assertThat(reader.getVersion()).isEqualTo(version);
			for (int i = 0; i < before.size(); i++) {
				assertThat(reader.storedFields().document(i).toString()).isEqualTo(before.get(i).toString());
			}
		}
	}

	@Test
	void handlesAddModifyDeleteAndUnchangedNotesInOneRun() throws Exception {
		List<String> chunked = new ArrayList<>();
		var trackedChunker = new Chunker(settings) {
			@Override
			public List<Chunk> chunk(Note note) {
				chunked.add(note.path());
				return super.chunk(note);
			}
		};
		indexer = new NoteIndexer(new VaultReader(), trackedChunker, factory, settings);
		write("p01.md", "oldxyz\n## 이전\noldxyz");
		write("p02.md", "삭제할 본문");
		write("p03.md", "그대로인 본문");
		indexer.index(vault, false);
		chunked.clear();
		write("p01.md", "newxyz\n## 다음\nnewxyz");
		Files.delete(vault.resolve("p02.md"));
		write("p04.md", "추가 본문");

		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(1, 1, 1, 1, 4, 0));
		assertThat(chunked).containsExactly("p01.md", "p04.md");

		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			var searcher = new IndexSearcher(reader);
			assertThat(reader.numDocs()).isEqualTo(4);
			assertThat(searcher.count(new TermQuery(new Term("body", "oldxyz")))).isZero();
			assertThat(searcher.count(new TermQuery(new Term("body", "newxyz")))).isEqualTo(2);
			assertThat(searcher.count(new TermQuery(new Term("path", "p02.md")))).isZero();
		}
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 0, 3, 4, 0));
	}

	@Test
	void storesEverySpecifiedFieldAndOmitsNullMetadata() throws Exception {
		write("p01.md", "---\ntype: note\nsensitivity: company\ncreated: 2026-01-01\n"
				+ "tags: [acme, xyz]\n---\n# 제목\n## 소제목\n본문");
		write("p02.md", "본문");
		indexer.index(vault, false);
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			var searcher = new IndexSearcher(reader);
			var docs = searcher.search(new TermQuery(new Term("path", "p01.md")), 10).scoreDocs;
			assertThat(docs).hasSize(2);
			Document document = searcher.storedFields().document(docs[1].doc);
			assertThat(document.get("path")).isEqualTo("p01.md");
			assertThat(document.get("noteTitle")).isEqualTo("제목");
			assertThat(document.get("heading")).isEqualTo("소제목");
			assertThat(document.get("body")).contains("본문");
			assertThat(document.get("type")).isEqualTo("note");
			assertThat(document.get("sensitivity")).isEqualTo("company");
			assertThat(document.get("created")).isEqualTo("2026-01-01");
			assertThat(document.getValues("tags")).containsExactly("acme", "xyz");
			assertThat(document.getField("ordinal").numericValue().intValue()).isEqualTo(1);
			assertThat(document.get("hash")).matches("[0-9a-f]{64}");
			int id = searcher.search(new TermQuery(new Term("path", "p02.md")), 1).scoreDocs[0].doc;
			Document plain = searcher.storedFields().document(id);
			assertThat(plain.getField("type")).isNull();
			assertThat(plain.getField("sensitivity")).isNull();
			assertThat(plain.getField("created")).isNull();
			assertThat(plain.getValues("tags")).isEmpty();
		}
	}

	@Test
	void headerOnlyChangeUpdatesNoteAndReadFailurePreservesExistingChunks() throws Exception {
		write("p01.md", "---\nsensitivity: personal\n---\n본문");
		indexer.index(vault, false);
		write("p01.md", "---\nsensitivity: company\n---\n본문");
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 1, 0, 0, 1, 0));
		Files.write(vault.resolve("p01.md"), new byte[] {(byte) 0xc3, 0x28});
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 0, 0, 1, 1));
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			var searcher = new IndexSearcher(reader);
			int id = searcher.search(new TermQuery(new Term("path", "p01.md")), 1).scoreDocs[0].doc;
			assertThat(reader.storedFields().document(id).get("sensitivity")).isEqualTo("company");
		}
	}

	@Test
	void tracksEmptyNotesWithoutCreatingEmptyChunkDocuments() throws Exception {
		write("p01.md", "\n\n");
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(1, 0, 0, 0, 0, 0));
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 0, 1, 0, 0));
		write("p01.md", "본문");
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 1, 0, 0, 1, 0));
		write("p01.md", "");
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 1, 0, 0, 0, 0));
		Files.delete(vault.resolve("p01.md"));
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 1, 0, 0, 0));
	}

	@Test
	void usesLuceneWriterLockAndReportsConflictInKorean() throws Exception {
		try (var directory = FSDirectory.open(settings.getIndexDir()); var analyzer = factory.analyzer();
				var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
			assertThat(writer.isOpen()).isTrue();
			assertThatThrownBy(() -> indexer.index(vault, false)).isInstanceOf(IOException.class)
					.hasMessageContaining("다른 색인 작업이 실행 중입니다");
		}
	}

	@Test
	void failedTraversalDoesNotCommitOrDeletePreviousNotes() throws Exception {
		write("p01.md", "본문");
		indexer.index(vault, false);
		assertThatThrownBy(() -> indexer.index(temp.resolve("missing"), false)).isInstanceOf(IOException.class);
		assertThat(indexer.index(vault, false)).isEqualTo(new IndexReport(0, 0, 0, 1, 1, 0));
	}

	private void write(String path, String text) throws IOException {
		Path file = vault.resolve(path);
		Files.createDirectories(file.getParent());
		Files.writeString(file, text);
	}
}
