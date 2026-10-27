package dev.aihub.search.cli;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.DefaultApplicationArguments;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchService;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class CommandRouterTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private CommandRouter router;
	private final DefaultAnalyzerFactory factory = new DefaultAnalyzerFactory();

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		Files.writeString(vault.resolve("p01.md"), "임시저장 일반 본문");
		Files.writeString(vault.resolve("p02.md"), "임시저장 다음 본문");
		Files.writeString(vault.resolve("p03.md"), "---\nsensitivity: company\n---\n# 임시저장\n임시저장");
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		router = router(new FakeEmbedder(settings.getEmbeddingDimension()));
	}

	@Test
	void indexesExplicitVaultAndPrintsSingleReportLine() {
		settings.setVault(temp.resolve("missing"));
		Result result = run("index", "--search.vault=" + vault);
		assertThat(result.code()).isZero();
		assertThat(result.out()).isEqualTo("추가 3 · 수정 0 · 삭제 0 · 변경 없음 0 · 조각 3 · 건너뜀 0 · 벡터 생성 3\n");
		assertThat(result.err()).isEmpty();
	}

	@Test
	void usesConfiguredVaultAndReportsMissingVaultAsUsageError() {
		Result missing = run("index");
		assertThat(missing.code()).isEqualTo(2);
		assertThat(missing.err()).contains("볼트 경로를 --search.vault=... 로 주세요", "사용법:");
		settings.setVault(vault);
		assertThat(run("index").code()).isZero();
		assertThat(run("index").out()).contains("추가 0 · 수정 0 · 삭제 0 · 변경 없음 3");
		assertThat(run("index", "--search.vault=").code()).isEqualTo(2);
	}

	@Test
	void queriesNotesWithDefaultsAndExplicitKAndCompanyFlag() {
		assertThat(run("index", "--search.vault=" + vault).code()).isZero();
		Result defaults = run("query", "임시저장");
		assertThat(defaults.code()).isZero();
		assertThat(defaults.out().lines().toList()).hasSize(2);
		assertThat(defaults.out()).doesNotContain("p03.md");
		assertThat(defaults.err()).isEmpty();
		assertThat(run("query", "임시저장", "--k", "1").out().lines().toList()).hasSize(1);
		Result company = run("query", "임시저장", "--include-company", "--k", "3");
		assertThat(company.code()).isZero();
		assertThat(company.out().lines().toList()).hasSize(3);
		assertThat(company.out()).contains("p03.md");
		assertThat(company.out().lines().toList()).allSatisfy(line ->
				assertThat(line).matches("[1-3] · [0-9]+\\.[0-9]{3} · p0[1-3]\\.md ·  · .+"));
	}

	@Test
	void evaluatesSyntheticSetAndPropagatesRunnerExitCode() throws Exception {
		assertThat(run("index", "--search.vault=" + vault).code()).isZero();
		Path set = Files.writeString(temp.resolve("eval.toml"), "[[query]]\nid='q1'\ntext='임시저장'\n"
				+ "company=false\nanswers=['p01.md','p02.md']\n");
		Path log = temp.resolve("scores.md");
		String[] args = {"eval", "--set", set.toString(), "--log", log.toString()};
		Result result = run(args);
		assertThat(result.code()).isZero();
		assertThat(result.out()).contains("Hit@5 (recallAt5) 1.000000", "MRR@5 1.000000", "q1 · 적중 1");
		assertThat(result.out()).doesNotContain("임시저장", "p01.md", temp.toString());
		assertThat(result.err()).isEmpty();
		var runner = new SearchCommandLine(router);
		runner.run(new DefaultApplicationArguments(args));
		assertThat(runner.getExitCode()).isZero();
	}

	@Test
	void rejectsUnknownCommandsArgumentsDuplicateFlagsAndInvalidKBeforeDoingWork() {
		List<String[]> cases = List.of(new String[] {}, new String[] {"xyz"}, new String[] {"query"},
				new String[] {"query", " "}, new String[] {"query", "임시저장", "--xyz"},
				new String[] {"query", "임시저장", "extra"}, new String[] {"query", "임시저장", "--k"},
				new String[] {"query", "임시저장", "--k", "0"}, new String[] {"query", "임시저장", "--k", "-1"},
				new String[] {"query", "임시저장", "--k", "xyz"},
				new String[] {"query", "임시저장", "--k", "999999999999"},
				new String[] {"query", "임시저장", "--k", "1", "--k", "2"},
				new String[] {"query", "임시저장", "--include-company", "--include-company"},
				new String[] {"eval", "xyz"}, new String[] {"index", "xyz"},
				new String[] {"index", "--search.vault=" + vault, "xyz"});
		for (String[] args : cases) {
			Result result = run(args);
			assertThat(result.code()).as(String.join(" ", args)).isEqualTo(2);
			assertThat(result.err()).contains("사용법:");
			assertThat(result.out()).isEmpty();
		}
		assertThat(settings.getIndexDir()).doesNotExist();
	}

	@Test
	void reportsOperationalFailureWithExitCodeOne() {
		Result result = run("query", "임시저장");
		assertThat(result.code()).isEqualTo(1);
		assertThat(result.err()).contains("색인이 없습니다. 먼저 index 를 실행하세요");
		assertThat(result.out()).isEmpty();
		settings.setVault(temp.resolve("missing"));
		assertThat(run("index").code()).isEqualTo(1);
	}

	@Test
	void selectsVectorModeAndKeepsBm25AsDefault() {
		assertThat(run("index", "--search.vault=" + vault).code()).isZero();
		assertThat(run("query", "임시저장", "--mode", "bm25")).isEqualTo(run("query", "임시저장"));
		Result vector = run("query", "xyz", "--mode", "vector", "--k", "2");
		assertThat(vector.code()).isZero();
		assertThat(vector.err()).isEmpty();
		assertThat(vector.out().lines().toList()).hasSize(2);
		assertThat(vector.out()).doesNotContain("p03.md");
		Result company = run("query", "xyz", "--mode", "vector", "--include-company");
		assertThat(company.code()).isZero();
		assertThat(company.out()).contains("p03.md");
		Result hybrid = run("query", "임시저장", "--mode", "hybrid", "--k", "2");
		assertThat(hybrid.code()).isZero();
		assertThat(hybrid.err()).isEmpty();
		assertThat(hybrid.out().lines().toList()).hasSize(2);
		assertThat(hybrid.out()).doesNotContain("p03.md");
		assertThat(run("query", "임시저장", "--mode", "hybrid", "--include-company").out()).contains("p03.md");
	}

	@Test
	void vectorEmbeddingFailureReturnsExitCodeOneWithoutBm25Fallback() {
		assertThat(run("index", "--search.vault=" + vault).code()).isZero();
		router = router(texts -> {
			throw new EmbeddingException("합성 질의 임베딩 실패");
		});
		assertThat(run("query", "임시저장").out()).isNotEmpty();
		for (String mode : List.of("vector", "hybrid")) {
			Result result = run("query", "임시저장", "--mode", mode);
			assertThat(result.code()).isEqualTo(1);
			assertThat(result.out()).isEmpty();
			assertThat(result.err()).contains("합성 질의 임베딩 실패").doesNotContain("사용법:");
		}
	}

	@Test
	void noVectorFlagWritesSchemaOneWithoutCallingEmbedder() throws Exception {
		router = router(texts -> {
			throw new AssertionError("벡터 없는 명령이 임베더를 호출했습니다");
		});
		Result result = run("index", "--no-vector", "--search.vault=" + vault);
		assertThat(result.code()).isZero();
		assertThat(result.out()).contains("벡터 생성 0");
		try (var directory = FSDirectory.open(settings.getIndexDir()); var reader = DirectoryReader.open(directory)) {
			assertThat(reader.getIndexCommit().getUserData()).containsEntry("schema", "1");
		}
		assertThat(run("query", "임시저장").code()).isZero();
		assertThat(run("query", "임시저장", "--mode", "vector").code()).isEqualTo(1);
	}

	@Test
	void rejectsMissingUnknownAndDuplicateNewOptions() {
		for (String[] args : List.of(new String[] {"query", "xyz", "--mode"},
				new String[] {"query", "xyz", "--mode", "xyz"},
				new String[] {"query", "xyz", "--mode", "bm25", "--mode", "vector"},
				new String[] {"index", "--no-vector", "--no-vector"},
				new String[] {"index", "--search.vault=" + vault, "--search.vault=" + vault})) {
			assertThat(run(args).code()).isEqualTo(2);
		}
		assertThat(settings.getIndexDir()).doesNotExist();
	}

	private CommandRouter router(Embedder embedder) {
		var indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, embedder);
		return new CommandRouter(indexer, new SearchService(factory, settings, embedder), settings);
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
