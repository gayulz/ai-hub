package dev.aihub.search.cli;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.io.StringWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.HashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.List;

import org.apache.lucene.analysis.standard.StandardAnalyzer;
import org.apache.lucene.document.Document;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchService;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class EvalCommandTest {

	@TempDir
	Path temp;
	private SearchSettings settings;
	private CommandRouter router;
	private Path source;
	private Path log;

	@BeforeEach
	void setUp() throws IOException {
		var vault = Files.createDirectory(temp.resolve("vault"));
		Files.writeString(vault.resolve("p01.md"), "임시저장 일반 본문");
		Files.writeString(vault.resolve("p02.md"), "---\nsensitivity: company\n---\n임시저장");
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(4);
		var fake = new FakeEmbedder(4);
		var factory = new DefaultAnalyzerFactory(settings);
		new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, fake).index(vault);
		router = router(fake);
		source = Files.writeString(temp.resolve("eval.toml"), query(false, "p01.md"));
		log = temp.resolve("scores.md");
	}

	@Test
	void evaluatesEachModeWithoutEchoingQueriesOrPathsAndKeepsOriginalAnswers() throws IOException {
		String original = Files.readString(source);
		for (String mode : List.of("bm25", "vector", "hybrid")) {
			var result = run("eval", "--set", source.toString(), "--mode", mode, "--k", "5", "--log", log.toString());
			assertThat(result.code()).isZero();
			assertThat(result.out()).contains("모드 " + mode, "Hit@5 (recallAt5) 1.000000", "MRR@5 1.000000");
			assertThat(result.err()).isEmpty();
			assertThat(result.out() + Files.readString(log)).doesNotContain("임시저장", "p01.md", temp.toString());
			assertThat(Files.readString(source)).isEqualTo(original);
		}
		Files.writeString(source, query(false, "p02.md"));
		assertThat(run("eval", "--set", source.toString(), "--log", log.toString()).out())
				.contains("Hit@5 (recallAt5) 0.000000");
		Files.writeString(source, query(true, "p02.md"));
		assertThat(run("eval", "--set", source.toString(), "--log", log.toString()).out())
				.contains("Hit@5 (recallAt5) 1.000000");
	}

	@Test
	void usesDefaultSetAndLogWithinAnIsolatedHome() throws IOException {
		String previousHome = System.getProperty("user.home");
		try {
			System.setProperty("user.home", temp.toString());
			Path home = Files.createDirectory(temp.resolve(".ai-hub"));
			Files.copy(source, home.resolve("search-eval.toml"));
			assertThat(run("eval").code()).isZero();
			assertThat(Files.readString(home.resolve("search-eval-log.md"))).contains("모드: bm25");
		} finally {
			System.setProperty("user.home", previousHome);
		}
	}

	@Test
	void failsWithoutPartialScoresOrLogsWhenVectorOrLogWriteFails() throws IOException {
		router = router(texts -> {
			throw new EmbeddingException("acme-private-query p01.md");
		});
		var failure = run("eval", "--set", source.toString(), "--mode", "hybrid", "--log", log.toString());
		assertThat(failure.code()).isEqualTo(1);
		assertThat(failure.out()).isEmpty();
		assertThat(failure.err()).doesNotContain("acme-private-query", "p01.md", temp.toString(), "사용법:");
		assertThat(log).doesNotExist();
		var logFailure = run("eval", "--set", source.toString(), "--log", temp.toString());
		assertThat(logFailure.code()).isEqualTo(1);
		assertThat(logFailure.out()).isEmpty();
		assertThat(logFailure.err()).doesNotContain(temp.toString());
	}

	@Test
	void rejectsBadOptionsInvalidTomlAndSourceAsLogWithoutChangingFiles() throws IOException {
		for (String[] args : List.of(new String[] {"eval", "--set"}, new String[] {"eval", "--mode", "xyz"},
				new String[] {"eval", "--k", "0"}, new String[] {"eval", "--k", "1", "--k", "2"},
				new String[] {"eval", "--log", ""}, new String[] {"eval", "--set", "--mode"})) {
			assertThat(run(args).code()).isEqualTo(2);
		}
		String original = Files.readString(source);
		assertThat(run("eval", "--set", source.toString(), "--log", source.toString()).code()).isEqualTo(2);
		Path alias = Files.createLink(temp.resolve("alias.md"), source);
		assertThat(run("eval", "--set", source.toString(), "--log", alias.toString()).code()).isEqualTo(2);
		assertThat(Files.readString(source)).isEqualTo(original);
		Files.writeString(source, "[[query]]\nid='q1'\ntext='acme-private-query'\ncompany=false\nanswers=[]\n");
		var invalid = run("eval", "--set", source.toString(), "--log", log.toString());
		assertThat(invalid.code()).isEqualTo(2);
		assertThat(invalid.err()).contains("평가 TOML").doesNotContain("acme-private-query", temp.toString());
		assertThat(log).doesNotExist();
		assertThat(run("eval", "--set", temp.resolve("missing.toml").toString(), "--log", log.toString()).code())
				.isEqualTo(1);
	}

	@Test
	void appendsConditionsAndOrdinalsWithoutRewritingExistingLog() throws Exception {
		String queries = "[[query]]\nid='private-one'\ntext='임시저장'\ncompany=false\nanswers=['p01.md']\n"
				+ "[[query]]\nid='private-two'\ntext='임시저장'\ncompany=false\nanswers=['p01.md']\n";
		Files.writeString(source, queries);
		byte[] old = "# 기존 점수\n\n기존 내용\n".getBytes(StandardCharsets.UTF_8);
		Files.write(log, old);
		String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(source)))
				.substring(0, 12);
		for (String mode : List.of("bm25", "vector", "hybrid")) {
			var result = run("eval", "--set", source.toString(), "--mode", mode, "--log", log.toString());
			assertThat(result.code()).isZero();
			assertThat(result.out()).contains("private-one", "private-two");
		}
		byte[] now = Files.readAllBytes(log);
		assertThat(now).startsWith(old);
		String text = new String(now, StandardCharsets.UTF_8);
		String committed;
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			committed = reader.getIndexCommit().getUserData().get(NoteIndexer.COMMIT_TIME_KEY);
		}
		assertThat(text).contains(hash, "실행 파일", "색인 커밋 시각(UTC): " + committed, "| 1 |", "| 2 |", "bm25", "vector", "hybrid")
				.doesNotContain("private-one", "private-two", "임시저장", "p01.md", source.getFileName().toString(), temp.toString());
	}

	@Test
	void evaluatesOldIndexWithUnknownTimeAndPreservesExistingLog() throws Exception {
		try (var directory = FSDirectory.open(settings.getIndexDir());
				var reader = DirectoryReader.open(directory); var analyzer = new StandardAnalyzer()) {
			var data = new HashMap<>(reader.getIndexCommit().getUserData());
			data.remove(NoteIndexer.COMMIT_TIME_KEY);
			try (var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
				writer.setLiveCommitData(data.entrySet());
				writer.commit();
			}
		}
		byte[] previous = "# 기존 기록\n한글 점수\n".getBytes(StandardCharsets.UTF_8);
		Files.write(log, previous);
		assertThat(run("eval", "--set", source.toString(), "--log", log.toString()).code()).isZero();
		byte[] result = Files.readAllBytes(log);
		assertThat(result).startsWith(previous);
		assertThat(new String(result, StandardCharsets.UTF_8)).contains("색인 커밋 시각(UTC): 미확인");
	}

	@Test
	void doesNotAppendOrPrintScoresWhenTheIndexCommitChanges() throws Exception {
		byte[] previous = "# 기존 점수\n".getBytes(StandardCharsets.UTF_8);
		Files.write(log, previous);
		var fake = new FakeEmbedder(4);
		var changed = new AtomicBoolean();
		router = router(texts -> {
			if (changed.compareAndSet(false, true)) {
				try (var directory = FSDirectory.open(settings.getIndexDir()); var analyzer = new StandardAnalyzer();
						var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
					writer.addDocument(new Document());
					writer.commit();
				} catch (IOException exception) {
					throw new EmbeddingException("합성 색인 갱신 실패");
				}
			}
			return fake.embed(texts);
		});
		var result = run("eval", "--set", source.toString(), "--mode", "vector", "--log", log.toString());
		assertThat(changed).isTrue();
		assertThat(result.code()).isEqualTo(1);
		assertThat(result.out()).isEmpty();
		assertThat(result.err()).doesNotContain(temp.toString(), source.getFileName().toString());
		assertThat(Files.readAllBytes(log)).isEqualTo(previous);
	}

	@Test
	void missingIndexInformationDoesNotCreateAnIndexOrAppendScores() throws Exception {
		byte[] previous = "# 기존 점수\n".getBytes(StandardCharsets.UTF_8);
		Files.write(log, previous);
		Path missing = temp.resolve("missing-synthetic-index");
		settings.setIndexDir(missing);
		var result = run("eval", "--set", source.toString(), "--log", log.toString());
		assertThat(result.code()).isEqualTo(1);
		assertThat(result.out()).isEmpty();
		assertThat(result.err()).doesNotContain(temp.toString(), missing.getFileName().toString());
		assertThat(missing).doesNotExist();
		assertThat(Files.readAllBytes(log)).isEqualTo(previous);
	}

	private String query(boolean company, String answer) {
		return "[[query]]\nid='q1'\ntext='임시저장'\ncompany=" + company + "\nanswers=['" + answer + "']\n";
	}

	private CommandRouter router(Embedder embedder) {
		var factory = new DefaultAnalyzerFactory(settings);
		return new CommandRouter(new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, embedder),
				new SearchService(factory, settings, embedder), settings);
	}

	private Result run(String... args) {
		var out = new StringWriter();
		var err = new StringWriter();
		int code = router.run(args, new PrintWriter(out), new PrintWriter(err));
		return new Result(code, out.toString(), err.toString());
	}

	private record Result(int code, String out, String err) {
	}
}
