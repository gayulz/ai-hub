package dev.aihub.search;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.boot.context.properties.source.MapConfigurationPropertySource;
import org.springframework.core.env.SystemEnvironmentPropertySource;
import org.springframework.mock.env.MockEnvironment;

class SearchSettingsTest {

	@TempDir
	Path temp;

	@Test
	void containsAllDefaultsInOnePlace() {
		var settings = new SearchSettings();
		assertThat(settings.getBm25K1()).isEqualTo(1.2f);
		assertThat(settings.getBm25B()).isEqualTo(0.3f);
		assertThat(settings.getChunkSoftLimit()).isEqualTo(1200);
		assertThat(settings.getChunkHardLimit()).isEqualTo(2000);
		assertThat(settings.getBodyBoost()).isEqualTo(1.0f);
		assertThat(settings.getHeadingBoost()).isEqualTo(1.5f);
		assertThat(settings.getNoteTitleBoost()).isEqualTo(2.0f);
		assertThat(settings.getEmbeddingModel()).isEqualTo("bge-m3");
		assertThat(settings.getOllamaUrl()).isEqualTo("http://127.0.0.1:11434");
		assertThat(settings.getEmbeddingBatch()).isEqualTo(8);
		assertThat(settings.getEmbeddingDimension()).isEqualTo(1024);
		assertThat(settings.getVault()).isNull();
		assertThat(settings.getIndexDir().endsWith(Path.of(".ai-hub", "index"))).isTrue();
	}

	@Test
	void bindsPathsLimitsSimilarityAndWeights() {
		var source = new MapConfigurationPropertySource(Map.of(
				"search.index-dir", temp.resolve("index").toString(), "search.vault", temp.resolve("vault").toString(),
				"search.chunk-soft-limit", "100", "search.chunk-hard-limit", "200",
				"search.bm25-k1", "0.9", "search.bm25-b", "0.2",
				"search.body-boost", "3", "search.heading-boost", "4", "search.note-title-boost", "5"));
		var settings = new Binder(source).bind("search", Bindable.of(SearchSettings.class)).get();
		assertThat(settings.getIndexDir()).isEqualTo(temp.resolve("index"));
		assertThat(settings.getVault()).isEqualTo(temp.resolve("vault"));
		assertThat(settings.getChunkSoftLimit()).isEqualTo(100);
		assertThat(settings.getChunkHardLimit()).isEqualTo(200);
		assertThat(settings.getBm25K1()).isEqualTo(0.9f);
		assertThat(settings.getBm25B()).isEqualTo(0.2f);
		assertThat(settings.getBodyBoost()).isEqualTo(3f);
		assertThat(settings.getHeadingBoost()).isEqualTo(4f);
		assertThat(settings.getNoteTitleBoost()).isEqualTo(5f);
	}

	@Test
	void bindsSyntheticEnvironmentVariableForVault() {
		var environment = new MockEnvironment();
		environment.getPropertySources().addFirst(new SystemEnvironmentPropertySource("synthetic",
				Map.of("SEARCH_VAULT", temp.resolve("vault").toString())));
		var settings = Binder.get(environment).bind("search", Bindable.of(SearchSettings.class)).get();
		assertThat(settings.getVault()).isEqualTo(temp.resolve("vault"));
	}

	@Test
	void bindsEmbeddingSettings() {
		var source = new MapConfigurationPropertySource(Map.of("search.embedding-model", "xyz",
				"search.ollama-url", "http://localhost:12345", "search.embedding-batch", "4",
				"search.embedding-dimension", "16"));
		var settings = new Binder(source).bind("search", Bindable.of(SearchSettings.class)).get();
		assertThat(settings.getEmbeddingModel()).isEqualTo("xyz");
		assertThat(settings.getOllamaUrl()).isEqualTo("http://localhost:12345");
		assertThat(settings.getEmbeddingBatch()).isEqualTo(4);
		assertThat(settings.getEmbeddingDimension()).isEqualTo(16);
	}

	@Test
	void rejectsNonLocalOllamaHostDuringConfigurationBinding() {
		var source = new MapConfigurationPropertySource(Map.of("search.ollama-url", "http://example.org"));
		assertThatThrownBy(() -> new Binder(source).bind("search", Bindable.of(SearchSettings.class)))
				.hasRootCauseInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void allowsOnlyExplicitLoopbackHostsAndHttpAddresses() {
		var settings = new SearchSettings();
		for (String url : List.of("http://127.0.0.1:11434", "http://localhost:12345", "http://[::1]:11434")) {
			settings.setOllamaUrl(url);
			assertThat(settings.getOllamaUrl()).isEqualTo(url);
		}
		for (String url : List.of("http://example.org", "http://127.0.0.2", "http://0.0.0.0",
				"http://localhost.example.org", "http://127.0.0.1@example.org", "http://[::ffff:127.0.0.1]",
				"ftp://localhost", "xyz", "http://localhost?xyz", "http://localhost#xyz")) {
			assertThatThrownBy(() -> settings.setOllamaUrl(url)).isInstanceOf(IllegalArgumentException.class);
		}
	}

	@Test
	void rejectsEmptyModelAndNonPositiveBatchOrDimension() {
		var settings = new SearchSettings();
		assertThatThrownBy(() -> settings.setEmbeddingModel(" ")).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setEmbeddingModel(null)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setEmbeddingBatch(0)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setEmbeddingBatch(-1)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setEmbeddingDimension(0)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setEmbeddingDimension(-1)).isInstanceOf(IllegalArgumentException.class);
	}

	@Test
	void rejectsInvalidBm25AndWeights() {
		var settings = new SearchSettings();
		assertThatThrownBy(() -> settings.setBm25K1(-1)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setBm25K1(Float.POSITIVE_INFINITY)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setBm25B(1.1f)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setBm25B(Float.NaN)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setBodyBoost(0)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setHeadingBoost(Float.NaN)).isInstanceOf(IllegalArgumentException.class);
		assertThatThrownBy(() -> settings.setNoteTitleBoost(-1)).isInstanceOf(IllegalArgumentException.class);
	}
}
