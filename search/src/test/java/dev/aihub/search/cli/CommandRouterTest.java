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
import org.springframework.boot.DefaultApplicationArguments;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchService;
import dev.aihub.search.vault.VaultReader;

class CommandRouterTest {

	@TempDir
	Path temp;
	private Path vault;
	private SearchSettings settings;
	private CommandRouter router;

	@BeforeEach
	void setUp() throws IOException {
		vault = Files.createDirectory(temp.resolve("vault"));
		Files.writeString(vault.resolve("p01.md"), "임시저장 일반 본문");
		Files.writeString(vault.resolve("p02.md"), "임시저장 다음 본문");
		Files.writeString(vault.resolve("p03.md"), "---\nsensitivity: company\n---\n# 임시저장\n임시저장");
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		var factory = new DefaultAnalyzerFactory();
		var indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings);
		router = new CommandRouter(indexer, new SearchService(factory, settings), settings);
	}

	@Test
	void indexesExplicitVaultAndPrintsSingleReportLine() {
		settings.setVault(temp.resolve("missing"));
		Result result = run("index", "--search.vault=" + vault);
		assertThat(result.code()).isZero();
		assertThat(result.out()).isEqualTo("추가 3 · 수정 0 · 삭제 0 · 변경 없음 0 · 조각 3 · 건너뜀 0\n");
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
	void leavesEvalAsUserExerciseAndPropagatesRunnerExitCode() {
		Result result = run("eval");
		assertThat(result.code()).isEqualTo(2);
		assertThat(result.out()).isEqualTo("평가 실행기는 사용자 과제입니다(6-2c)\n");
		assertThat(result.err()).isEmpty();
		var runner = new SearchCommandLine(router);
		runner.run(new DefaultApplicationArguments("eval"));
		assertThat(runner.getExitCode()).isEqualTo(2);
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

	private Result run(String... args) {
		var out = new StringWriter();
		var err = new StringWriter();
		int code = router.run(args, new PrintWriter(out), new PrintWriter(err));
		return new Result(code, out.toString(), err.toString());
	}

	private record Result(int code, String out, String err) {
	}
}
