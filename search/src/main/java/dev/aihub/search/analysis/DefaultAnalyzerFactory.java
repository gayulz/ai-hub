package dev.aihub.search.analysis;

import java.io.IOException;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.analysis.ko.KoreanAnalyzer;
import org.apache.lucene.analysis.ko.KoreanPartOfSpeechStopFilter;
import org.apache.lucene.analysis.ko.KoreanTokenizer.DecompoundMode;
import org.apache.lucene.analysis.ko.dict.UserDictionary;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.vault.VaultReader;

@Component
public class DefaultAnalyzerFactory implements AnalyzerFactory {

	private final SearchSettings settings;

	public DefaultAnalyzerFactory() {
		this(new SearchSettings());
	}

	@Autowired
	public DefaultAnalyzerFactory(SearchSettings settings) {
		this.settings = settings;
	}

	@Override
	public Analyzer analyzer() {
		try (var reader = new StringReader(dictionary())) {
			return new KoreanAnalyzer(UserDictionary.open(reader), DecompoundMode.valueOf(settings.getDecompoundMode()),
					KoreanPartOfSpeechStopFilter.DEFAULT_STOP_TAGS, false);
		} catch (IOException | RuntimeException exception) {
			throw new IllegalArgumentException("Nori 사용자 사전을 읽을 수 없거나 형식이 잘못되었습니다");
		}
	}

	@Override
	public String fingerprint() {
		try {
			String configuration = "nori-1\n" + settings.getDecompoundMode() + "\n" + dictionary();
			return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
					.digest(configuration.getBytes(StandardCharsets.UTF_8)));
		} catch (NoSuchAlgorithmException exception) {
			throw new IllegalStateException("SHA-256을 사용할 수 없습니다");
		}
	}

	private String dictionary() {
		try {
			if (settings.getUserDictionary() != null) {
				Path source = settings.getUserDictionary();
				rejectProtected(source.toAbsolutePath());
				Path resolvedSource = source.toRealPath();
				rejectProtected(resolvedSource);
				return Files.readString(resolvedSource, StandardCharsets.UTF_8);
			}
			try (var input = getClass().getResourceAsStream("/nori-user-dictionary.txt")) {
				if (input == null) {
					throw new IOException();
				}
				return new String(input.readAllBytes(), StandardCharsets.UTF_8);
			}
		} catch (IOException exception) {
			throw new IllegalArgumentException("Nori 사용자 사전을 읽을 수 없습니다");
		}
	}

	private void rejectProtected(Path path) throws IOException {
		for (Path part : path) {
			if (part.toString().startsWith(VaultReader.SECRET_PREFIX)) {
				throw new IOException("보호 경로는 읽을 수 없습니다");
			}
		}
	}
}
