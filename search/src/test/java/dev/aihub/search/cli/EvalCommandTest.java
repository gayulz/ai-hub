package dev.aihub.search.cli;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

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
