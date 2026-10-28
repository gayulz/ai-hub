package dev.aihub.search.eval;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.text.Normalizer;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.vault.VaultReader;

class EvalSetReaderTest {

	@TempDir
	Path temp;

	@Test
	void returnsTheHashOfTheParsedBytesWithQueriesWithoutRewritingOrExposingInput() throws Exception {
		String question = "합성 질의 " + Normalizer.normalize("설정", Normalizer.Form.NFD);
		String answer = "sample/sentinel-answer.md";
		String toml = "# 합성 평가\n[[query]]\nid='q1'\ntext='" + question
				+ "'\ncompany=false\nanswers=['" + answer + "']\n";
		byte[] original = toml.getBytes(StandardCharsets.UTF_8);
		Path file = Files.write(temp.resolve("sentinel-eval.toml"), original);
		var reader = new EvalSetReader();
		var result = reader.readWithHash(file);
		assertThat(result.queries()).containsExactly(
				new EvalQuery("q1", Normalizer.normalize(question, Normalizer.Form.NFC), false, List.of(answer)));
		assertThat(result.queries()).isEqualTo(reader.read(file));
		assertThatThrownBy(() -> result.queries().clear()).isInstanceOf(UnsupportedOperationException.class);
		assertThat(result.sha256Prefix()).isEqualTo(hash(original)).matches("[0-9a-f]{12}")
				.doesNotContain(question, answer, file.getFileName().toString());
		assertThat(result.toString()).doesNotContain(question, Normalizer.normalize(question, Normalizer.Form.NFC),
				answer, file.getFileName().toString());
		assertThat(Files.readAllBytes(file)).isEqualTo(original);

		Path renamed = Files.copy(file, temp.resolve("another-name.toml"));
		assertThat(reader.readWithHash(renamed).sha256Prefix()).isEqualTo(result.sha256Prefix());
		byte[] changed = (toml + "# 다른 주석\n").getBytes(StandardCharsets.UTF_8);
		Files.write(file, changed);
		var changedResult = reader.readWithHash(file);
		assertThat(changedResult.queries()).isEqualTo(result.queries());
		assertThat(changedResult.sha256Prefix()).isEqualTo(hash(changed)).isNotEqualTo(result.sha256Prefix());
		assertThat(Files.readAllBytes(file)).isEqualTo(changed);
		Files.write(file, new byte[0]);
		var empty = reader.readWithHash(file);
		assertThat(empty.queries()).isEmpty();
		assertThat(empty.sha256Prefix()).isEqualTo(hash(new byte[0]));
	}

	@Test
	void enforcesTheByteLimitAndKeepsFailuresAndTemporaryFilesPrivate() throws Exception {
		var before = temporaryEvalDirectories();
		String question = "sentinel-query";
		String answer = "sample/sentinel-answer.md";
		String valid = "[[query]]\nid='q1'\ntext='" + question + "'\ncompany=false\nanswers=['" + answer + "']\n";
		String atLimit = valid + "#" + "x".repeat(4 * 1024 * 1024 - valid.length() - 1);
		Path file = Files.writeString(temp.resolve("sentinel-eval.toml"), atLimit);
		var reader = new EvalSetReader();
		assertThat(reader.readWithHash(file).sha256Prefix()).isEqualTo(hash(atLimit.getBytes(StandardCharsets.UTF_8)));
		assertThat(temporaryEvalDirectories()).isEqualTo(before);

		String tooLarge = atLimit + "x";
		Files.writeString(file, tooLarge);
		assertThatThrownBy(() -> reader.readWithHash(file)).isInstanceOf(IllegalArgumentException.class)
				.hasMessageNotContaining(question).hasMessageNotContaining(answer)
				.hasMessageNotContaining(file.getFileName().toString()).hasNoCause();
		assertThatThrownBy(() -> reader.read(file)).isInstanceOf(IllegalArgumentException.class)
				.hasMessageNotContaining(question).hasMessageNotContaining(answer)
				.hasMessageNotContaining(file.getFileName().toString()).hasNoCause();
		assertThat(Files.readString(file)).isEqualTo(tooLarge);
		assertThat(temporaryEvalDirectories()).isEqualTo(before);

		Files.writeString(file, valid + valid);
		assertThatThrownBy(() -> reader.readWithHash(file)).isInstanceOf(IllegalArgumentException.class)
				.hasMessageNotContaining(question).hasMessageNotContaining(answer)
				.hasMessageNotContaining(file.getFileName().toString()).hasNoCause();
		assertThat(Files.readString(file)).isEqualTo(valid + valid);
		assertThat(temporaryEvalDirectories()).isEqualTo(before);
		assertThatThrownBy(() -> new EvalSetReader(Duration.ZERO).readWithHash(file)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(question).hasMessageNotContaining(answer)
				.hasMessageNotContaining(file.getFileName().toString()).hasNoCause();
		assertThat(temporaryEvalDirectories()).isEqualTo(before);
	}

	private String hash(byte[] bytes) throws Exception {
		return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)).substring(0, 12);
	}

	private Set<Path> temporaryEvalDirectories() throws IOException {
		try (var entries = Files.list(Path.of(System.getProperty("java.io.tmpdir")))) {
			return entries.filter(path -> path.getFileName().toString().startsWith("aihub-eval-"))
					.collect(Collectors.toSet());
		}
	}

	@Test
	void ignoresSourceAndCandidateMetadataWithoutChangingConfirmedAnswers() throws Exception {
		String toml = "[[query]]\nid='q1'\ntext='임시저장'\ncompany=false\nanswers=['acme/p01.md']\n"
				+ "category='study'\nsource='missing-source.md'\n"
				+ "answer_candidates=[{path='xyz/p02.md', reason='unused'}]\n";
		Path file = Files.writeString(temp.resolve("metadata.toml"), toml);
		assertThat(new EvalSetReader().read(file)).containsExactly(
				new EvalQuery("q1", "임시저장", false, List.of("acme/p01.md")));
		assertThat(Files.readString(file)).isEqualTo(toml);
		assertThat(temp.resolve("missing-source.md")).doesNotExist();
	}

	@Test
	void rejectsProtectedTomlSourceAndItsSymlinkAlias() throws Exception {
		Path directory = Files.createDirectory(temp.resolve(VaultReader.SECRET_PREFIX + "acme"));
		Path source = Files.writeString(directory.resolve("eval.toml"), "query=[]\n");
		Path alias = Files.createSymbolicLink(temp.resolve("alias.toml"), source);
		for (Path file : List.of(source, alias)) {
			assertThatThrownBy(() -> new EvalSetReader().read(file)).isInstanceOf(IOException.class)
					.hasMessageNotContaining(file.toString()).hasNoCause();
			assertThatThrownBy(() -> new EvalSetReader().readWithHash(file)).isInstanceOf(IOException.class)
					.hasMessageNotContaining(file.toString()).hasMessageNotContaining(file.getFileName().toString())
					.hasNoCause();
		}
	}

	@Test
	void readsRealTomlEscapesMultilineAndNfcWithoutRewritingSource() throws Exception {
		String decomposed = Normalizer.normalize("설정", Normalizer.Form.NFD);
		String toml = "# 합성 평가\n[[query]]\nid = 'q1'\ntext = \"\"\"임시저장\n설정\"\"\"\n"
				+ "company = true\nanswers = ['acme/" + decomposed + ".md', \"xyz/p01.md\"]\n"
				+ "[[query]]\nid = 'q2'\ntext = \"스프링\\uBD80트\"\ncompany = false\nanswers = ['p01.md']\n";
		Path file = Files.writeString(temp.resolve("eval.toml"), toml);
		var queries = new EvalSetReader().read(file);
		assertThat(queries).containsExactly(
				new EvalQuery("q1", "임시저장\n설정", true, List.of("acme/설정.md", "xyz/p01.md")),
				new EvalQuery("q2", "스프링부트", false, List.of("p01.md")));
		assertThat(Files.readString(file)).isEqualTo(toml);
		assertThatThrownBy(() -> queries.clear()).isInstanceOf(UnsupportedOperationException.class);
		Files.writeString(file, "query = []\n");
		assertThat(new EvalSetReader().read(file)).isEmpty();
	}

	@Test
	void rejectsEmptyAnswersDuplicateIdsAndWrongTypesWithoutEchoingInput() throws Exception {
		String valid = "[[query]]\nid='q1'\ntext='acme-private-query'\ncompany=false\nanswers=['p01.md']\n";
		var invalid = List.of(valid.replace("['p01.md']", "[]"), valid.replace("['p01.md']", "['']"),
				valid.replace("['p01.md']", "[12]"), valid.replace("['p01.md']", "'p01.md'"),
				valid.replace("company=false", "company='false'"), valid.replace("id='q1'", "id=1"),
				valid.replace("text='acme-private-query'", "text=[]"), valid + valid,
				valid.replace("id='q1'", "id='acme/path'"), valid.replace("answers=['p01.md']", ""),
				"query = 'acme-private-query'", valid + "extra = 1", "[[query]\nacme-private-query");
		for (String toml : invalid) {
			Path file = Files.writeString(temp.resolve("invalid.toml"), toml);
			assertThatThrownBy(() -> new EvalSetReader().read(file)).isInstanceOf(IllegalArgumentException.class)
					.hasMessageNotContaining("acme-private-query").hasMessageNotContaining(temp.toString());
		}
		String id = Normalizer.normalize("설정", Normalizer.Form.NFD);
		Path duplicate = Files.writeString(temp.resolve("duplicate.toml"),
				valid.replace("q1", "설정") + valid.replace("q1", id));
		assertThatThrownBy(() -> new EvalSetReader().read(duplicate)).isInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void rejectsAbsoluteParentAndProtectedAnswerPaths() throws Exception {
		for (String answer : List.of("/acme/p01.md", "../p01.md", "acme/../p01.md", "acme/./p01.md",
				"acme//p01.md", "C:/acme/p01.md", "acme\\p01.md", "🔐p01.md", "acme/🔐xyz/p01.md",
				".git/p01.md", "acme/.obsidian/p01.md", ".trash/p01.md", "p01.txt", "p01.md\n")) {
			Path file = Files.writeString(temp.resolve("path.toml"),
					"[[query]]\nid='q1'\ntext='임시저장'\ncompany=false\nanswers=['" + answer + "']\n");
			assertThatThrownBy(() -> new EvalSetReader().read(file)).isInstanceOf(IllegalArgumentException.class)
					.hasMessageNotContaining(answer);
		}
	}

	@Test
	void reportsMissingFilesAndTimeoutSafelyAndCanReadAgainAfterTimeout() throws Exception {
		Path missing = temp.resolve("missing.toml");
		assertThatThrownBy(() -> new EvalSetReader().read(missing)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(missing.toString());
		assertThatThrownBy(() -> new EvalSetReader().readWithHash(missing)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(missing.toString()).hasMessageNotContaining(missing.getFileName().toString())
				.hasNoCause();
		Path file = Files.writeString(temp.resolve("empty.toml"), "");
		assertThatThrownBy(() -> new EvalSetReader(Duration.ZERO).read(file)).isInstanceOf(IOException.class)
				.hasMessageContaining("제한 시간");
		assertThat(new EvalSetReader().read(file)).isEmpty();
	}

	@Test
	void appendsOnlyIdsScoresAndSettingsAndReportsLogFailureSafely() throws Exception {
		var settings = new SearchSettings();
		var report = new EvalRunnerImpl().run(List.of(new EvalQuery("q1", "acme-private-query", false,
				List.of("acme/p01.md"))), (query, k, company) -> List.of("acme/p01.md"), 5);
		Path log = temp.resolve("logs/scores.md");
		var writer = new EvalLog();
		String fingerprint = new DefaultAnalyzerFactory(settings).fingerprint();
		var executable = new EvalContext.Executable("jar", "0123456789ab", null);
		var index = new EvalContext.IndexState(1, Instant.parse("2026-10-28T01:02:03Z"), 1, "synthetic",
				EvalContext.TimeSource.SEGMENTS_FILE_MTIME_APPROXIMATION);
		writer.append(log, "hybrid", 5, settings, fingerprint, report, "abcdef012345", executable, index);
		writer.append(log, "bm25", 5, settings, fingerprint, report, "abcdef012345", executable, index);
		String text = Files.readString(log);
		assertThat(text).contains("hybrid", "bm25", "DISCARD", fingerprint, "k1=1.2", "b=0.3", "bge-m3",
				"1024", "Hit@5 (recallAt5): 1.000000", "MRR@5: 1.000000", "| 1 | 1 | 1.000000 |");
		assertThat(text.lines().filter(line -> line.startsWith("## "))).hasSize(2);
		assertThat(text).doesNotContain("acme-private-query", "acme/p01.md", temp.toString(), "rankedPaths", "q1");
		byte[] previous = Files.readAllBytes(log);
		assertThatThrownBy(() -> writer.append(log, "bm25", 5, settings, fingerprint, report, "invalid-private-hash", executable, index))
				.isInstanceOf(IOException.class).hasMessageNotContaining("invalid-private-hash").hasNoCause();
		assertThatThrownBy(() -> writer.append(log, "bm25", 5, settings, fingerprint, report, "abcdef012345", null, index))
				.isInstanceOf(IOException.class).hasNoCause();
		assertThat(Files.readAllBytes(log)).isEqualTo(previous);
		assertThatThrownBy(() -> writer.append(temp, "bm25", 5, settings, fingerprint, report, "abcdef012345", executable, index))
				.isInstanceOf(IOException.class).hasMessageNotContaining(temp.toString());
	}
}
