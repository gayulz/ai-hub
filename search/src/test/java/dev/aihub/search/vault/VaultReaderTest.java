package dev.aihub.search.vault;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AccessDeniedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Set;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class VaultReaderTest {

	@TempDir
	Path temp;

	@Test
	void filtersPathsBeforeAttemptingToReadContentAndNeverFollowsLinks() throws Exception {
		Path root = Files.createDirectory(temp.resolve("vault"));
		Path allowed = write(root.resolve("acme/p01.md"), "# p01\n본문");
		Path secretFile = write(root.resolve(VaultReader.SECRET_PREFIX + "p02.md"), "# p02");
		write(root.resolve("xyz/" + VaultReader.SECRET_PREFIX + "acme/p03.md"), "# p03");
		for (String directory : List.of(".obsidian", ".git", ".trash")) {
			write(root.resolve(directory + "/p04.md"), "# p04");
		}
		write(root.resolve("p05.txt"), "# p05");
		Path outside = write(temp.resolve("xyz/p06.md"), "# p06");
		Files.createSymbolicLink(root.resolve("p07.md"), outside);
		Files.createSymbolicLink(root.resolve("link"), outside.getParent());
		Files.createSymbolicLink(root.resolve("p08.md"), temp.resolve("missing.md"));
		var reader = new TrackingReader();
		reader.forbidden.add(secretFile);
		reader.forbidden.add(root.resolve("xyz/" + VaultReader.SECRET_PREFIX + "acme"));
		for (String directory : List.of(".obsidian", ".git", ".trash")) {
			reader.forbidden.add(root.resolve(directory));
		}

		var result = reader.read(root);

		assertThat(result.notes()).extracting(Note::path).containsExactly("acme/p01.md");
		assertThat(result.skippedFiles()).isZero();
		assertThat(reader.opened).containsExactly(allowed);
		assertThat(reader.openedDirectories).containsExactlyInAnyOrder(root, root.resolve("acme"), root.resolve("xyz"));
	}

	@Test
	void excludesSecretRootWithoutOpeningNotesAndRejectsLinkedRoot() throws Exception {
		Path secret = Files.createDirectory(temp.resolve(VaultReader.SECRET_PREFIX + "acme"));
		write(secret.resolve("xyz/p01.md"), "본문");
		var reader = new TrackingReader();
		assertThat(reader.read(secret.resolve("xyz")).notes()).isEmpty();
		assertThat(reader.opened).isEmpty();
		assertThat(reader.openedDirectories).isEmpty();
		Path linked = Files.createSymbolicLink(temp.resolve("link"), secret);
		assertThatThrownBy(() -> reader.read(linked)).isInstanceOf(IOException.class)
				.hasMessageContaining("심볼릭 링크가 아닌 폴더");
	}

	@Test
	void countsBadEncodingAndPermissionFailuresWithoutLeakingContent() throws Exception {
		Path root = Files.createDirectory(temp.resolve("vault"));
		write(root.resolve("p01.md"), "# p01");
		Files.write(root.resolve("p02.md"), new byte[] {(byte) 0xc3, 0x28});
		Path denied = write(root.resolve("p03.md"), "# p03");
		var reader = new TrackingReader();
		reader.denied.add(denied);

		var result = reader.read(root);

		assertThat(result.notes()).extracting(Note::path).containsExactly("p01.md");
		assertThat(result.skippedFiles()).isEqualTo(2);
		assertThat(result.unreadablePaths()).containsExactlyInAnyOrder("p02.md", "p03.md");
	}

	@Test
	void normalizesPathsAndFullTextAndHashesTheNormalizedHeaderToo() throws Exception {
		Path root = Files.createDirectory(temp.resolve("vault"));
		String text = "---\ntype: note\ntags: [\u1100\u1161]\n---\n# \u1100\u1161\n본문";
		Path file = write(root.resolve("p01-\u1100\u1161.md"), text);
		var reader = new VaultReader();
		Note note = reader.read(root).notes().getFirst();
		String normalized = Normalizer.normalize(text, Normalizer.Form.NFC);
		String expected = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
				.digest(normalized.getBytes(StandardCharsets.UTF_8)));
		assertThat(note.path()).isEqualTo("p01-가.md");
		assertThat(note.title()).isEqualTo("가");
		assertThat(note.body()).isEqualTo("# 가\n본문");
		assertThat(note.tags()).containsExactly("가");
		assertThat(note.hash()).isEqualTo(expected).matches("[0-9a-f]{64}");
		assertThat(reader.read(root).notes().getFirst().hash()).isEqualTo(note.hash());
		Files.writeString(file, normalized);
		assertThat(reader.read(root).notes().getFirst().hash()).isEqualTo(note.hash());
		Files.writeString(file, normalized.replace("type: note", "type: xyz"));
		assertThat(reader.read(root).notes().getFirst().hash()).isNotEqualTo(note.hash());
	}

	@Test
	void findsFirstTitleOrUsesFilenameAndHandlesBrokenHeaders() throws Exception {
		Path root = Files.createDirectory(temp.resolve("vault"));
		write(root.resolve("p01.md"), "---\ntype: note\n---\n도입\n# 첫 제목\n# 다음 제목");
		write(root.resolve("p02.md"), "---\ntype: [\n---\n본문");
		var notes = new VaultReader().read(root).notes();
		assertThat(notes.get(0).title()).isEqualTo("첫 제목");
		assertThat(notes.get(1).title()).isEqualTo("p02");
		assertThat(notes.get(1).type()).isNull();
		assertThat(notes.get(1).tags()).isEmpty();
	}

	@Test
	void abortsInsteadOfTreatingAnUnreadableDirectoryAsDeletedNotes() throws Exception {
		Path root = Files.createDirectory(temp.resolve("vault"));
		Path file = write(root.resolve("acme/p01.md"), "본문");
		var reader = new TrackingReader();
		reader.denied.add(file.getParent());
		assertThatThrownBy(() -> reader.read(root)).isInstanceOf(IOException.class)
				.hasMessage("볼트 폴더를 읽을 수 없어 색인을 중단합니다");
		assertThat(reader.opened).isEmpty();
	}

	private Path write(Path file, String text) throws IOException {
		Files.createDirectories(file.getParent());
		return Files.writeString(file, text);
	}

	private static class TrackingReader extends VaultReader {
		private final List<Path> opened = new ArrayList<>();
		private final List<Path> openedDirectories = new ArrayList<>();
		private final Set<Path> forbidden = new HashSet<>();
		private final Set<Path> denied = new HashSet<>();

		@Override
		DirectoryStream<Path> openDirectory(Path directory) throws IOException {
			openedDirectories.add(directory);
			if (forbidden.contains(directory)) {
				throw new AssertionError("제외 폴더의 열기를 시도했습니다");
			}
			if (denied.contains(directory)) {
				throw new AccessDeniedException(directory.getFileName().toString());
			}
			return super.openDirectory(directory);
		}

		@Override
		String readText(Path file) throws IOException {
			opened.add(file);
			if (forbidden.contains(file)) {
				throw new AssertionError("제외 파일의 읽기를 시도했습니다");
			}
			if (denied.contains(file)) {
				throw new AccessDeniedException(file.getFileName().toString());
			}
			return super.readText(file);
		}
	}
}
