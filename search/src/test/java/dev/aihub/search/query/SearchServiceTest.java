package dev.aihub.search.query;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.search.BooleanClause.Occur;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.BoostQuery;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.AnalyzerFactory;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.vault.VaultReader;

class SearchServiceTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private SearchService service;
	private NoteIndexer indexer;
	private final DefaultAnalyzerFactory factory = new DefaultAnalyzerFactory();

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		service = new SearchService(factory, settings);
		indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings);
	}

	@Test
	void excludesCompanyChunksByDefaultAndIncludesThemOnlyWhenRequested() throws Exception {
		write("p01.md", "---\nsensitivity: company\n---\n# 임시저장\n임시저장 본문");
		indexer.index(vault);
		assertThat(service.searchChunks("임시저장", 5, false)).isEmpty();
		assertThat(service.searchNotes("임시저장", 5, false)).isEmpty();
		assertThat(service.searchChunks("임시저장", 5, true)).singleElement()
				.satisfies(hit -> assertThat(hit.sensitivity()).isEqualTo("company"));
		assertThat(service.searchNotes("임시저장", 5, true)).extracting(SearchHit::path).containsExactly("p01.md");
	}

	@Test
	void excludesCompanyNotesWithUnknownFrontMatterAndContinuationLines() throws Exception {
		write("p01.md", "---\nsensitivity: company\naliases:\n  - acme\n작성자: xyz\n"
				+ "description: >\n  문장: 설명\n  sensitivity: public\n  tags: [acme\n"
				+ "깨진 줄\n---\n# 임시저장\n임시저장 본문");
		indexer.index(vault);
		assertThat(service.searchChunks("임시저장", 5, false)).isEmpty();
		assertThat(service.searchNotes("임시저장", 5, false)).isEmpty();
		assertThat(service.searchChunks("임시저장", 5, true)).singleElement()
				.satisfies(hit -> assertThat(hit.sensitivity()).isEqualTo("company"));
		assertThat(service.searchNotes("임시저장", 5, true)).extracting(SearchHit::path).containsExactly("p01.md");
	}

	@Test
	void excludesNotesWhoseHeaderCannotBeReadFromDefaultSearch() throws Exception {
		write("p01.md", "---\nsensitivity: company\ntags: [acme, 'xyz]\n---\n# 임시저장\n임시저장 본문");
		indexer.index(vault);
		assertThat(service.searchChunks("임시저장", 5, false)).isEmpty();
		assertThat(service.searchNotes("임시저장", 5, false)).isEmpty();
		assertThat(service.searchNotes("임시저장", 5, true)).extracting(SearchHit::path).containsExactly("p01.md");
	}

	@Test
	void appliesCompanyFilterInsideQuerySoCompanyHitsDoNotConsumeK() throws Exception {
		for (int i = 0; i < 8; i++) {
			write("acme/p" + i + ".md", "---\nsensitivity: company\n---\n# 임시저장\n## 임시저장\n임시저장");
		}
		write("p01.md", "임시저장 " + "일반 본문 ".repeat(30));
		write("p02.md", "임시저장 " + "다음 본문 ".repeat(30));
		indexer.index(vault);
		assertThat(service.searchChunks("임시저장", 2, false)).hasSize(2)
				.allSatisfy(hit -> assertThat(hit.sensitivity()).isNull());
		assertThat(service.searchNotes("임시저장", 2, false)).hasSize(2)
				.extracting(SearchHit::path).containsExactlyInAnyOrder("p01.md", "p02.md");
		try (var analyzer = factory.analyzer()) {
			var query = (BooleanQuery) service.buildQuery("임시저장", false, analyzer);
			assertThat(query.clauses().stream().filter(clause -> clause.occur() == Occur.MUST_NOT).toList())
					.singleElement().satisfies(clause -> {
						var term = ((TermQuery) clause.query()).getTerm();
						assertThat(term.field()).isEqualTo("sensitivity");
						assertThat(term.text()).isEqualTo("company");
					});
		}
	}

	@Test
	void findsExpectedKoreanNotesWithDefaultNori() throws Exception {
		write("p01.md", "# 임시저장\n임시저장 기능을 시험한다");
		write("p02.md", "# 스프링부트\n스프링부트 설정을 시험한다");
		write("p03.md", "# xyz\n다른 낱말을 기록한다");
		indexer.index(vault);
		assertThat(service.searchNotes("임시저장", 5, false).getFirst().path()).isEqualTo("p01.md");
		assertThat(service.searchNotes("스프링부트", 5, false).getFirst().path()).isEqualTo("p02.md");
	}

	@Test
	void returnsBestChunkPerNoteInScoreOrderAndPagesPastDuplicateChunks() throws Exception {
		String sections = IntStream.range(0, 70).mapToObj(i -> "## 소제목 " + i + "\n임시저장")
				.collect(Collectors.joining("\n"));
		write("p01.md", "# 임시저장\n" + sections);
		write("p02.md", "임시저장 " + "일반 본문 ".repeat(100));
		indexer.index(vault);
		List<SearchHit> chunks = service.searchChunks("임시저장", 100, false);
		List<SearchHit> notes = service.searchNotes("임시저장", 2, false);
		assertThat(chunks.subList(0, 32)).allSatisfy(hit -> assertThat(hit.path()).isEqualTo("p01.md"));
		assertThat(notes).extracting(SearchHit::path).containsExactly("p01.md", "p02.md");
		assertThat(notes).extracting(SearchHit::score).isSortedAccordingTo(java.util.Comparator.reverseOrder());
		for (SearchHit hit : notes) {
			float best = chunks.stream().filter(chunk -> chunk.path().equals(hit.path())).map(SearchHit::score)
					.max(Float::compare).orElseThrow();
			assertThat(hit.score()).isEqualTo(best);
		}
	}

	@Test
	void producesSingleLineSnippetOfAtMost120CodePoints() throws Exception {
		String symbol = new String(Character.toChars(0x1d11e));
		write("p01.md", "임시저장\n" + symbol.repeat(200) + "\n다음");
		indexer.index(vault);
		SearchHit hit = service.searchChunks("임시저장", 1, false).getFirst();
		assertThat(hit.snippet()).startsWith("임시저장 ").doesNotContain("\n", "\r");
		assertThat(hit.snippet().codePointCount(0, hit.snippet().length())).isEqualTo(120);
		assertThat(hit.snippet().codePoints().allMatch(cp -> cp < 0xd800 || cp > 0xdfff)).isTrue();
	}

	@Test
	void reportsMissingIndexAndReturnsEmptyResultsForTokenlessQueriesOrEmptyIndex() throws Exception {
		assertThatThrownBy(() -> service.searchNotes("임시저장", 5, false)).isInstanceOf(IOException.class)
				.hasMessage("색인이 없습니다. 먼저 index 를 실행하세요");
		assertThat(settings.getIndexDir()).doesNotExist();
		Files.createDirectory(settings.getIndexDir());
		assertThatThrownBy(() -> service.searchChunks("임시저장", 5, false)).isInstanceOf(IOException.class)
				.hasMessage("색인이 없습니다. 먼저 index 를 실행하세요");
		indexer.index(vault);
		assertThat(service.searchNotes("임시저장", 5, false)).isEmpty();
		write("p01.md", "임시저장");
		indexer.index(vault);
		assertThat(service.searchChunks("!!!", 5, true)).isEmpty();
		assertThat(service.searchNotes(" ", 5, false)).isEmpty();
		assertThatThrownBy(() -> service.searchNotes("임시저장", 0, false))
				.isInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void readsFieldBoostsAndBm25FromSharedSettings() throws Exception {
		settings.setBodyBoost(3.0f);
		settings.setHeadingBoost(4.0f);
		settings.setNoteTitleBoost(5.0f);
		settings.setBm25K1(0.9f);
		settings.setBm25B(0.2f);
		write("p01.md", "# 임시저장\n## 기능\n임시저장 ".repeat(2));
		write("p02.md", "임시저장 " + "일반 본문 ".repeat(30));
		indexer.index(vault);
		try (var analyzer = factory.analyzer(); var directory = FSDirectory.open(settings.getIndexDir());
				var reader = DirectoryReader.open(directory)) {
			var query = (BooleanQuery) service.buildQuery("임시저장", true, analyzer);
			assertThat(query.clauses()).allSatisfy(clause -> assertThat(clause.occur()).isEqualTo(Occur.SHOULD));
			assertThat(query.clauses()).extracting(clause -> ((BoostQuery) clause.query()).getBoost())
					.containsExactly(3.0f, 4.0f, 5.0f);
			var searcher = new IndexSearcher(reader);
			searcher.setSimilarity(new BM25Similarity(settings.getBm25K1(), settings.getBm25B()));
			var expected = searcher.search(query, 1).scoreDocs[0];
			var actual = service.searchChunks("임시저장", 1, true).getFirst();
			assertThat(actual.path()).isEqualTo(searcher.storedFields().document(expected.doc).get("path"));
			assertThat(actual.score()).isCloseTo(expected.score, within(1e-6f));
		}
	}

	@Test
	void bothIndexAndQueryUseAnalyzerFactory() throws Exception {
		int[] calls = {0};
		AnalyzerFactory tracked = () -> {
			calls[0]++;
			return factory.analyzer();
		};
		var trackedIndexer = new NoteIndexer(new VaultReader(), new Chunker(settings), tracked, settings);
		write("p01.md", "임시저장");
		trackedIndexer.index(vault);
		assertThat(new SearchService(tracked, settings).searchNotes("임시저장", 1, false)).hasSize(1);
		assertThat(calls[0]).isEqualTo(2);
	}

	private void write(String path, String text) throws IOException {
		Path file = vault.resolve(path);
		Files.createDirectories(file.getParent());
		Files.writeString(file, text);
	}
}
