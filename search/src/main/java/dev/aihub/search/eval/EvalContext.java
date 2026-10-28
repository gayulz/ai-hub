package dev.aihub.search.eval;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.Properties;
import java.util.jar.JarFile;
import java.util.regex.Pattern;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexCommit;
import org.apache.lucene.store.FSDirectory;

public final class EvalContext {

	public enum TimeSource {
		SEGMENTS_FILE_MTIME_APPROXIMATION
	}

	public record Executable(String kind, String identifier, Instant buildTime) {
	}

	public record IndexState(long generation, Instant modifiedTime, long size, String fileIdentity,
			TimeSource timeSource) {
		@Override
		public String toString() {
			return "IndexState[generation=" + generation + ", modifiedTime=" + modifiedTime
					+ ", timeSource=" + timeSource + "]";
		}
	}

	private EvalContext() {
	}

	public static Executable executable() throws IOException {
		try {
			var source = EvalContext.class.getProtectionDomain().getCodeSource();
			if (source == null || source.getLocation() == null) {
				throw new IOException();
			}
			var location = source.getLocation();
			if ("file".equals(location.getProtocol())) {
				Path path = Path.of(location.toURI());
				try (InputStream metadata = EvalContext.class.getResourceAsStream("/META-INF/build-info.properties")) {
					return executable(path, metadata);
				}
			}
			// Spring Boot 중첩 URL은 단일 실행 JAR 클래스 경로로 원본을 확인한다.
			String[] entries = System.getProperty("java.class.path", "").split(Pattern.quote(File.pathSeparator), -1);
			if (entries.length != 1 || entries[0].isBlank()) {
				throw new IOException();
			}
			return executable(Path.of(entries[0]), null);
		} catch (IOException | URISyntaxException | RuntimeException exception) {
			throw new IOException("검색 실행 파일 정보를 확인하지 못했습니다");
		}
	}

	static Executable executable(Path location, InputStream metadata) throws IOException {
		try {
			if (Files.isRegularFile(location)) {
				var before = Files.readAttributes(location, BasicFileAttributes.class);
				try (var jar = new JarFile(location.toFile())) {
					if (jar.size() == 0) {
						throw new IOException();
					}
				}
				var digest = sha256();
				try (var input = Files.newInputStream(location)) {
					byte[] buffer = new byte[16384];
					for (int count; (count = input.read(buffer)) != -1;) {
						digest.update(buffer, 0, count);
					}
				}
				var after = Files.readAttributes(location, BasicFileAttributes.class);
				if (!after.isRegularFile() || before.fileKey() == null || !before.fileKey().equals(after.fileKey())
						|| before.size() != after.size() || !before.lastModifiedTime().equals(after.lastModifiedTime())) {
					throw new IOException();
				}
				return new Executable("jar", prefix(digest.digest()), null);
			}
			if (!Files.isDirectory(location) || metadata == null) {
				throw new IOException();
			}
			var properties = new Properties();
			properties.load(metadata);
			String version = properties.getProperty("build.version", "");
			if (!version.matches("[A-Za-z0-9][A-Za-z0-9._+-]{0,79}")) {
				throw new IOException();
			}
			return new Executable("build", version, Instant.parse(properties.getProperty("build.time", "")));
		} catch (IOException | RuntimeException exception) {
			throw new IOException("검색 실행 파일 정보를 확인하지 못했습니다");
		}
	}

	public static IndexState indexState(Path index) throws IOException {
		try {
			if (!Files.isDirectory(index)) {
				throw new IOException();
			}
			try (var directory = FSDirectory.open(index)) {
				IndexCommit before = DirectoryReader.listCommits(directory).getLast();
				Path file = index.resolve(before.getSegmentsFileName());
				var first = Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
				IndexCommit after = DirectoryReader.listCommits(directory).getLast();
				var second = Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
				if (!first.isRegularFile() || first.fileKey() == null || second.fileKey() == null
						|| before.getGeneration() != after.getGeneration()
						|| !before.getSegmentsFileName().equals(after.getSegmentsFileName())
						|| first.size() != second.size() || !first.lastModifiedTime().equals(second.lastModifiedTime())
						|| !first.fileKey().equals(second.fileKey())) {
					throw new IOException();
				}
				String identity = HexFormat.of().formatHex(sha256().digest(first.fileKey().toString().getBytes(StandardCharsets.UTF_8)));
				return new IndexState(before.getGeneration(), first.lastModifiedTime().toInstant(), first.size(), identity,
						TimeSource.SEGMENTS_FILE_MTIME_APPROXIMATION);
			}
		} catch (IOException | RuntimeException exception) {
			throw new IOException("색인 커밋 정보를 확인하지 못했습니다");
		}
	}

	public static void requireUnchanged(Path index, IndexState previous) throws IOException {
		if (previous == null || !previous.equals(indexState(index))) {
			throw new IOException("평가 중 색인 커밋이 변경됐습니다");
		}
	}

	private static MessageDigest sha256() {
		try {
			return MessageDigest.getInstance("SHA-256");
		} catch (NoSuchAlgorithmException exception) {
			throw new IllegalStateException("SHA-256을 사용할 수 없습니다");
		}
	}

	private static String prefix(byte[] bytes) {
		return HexFormat.of().formatHex(bytes, 0, 6);
	}
}
