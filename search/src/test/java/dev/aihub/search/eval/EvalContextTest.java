package dev.aihub.search.eval;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.Map;
import java.util.TreeMap;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;

import org.apache.lucene.analysis.standard.StandardAnalyzer;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.StringField;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.store.FSDirectory;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class EvalContextTest {

	@TempDir
	Path temp;

	@Test
	void hashesTheSyntheticExecutableJarWithoutExposingItsNameOrPath() throws Exception {
		Path jar = temp.resolve("private-jar-name.jar");
		try (var output = new JarOutputStream(Files.newOutputStream(jar))) {
			output.putNextEntry(new JarEntry("synthetic.txt"));
			output.write("합성 자료".getBytes(StandardCharsets.UTF_8));
			output.closeEntry();
		}
		byte[] before = Files.readAllBytes(jar);
		var executable = EvalContext.executable(jar, null);
		String expected = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(before)).substring(0, 12);
		assertThat(executable.kind()).isEqualTo("jar");
		assertThat(executable.identifier()).isEqualTo(expected).matches("[0-9a-f]{12}");
		assertThat(executable.buildTime()).isNull();
		assertThat(executable.toString()).doesNotContain(temp.toString(), jar.getFileName().toString());
		assertThat(Files.readAllBytes(jar)).isEqualTo(before);
		Path renamed = Files.copy(jar, temp.resolve("renamed-without-extension"));
		assertThat(EvalContext.executable(renamed, null).identifier()).isEqualTo(expected);
	}

	@Test
	void usesGeneratedVersionAndTimeForADevelopmentDirectory() throws Exception {
		Path classes = Files.createDirectory(temp.resolve("classes"));
		var metadata = bytes("build.version=1.2.3-test\nbuild.time=2026-10-28T01:02:03Z\n");
		var executable = EvalContext.executable(classes, metadata);
		assertThat(executable.kind()).isEqualTo("build");
		assertThat(executable.identifier()).isEqualTo("1.2.3-test");
		assertThat(executable.buildTime()).isEqualTo(Instant.parse("2026-10-28T01:02:03Z"));
		assertThat(executable.toString()).doesNotContain(temp.toString(), classes.getFileName().toString());
	}

	@Test
	void runningDevelopmentBuildReadsTheGeneratedVersionAndTime() throws Exception {
		var executable = EvalContext.executable();
		assertThat(executable.kind()).isEqualTo("build");
		assertThat(executable.identifier()).matches("[A-Za-z0-9][A-Za-z0-9._+-]{0,79}");
		assertThat(executable.buildTime()).isNotNull();
	}

	@Test
	void failsClosedWhenExecutableInformationIsMissingOrInvalid() throws Exception {
		Path classes = Files.createDirectory(temp.resolve("classes"));
		Path missing = temp.resolve("private-missing.jar");
		Path invalidJar = Files.writeString(temp.resolve("not-an-archive"), "합성 일반 파일");
		assertThatThrownBy(() -> EvalContext.executable(invalidJar, null)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(invalidJar.toString()).hasNoCause();
		assertThatThrownBy(() -> EvalContext.executable(missing, null)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(temp.toString()).hasMessageNotContaining(missing.getFileName().toString()).hasNoCause();
		assertThatThrownBy(() -> EvalContext.executable(classes, null)).isInstanceOf(IOException.class).hasNoCause();
		assertThatThrownBy(() -> EvalContext.executable(classes, bytes("build.version=1\n")))
				.isInstanceOf(IOException.class).hasNoCause();
		assertThatThrownBy(() -> EvalContext.executable(classes, bytes("build.version=private/path\nbuild.time=bad\n")))
				.isInstanceOf(IOException.class).hasNoCause();
	}

	@Test
	void readsCommitFileTimeAsAnExplicitApproximationWithoutWritingTheIndex() throws Exception {
		Path index = Files.createDirectory(temp.resolve("private-index"));
		commit(index, "first");
		Path segments;
		try (var directory = FSDirectory.open(index)) {
			segments = index.resolve(DirectoryReader.listCommits(directory).getLast().getSegmentsFileName());
		}
		Instant modified = Instant.parse("2026-10-27T01:02:03Z");
		Files.setLastModifiedTime(segments, FileTime.from(modified));
		Map<String, String> before = fileHashes(index);
		var state = EvalContext.indexState(index);
		assertThat(state.generation()).isPositive();
		assertThat(state.modifiedTime()).isEqualTo(modified);
		assertThat(state.timeSource()).isEqualTo(EvalContext.TimeSource.SEGMENTS_FILE_MTIME_APPROXIMATION);
		assertThat(state.toString()).doesNotContain(temp.toString(), index.getFileName().toString(), segments.getFileName().toString());
		assertThat(fileHashes(index)).isEqualTo(before);
		EvalContext.requireUnchanged(index, state);
		assertThat(fileHashes(index)).isEqualTo(before);
	}

	@Test
	void detectsAChangedCommitAndReportsMissingIndexesWithoutPaths() throws Exception {
		Path index = Files.createDirectory(temp.resolve("private-index"));
		commit(index, "first");
		var first = EvalContext.indexState(index);
		commit(index, "second");
		var second = EvalContext.indexState(index);
		assertThat(second.generation()).isGreaterThan(first.generation());
		assertThatThrownBy(() -> EvalContext.requireUnchanged(index, first)).isInstanceOf(IOException.class)
				.hasMessageContaining("변경").hasMessageNotContaining(index.toString()).hasNoCause();
		Path missing = temp.resolve("private-missing-index");
		assertThatThrownBy(() -> EvalContext.indexState(missing)).isInstanceOf(IOException.class)
				.hasMessageNotContaining(temp.toString()).hasMessageNotContaining(missing.getFileName().toString()).hasNoCause();
		assertThat(missing).doesNotExist();
	}

	private ByteArrayInputStream bytes(String text) {
		return new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8));
	}

	private void commit(Path index, String value) throws Exception {
		try (var directory = FSDirectory.open(index); var analyzer = new StandardAnalyzer();
				var writer = new IndexWriter(directory, new IndexWriterConfig(analyzer))) {
			var document = new Document();
			document.add(new StringField("id", value, StringField.Store.YES));
			writer.addDocument(document);
			writer.commit();
		}
	}

	private Map<String, String> fileHashes(Path index) throws Exception {
		Map<String, String> result = new TreeMap<>();
		try (var files = Files.list(index)) {
			for (Path path : files.filter(Files::isRegularFile).toList()) {
				result.put(path.getFileName().toString(), HexFormat.of().formatHex(
						MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(path))));
			}
		}
		return result;
	}
}
