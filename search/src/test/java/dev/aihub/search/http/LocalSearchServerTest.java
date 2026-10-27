package dev.aihub.search.http;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.net.Socket;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.boot.SpringApplication;
import org.springframework.core.env.MapPropertySource;

import dev.aihub.search.SearchApplication;
import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.DefaultAnalyzerFactory;
import dev.aihub.search.chunk.Chunker;
import dev.aihub.search.cli.CommandRouter;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchHit;
import dev.aihub.search.query.SearchService;
import dev.aihub.search.vault.VaultReader;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.FakeEmbedder;

class LocalSearchServerTest {

	@TempDir
	Path temp;
	private SearchSettings settings;
	private DefaultAnalyzerFactory factory;
	private FakeEmbedder fake;
	private NoteIndexer indexer;
	private LocalSearchServer server;
	private SearchService service;

	@BeforeEach
	void setUp() throws IOException {
		Path vault = Files.createDirectory(temp.resolve("vault"));
		Files.writeString(vault.resolve("p01.md"), "# 임시저장 \"xyz\"\n일반 본문");
		Files.writeString(vault.resolve("p02.md"), "---\nsensitivity: company\n---\n임시저장");
		settings = new SearchSettings();
		settings.setIndexDir(temp.resolve("index"));
		settings.setEmbeddingDimension(4);
		factory = new DefaultAnalyzerFactory(settings);
		fake = new FakeEmbedder(4);
		indexer = new NoteIndexer(new VaultReader(), new Chunker(settings), factory, settings, fake);
		indexer.index(vault);
		service = new SearchService(factory, settings, fake);
		server = new LocalSearchServer(service);
	}

	@AfterEach
	void close() {
		server.close();
	}

	@Test
	void servesHealthAndHybridByDefaultAndFiltersCompanyInEveryMode() throws IOException {
		server.start("127.0.0.1", 0);
		var health = get("/health");
		assertThat(health.status()).isEqualTo(200);
		assertThat(health.body()).isEqualTo("{\"status\":\"ok\"}");
		var defaults = get("/search?q=" + encoded("임시저장"));
		assertThat(defaults.status()).isEqualTo(200);
		assertThat(defaults.body()).startsWith("{\"hits\":[{\"path\":\"p01.md\"")
				.contains("\\\"xyz\\\"", "\"sensitivity\":null", "\"score\":")
				.doesNotContain("p02.md");
		assertThat(defaults.headers()).contains("application/json; charset=utf-8", "no-store")
				.doesNotContain("access-control-allow-origin");
		assertThat(get("/search?q=" + encoded("임시저장") + "&mode=hybrid&k=5&includeCompany=false").body())
				.isEqualTo(defaults.body());
		for (String mode : List.of("bm25", "vector", "hybrid")) {
			String path = "/search?q=" + encoded("임시저장") + "&mode=" + mode;
			assertThat(get(path).body()).contains("p01.md").doesNotContain("p02.md");
			var included = get(path + "&includeCompany=true");
			assertThat(included.status()).isEqualTo(200);
			assertThat(included.body()).contains("p02.md", "\"sensitivity\":\"company\"");
		}
	}

	@Test
	void rejectsNonLocalHostAndOriginAndNonGetRequestsWithoutCors() throws IOException {
		server.start("127.0.0.1", 0);
		String host = "127.0.0.1:" + server.port();
		for (String value : List.of("example.org:" + server.port(), "127.0.0.1.example.org", "localhost:1",
				"127.0.0.2:" + server.port(), "acme@" + host, host + "/xyz")) {
			assertThat(request("GET", "/health", "Host: " + value).status()).isEqualTo(403);
		}
		for (String origin : List.of("http://example.org", "null", "http://localhost:1", "https://" + host)) {
			var denied = request("GET", "/health", "Host: " + host + "\r\nOrigin: " + origin);
			assertThat(denied.status()).isEqualTo(403);
			assertThat(denied.headers()).doesNotContain("access-control-allow-origin");
		}
		assertThat(request("GET", "/health", "Host: " + host + "\r\nOrigin: http://" + host).status()).isEqualTo(200);
		for (String method : List.of("POST", "PUT", "DELETE", "OPTIONS")) {
			var response = request(method, "/search?q=xyz", "Host: " + host);
			assertThat(response.status()).isEqualTo(405);
			assertThat(response.headers()).contains("allow: get").doesNotContain("access-control-allow-origin");
		}
	}

	@Test
	void rejectsMalformedDuplicateUnknownAndOversizedParameters() throws IOException {
		server.start("127.0.0.1", 0);
		for (String path : List.of("/search", "/search?q=", "/search?q=+", "/search?q=xyz&k=0",
				"/search?q=xyz&k=101", "/search?q=xyz&k=-1", "/search?q=xyz&k=abc", "/search?q=xyz&k=1&k=2",
				"/search?q=xyz&q=acme", "/search?q=xyz&%71=acme", "/search?q=xyz&mode=xyz",
				"/search?q=xyz&mode=bm25&mode=vector", "/search?q=xyz&includeCompany=1",
				"/search?q=xyz&includeCompany=false&includeCompany=true", "/search?q=xyz&extra=acme",
				"/search?q=xyz&k", "/search?q=%FF", "/search?q=%00", "/search?q=%ZZ",
				"/search?q=" + "x".repeat(2049), "/search?q=" + "x".repeat(16385), "/health?q=xyz")) {
			assertThat(get(path).status()).as(path.substring(0, Math.min(path.length(), 80))).isEqualTo(400);
		}
		assertThat(get("/xyz").status()).isEqualTo(404);
		assertThat(get("/health/xyz").status()).isEqualTo(404);
	}

	@Test
	void returnsSafeFailureForVectorAndInternalErrorsWithoutBm25Fallback() throws IOException {
		server = new LocalSearchServer(new SearchService(factory, settings, texts -> {
			throw new EmbeddingException("acme-private-query p01.md");
		}));
		server.start("127.0.0.1", 0);
		assertThat(get("/search?q=" + encoded("임시저장") + "&mode=bm25").status()).isEqualTo(200);
		for (String mode : List.of("vector", "hybrid")) {
			var response = get("/search?q=acme-private-query&mode=" + mode);
			assertThat(response.status()).isEqualTo(503);
			assertThat(response.body()).doesNotContain("acme-private-query", "p01.md", "Exception");
		}
		server.close();
		server = new LocalSearchServer(new SearchService(factory, settings, fake) {
			@Override
			public List<SearchHit> searchHybrid(String query, int k, boolean company) {
				throw new IllegalStateException("acme-private-query p01.md");
			}
		});
		server.start("127.0.0.1", 0);
		var internal = get("/search?q=acme-private-query");
		assertThat(internal.status()).isEqualTo(500);
		assertThat(internal.body()).isEqualTo("{\"error\":\"검색을 완료하지 못했습니다\"}");
	}

	@Test
	void rejectsNonFiniteScoresInsteadOfSendingInvalidJson() throws IOException {
		server = new LocalSearchServer(new SearchService(factory, settings, fake) {
			@Override
			public List<SearchHit> searchHybrid(String query, int k, boolean company) {
				return List.of(new SearchHit("p01.md", "p01", "", Float.NaN, "본문", null));
			}
		});
		server.start("127.0.0.1", 0);
		var response = get("/search?q=xyz");
		assertThat(response.status()).isEqualTo(500);
		assertThat(response.body()).doesNotContain("NaN", "p01.md");
	}

	@Test
	void startsThroughCliAndClosesServerAndExecutorIdempotently() throws Exception {
		var thread = new AtomicReference<Thread>();
		var stub = new SearchService(factory, settings, fake) {
			@Override
			public List<SearchHit> searchHybrid(String query, int k, boolean company) {
				thread.set(Thread.currentThread());
				assertThat(k).isEqualTo(5);
				assertThat(company).isFalse();
				return List.of();
			}
		};
		server = new LocalSearchServer(stub);
		var router = new CommandRouter(indexer, stub, settings, server);
		assertThat(run(router, "serve", "--port", "0").code()).isZero();
		assertThat(server.isRunning()).isTrue();
		assertThat(get("/search?q=xyz").status()).isEqualTo(200);
		int port = server.port();
		assertThat(thread.get().isDaemon()).isFalse();
		server.close();
		server.close();
		thread.get().join(1500);
		assertThat(thread.get().isAlive()).isFalse();
		assertThat(server.isRunning()).isFalse();
		assertThatThrownBy(() -> new Socket("127.0.0.1", port)).isInstanceOf(IOException.class);
	}

	@Test
	void rejectsUnsafeBindAndCliErrorsBeforeStarting() {
		var router = new CommandRouter(indexer, service, settings, server);
		for (String[] args : List.of(new String[] {"serve", "--bind", "0.0.0.0"},
				new String[] {"serve", "--bind", "example.org"}, new String[] {"serve", "--port", "-1"},
				new String[] {"serve", "--port", "65536"}, new String[] {"serve", "--port", "xyz"},
				new String[] {"serve", "--port"}, new String[] {"serve", "--port", "0", "--port", "1"},
				new String[] {"serve", "--extra", "xyz"})) {
			var result = run(router, args);
			assertThat(result.code()).isEqualTo(2);
			assertThat(result.out()).isEmpty();
			assertThat(result.err()).contains("사용법:");
			assertThat(server.isRunning()).isFalse();
		}
	}

	@Test
	void occupiedPortProducesOperationalCliErrorAndDoesNotStartAnotherServer() throws IOException {
		server.start("127.0.0.1", 0);
		try (var another = new LocalSearchServer(service)) {
			var router = new CommandRouter(indexer, service, settings, another);
			var result = run(router, "serve", "--port", Integer.toString(server.port()));
			assertThat(result.code()).isEqualTo(1);
			assertThat(result.out()).isEmpty();
			assertThat(result.err()).doesNotContain("사용법:");
			assertThat(another.isRunning()).isFalse();
		}
	}

	@Test
	void springContextKeepsServeAliveAndDestroysServerWhenClosed() throws IOException {
		var application = new SpringApplication(SearchApplication.class);
		application.addInitializers(context -> context.getEnvironment().getPropertySources().addFirst(
				new MapPropertySource("synthetic", Map.of("search.index-dir", temp.resolve("spring-index").toString()))));
		try (var context = application.run("serve", "--port", "0")) {
			var managed = context.getBean(LocalSearchServer.class);
			assertThat(context.isActive()).isTrue();
			assertThat(managed.isRunning()).isTrue();
			server = managed;
			assertThat(get("/health").status()).isEqualTo(200);
		}
		assertThat(server.isRunning()).isFalse();
	}

	private Response get(String path) throws IOException {
		return request("GET", path, "Host: 127.0.0.1:" + server.port());
	}

	private Response request(String method, String path, String headers) throws IOException {
		try (var socket = new Socket("127.0.0.1", server.port())) {
			socket.setSoTimeout(5000);
			String request = method + " " + path + " HTTP/1.1\r\n" + headers + "\r\nConnection: close\r\n\r\n";
			socket.getOutputStream().write(request.getBytes(StandardCharsets.UTF_8));
			String response = new String(socket.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
			String[] sections = response.split("\r\n\r\n", 2);
			int status = Integer.parseInt(sections[0].split(" ")[1]);
			return new Response(status, sections[0].toLowerCase(java.util.Locale.ROOT), sections.length > 1 ? sections[1] : "");
		}
	}

	private String encoded(String query) {
		return URLEncoder.encode(query, StandardCharsets.UTF_8);
	}

	private Result run(CommandRouter router, String... args) {
		var out = new StringWriter();
		var err = new StringWriter();
		int code = router.run(args, new PrintWriter(out), new PrintWriter(err));
		return new Result(code, out.toString(), err.toString());
	}

	private record Response(int status, String headers, String body) {
	}

	private record Result(int code, String out, String err) {
	}
}
