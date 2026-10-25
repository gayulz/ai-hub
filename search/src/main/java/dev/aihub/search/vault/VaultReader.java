package dev.aihub.search.vault;

import java.io.IOException;
import java.io.StringWriter;
import java.nio.channels.Channels;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryIteratorException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Set;

import org.springframework.stereotype.Component;

@Component
public class VaultReader {

	public static final String SECRET_PREFIX = "🔐";
	private static final Set<String> IGNORED_DIRECTORIES = Set.of(".obsidian", ".git", ".trash");
	private final FrontMatterParser parser = new FrontMatterParser();

	public VaultReadResult read(Path vaultRoot) throws IOException {
		Path root = vaultRoot.toAbsolutePath().normalize();
		var notes = new ArrayList<Note>();
		var unreadable = new HashSet<String>();
		if (excluded(root)) {
			return new VaultReadResult(notes, unreadable);
		}
		if (!Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
			throw new IOException("볼트 경로는 심볼릭 링크가 아닌 폴더여야 합니다");
		}
		readDirectory(root, root, notes, unreadable);
		notes.sort(Comparator.comparing(Note::path));
		return new VaultReadResult(notes, unreadable);
	}

	private void readDirectory(Path root, Path directory, List<Note> notes, Set<String> unreadable) throws IOException {
		try (var entries = openDirectory(directory)) {
			for (Path file : entries) {
				if (excluded(file)) {
					continue;
				}
				BasicFileAttributes attributes;
				try {
					attributes = Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
				} catch (IOException exception) {
					if (allowedNote(file)) {
						unreadable.add(relative(root, file));
						continue;
					}
					throw exception;
				}
				if (attributes.isDirectory()) {
					readDirectory(root, file, notes, unreadable);
				} else if (attributes.isRegularFile() && allowedNote(file)) {
					try {
						notes.add(note(relative(root, file), nfc(readText(file))));
					} catch (IOException exception) {
						unreadable.add(relative(root, file));
					}
				}
			}
		} catch (IOException | DirectoryIteratorException exception) {
			throw new IOException("볼트 폴더를 읽을 수 없어 색인을 중단합니다", exception);
		}
	}

	DirectoryStream<Path> openDirectory(Path directory) throws IOException {
		return Files.newDirectoryStream(directory);
	}

	String readText(Path file) throws IOException {
		var decoder = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
				.onUnmappableCharacter(CodingErrorAction.REPORT);
		try (var channel = Files.newByteChannel(file, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS);
				var reader = Channels.newReader(channel, decoder, -1)) {
			var text = new StringWriter();
			reader.transferTo(text);
			return text.toString();
		}
	}

	private boolean allowedNote(Path file) {
		return !excluded(file) && file.getFileName().toString().endsWith(".md");
	}

	private boolean excluded(Path path) {
		for (Path part : path) {
			String name = part.toString();
			if (name.startsWith(SECRET_PREFIX) || IGNORED_DIRECTORIES.contains(name)) {
				return true;
			}
		}
		return false;
	}

	private Note note(String path, String text) {
		var parsed = parser.parse(text);
		String filename = path.substring(path.lastIndexOf('/') + 1);
		String title = parsed.body().lines().filter(line -> line.startsWith("# "))
				.map(line -> line.substring(2).strip()).findFirst()
				.orElse(filename.substring(0, filename.length() - 3));
		return new Note(path, title, parsed.body(), parsed.type(), parsed.sensitivity(), parsed.created(),
				parsed.tags(), hash(text));
	}

	private String relative(Path root, Path file) {
		return nfc(root.relativize(file).toString().replace(file.getFileSystem().getSeparator(), "/"));
	}

	private String nfc(String text) {
		return Normalizer.normalize(text, Normalizer.Form.NFC);
	}

	private String hash(String text) {
		try {
			return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
					.digest(text.getBytes(StandardCharsets.UTF_8)));
		} catch (NoSuchAlgorithmException exception) {
			throw new IllegalStateException("SHA-256을 사용할 수 없습니다", exception);
		}
	}
}
