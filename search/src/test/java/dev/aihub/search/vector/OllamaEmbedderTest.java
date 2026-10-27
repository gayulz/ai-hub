package dev.aihub.search.vector;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import dev.aihub.search.SearchSettings;

class OllamaEmbedderTest {

	private HttpServer server;
	private SearchSettings settings;
	private OllamaEmbedder embedder;
	private final List<Request> requests = new CopyOnWriteArrayList<>();
	private final Queue<String> replies = new ConcurrentLinkedQueue<>();
	private volatile String response = "{\"embeddings\":[[1,0]]}";
	private volatile int status = 200;

	@BeforeEach
	void setUp() throws Exception {
		server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
		server.createContext("/api/embed", exchange -> {
			try (exchange) {
				requests.add(new Request(exchange.getRequestMethod(), exchange.getRequestHeaders().getFirst("Content-Type"),
						new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)));
				String reply = replies.poll();
				byte[] bytes = (reply == null ? response : reply).getBytes(StandardCharsets.UTF_8);
				if (status == 302) {
					exchange.getResponseHeaders().add("Location", "http://example.org/api/embed");
				}
				exchange.getResponseHeaders().add("Content-Type", "application/json");
				exchange.sendResponseHeaders(status, bytes.length);
				exchange.getResponseBody().write(bytes);
			}
		});
		server.start();
		settings = new SearchSettings();
		settings.setOllamaUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/");
		settings.setEmbeddingDimension(2);
		embedder = new OllamaEmbedder(settings);
	}

	@AfterEach
	void tearDown() {
		if (server != null) {
			server.stop(0);
		}
	}

	@Test
	void postsJsonWithEscapedQuotesBackslashesControlsAndUnicode() throws Exception {
		String text = "acme\"\\\b\f\n\r\t" + (char) 1 + "한" + new String(Character.toChars(0x1d11e));
		assertThat(embedder.embed(List.of(text))).singleElement()
				.satisfies(vector -> assertThat(vector).containsExactly(1, 0));
		assertThat(requests).containsExactly(new Request("POST", "application/json",
				"{\"model\":\"bge-m3\",\"input\":[\"acme\\\"\\\\\\b\\f\\n\\r\\t\\u0001\\ud55c\\ud834\\udd1e\"]}"));
	}

	@Test
	void usesConfiguredModelAndBatchSizeAndKeepsInputOrder() throws Exception {
		settings.setEmbeddingModel("xyz\"\n");
		settings.setEmbeddingBatch(8);
		List<String> texts = IntStream.range(0, 20).mapToObj(i -> "p" + i).toList();
		int offset = 0;
		for (int size : List.of(8, 8, 4)) {
			replies.add("{\"embeddings\":[" + IntStream.range(offset, offset + size).mapToObj(i -> "[" + (i + 1) + ",1]")
					.collect(Collectors.joining(",")) + "]}");
			offset += size;
		}
		var vectors = embedder.embed(texts);
		assertThat(vectors).hasSize(20);
		for (int i = 0; i < 20; i++) {
			assertThat(vectors.get(i)).containsExactly(i + 1, 1);
		}
		assertThat(requests).extracting(Request::body).containsExactly(
				requestBody(texts.subList(0, 8)), requestBody(texts.subList(8, 16)), requestBody(texts.subList(16, 20)));
	}

	@Test
	void makesNoRequestForEmptyInput() throws Exception {
		assertThat(embedder.embed(List.of())).isEmpty();
		assertThat(requests).isEmpty();
	}

	@Test
	void readsExponentNumbersAndIgnoresMetadataWithoutMistakingItForEmbeddings() throws Exception {
		response = "{\"model\":\"xyz\",\"extra\":{\"embeddings\":\"xyz\",\"data\":[null,true,false,12]},"
				+ "\"embeddings\":[[1e0,-2.5e-1]],\"total_duration\":123}";
		assertThat(embedder.embed(List.of("p01"))).singleElement()
				.satisfies(vector -> assertThat(vector).containsExactly(1, -0.25f));
	}

	@Test
	void rejectsCountAndDimensionMismatchWithoutLeakingInputOrResponse() {
		String text = "acme-private-p01";
		for (String reply : List.of("{\"embeddings\":[]}", "{\"embeddings\":[[1,0],[0,1]]}",
				"{\"embeddings\":[[1]]}", "{\"embeddings\":[[1,0,0]]}")) {
			response = reply;
			assertThatThrownBy(() -> embedder.embed(List.of(text))).isInstanceOf(EmbeddingException.class)
					.hasMessageNotContaining(text).hasMessageNotContaining(reply).hasNoCause();
		}
	}

	@Test
	void rejectsMalformedJsonNonFiniteAndZeroVectorsWithoutLeakingData() {
		String text = "acme-private-p01";
		for (String reply : List.of(text, "{\"input\":\"" + text + "\"}", "{\"embeddings\":[[NaN,0]]}",
				"{\"embeddings\":[[1e100,0]]}", "{\"embeddings\":[[0,0]]}", "{\"embeddings\":[[01,0]]}",
				"{\"embeddings\":[[1,0,]]}", "{\"embeddings\":[[1,0]]}xyz",
				"{\"embeddings\":[[1,0]],\"embeddings\":[[1,0]]}")) {
			response = reply;
			assertThatThrownBy(() -> embedder.embed(List.of(text))).isInstanceOf(EmbeddingException.class)
					.hasMessageNotContaining(text).hasMessageNotContaining(reply).hasNoCause();
		}
	}

	@Test
	void rejectsHttpErrorsAndRedirectsWithoutReturningResponseText() {
		String text = "acme-private-p01";
		response = text;
		for (int code : List.of(500, 302)) {
			status = code;
			assertThatThrownBy(() -> embedder.embed(List.of(text))).isInstanceOf(EmbeddingException.class)
					.hasMessageContaining("HTTP " + code).hasMessageNotContaining(text).hasNoCause();
		}
		assertThat(requests).hasSize(2);
	}

	@Test
	void reportsConnectionRefusalWithoutIncludingInput() {
		server.stop(0);
		assertThatThrownBy(() -> embedder.embed(List.of("acme-private-p01")))
				.isInstanceOf(EmbeddingException.class).hasMessageContaining("Ollama")
				.hasMessageNotContaining("acme-private-p01").hasNoCause();
	}

	private String requestBody(List<String> texts) {
		return "{\"model\":\"xyz\\\"\\n\",\"input\":["
				+ texts.stream().map(text -> "\"" + text + "\"").collect(Collectors.joining(",")) + "]}";
	}

	private record Request(String method, String contentType, String body) {
	}
}
