package dev.aihub.search.analysis;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.apache.lucene.analysis.tokenattributes.CharTermAttribute;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.boot.context.properties.source.MapConfigurationPropertySource;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.vault.VaultReader;

class DefaultAnalyzerFactoryTest {

	@TempDir
	Path temp;

	@Test
	void rejectsProtectedDictionaryAndItsSymlinkAliasBeforeReading() throws Exception {
		Path directory = Files.createDirectory(temp.resolve(VaultReader.SECRET_PREFIX + "acme"));
		Path source = Files.writeString(directory.resolve("dictionary.txt"), "임시저장\n");
		Path alias = Files.createSymbolicLink(temp.resolve("alias.txt"), source);
		var settings = new SearchSettings();
		var factory = new DefaultAnalyzerFactory(settings);
		for (Path file : List.of(source, alias)) {
			settings.setUserDictionary(file);
			assertThatThrownBy(factory::analyzer).isInstanceOf(IllegalArgumentException.class)
					.hasMessageNotContaining(file.toString()).hasNoCause();
			assertThatThrownBy(factory::fingerprint).isInstanceOf(IllegalArgumentException.class)
					.hasMessageNotContaining(file.toString()).hasNoCause();
		}
	}

	@Test
	void usesBundledDictionaryStopPartsOfSpeechAndLowercaseInAllModes() throws IOException {
		var settings = new SearchSettings();
		var factory = new DefaultAnalyzerFactory(settings);
		assertThat(settings.getDecompoundMode()).isEqualTo("DISCARD");
		assertThat(tokens(factory, "임시저장 스프링부트 SpringBOOT 설정을"))
				.containsExactly("임시", "저장", "스프링", "부트", "springboot", "설정");
		settings.setDecompoundMode("MIXED");
		assertThat(tokens(factory, "임시저장 스프링부트"))
				.containsExactly("임시저장", "임시", "저장", "스프링부트", "스프링", "부트");
		settings.setDecompoundMode("none");
		assertThat(tokens(factory, "임시저장 스프링부트"))
				.containsExactly("임시저장", "스프링부트");
	}

	@Test
	void userFileReplacesBundledDictionaryAndEmptyFileDisablesIt() throws IOException {
		Path dictionary = temp.resolve("dictionary.txt");
		Files.writeString(dictionary, "임시저장\n");
		var source = new MapConfigurationPropertySource(Map.of("search.decompound-mode", "DISCARD",
				"search.user-dictionary", dictionary.toString()));
		var settings = new Binder(source).bind("search", Bindable.of(SearchSettings.class)).get();
		assertThat(settings.getUserDictionary()).isEqualTo(dictionary);
		var factory = new DefaultAnalyzerFactory(settings);
		assertThat(tokens(factory, "임시저장")).containsExactly("임시저장");
		String previous = factory.fingerprint();
		Files.writeString(dictionary, "");
		assertThat(tokens(factory, "임시저장")).containsExactly("임시", "저장");
		assertThat(factory.fingerprint()).isNotEqualTo(previous);
	}

	@Test
	void reportsInvalidModeMissingDictionaryAndMalformedDictionaryWithoutItsContent() throws IOException {
		var settings = new SearchSettings();
		for (String mode : new String[] {"xyz", "", null}) {
			assertThatThrownBy(() -> settings.setDecompoundMode(mode)).isInstanceOf(IllegalArgumentException.class)
					.hasMessageContaining("DISCARD, MIXED, NONE");
		}
		var source = new MapConfigurationPropertySource(Map.of("search.decompound-mode", "xyz"));
		assertThatThrownBy(() -> new Binder(source).bind("search", Bindable.of(SearchSettings.class)))
				.hasRootCauseInstanceOf(IllegalArgumentException.class);
		settings.setUserDictionary(temp.resolve("missing.txt"));
		var factory = new DefaultAnalyzerFactory(settings);
		assertThatThrownBy(factory::analyzer).hasMessageContaining("사전").hasMessageNotContaining("missing.txt");
		assertThatThrownBy(factory::fingerprint).isInstanceOf(IllegalArgumentException.class);
		Path bad = Files.writeString(temp.resolve("bad.txt"), "임시저장 임시 저장xxxx\n");
		settings.setUserDictionary(bad);
		assertThatThrownBy(factory::analyzer).isInstanceOf(IllegalArgumentException.class)
				.hasMessageNotContaining("저장xxxx");
	}

	private List<String> tokens(DefaultAnalyzerFactory factory, String text) throws IOException {
		try (var analyzer = factory.analyzer(); var stream = analyzer.tokenStream("body", text)) {
			var term = stream.addAttribute(CharTermAttribute.class);
			var tokens = new ArrayList<String>();
			stream.reset();
			while (stream.incrementToken()) {
				tokens.add(term.toString());
			}
			stream.end();
			return tokens;
		}
	}
}
