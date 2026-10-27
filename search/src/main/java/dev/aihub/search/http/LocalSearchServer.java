package dev.aihub.search.http;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.springframework.beans.factory.DisposableBean;
import org.springframework.stereotype.Component;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import dev.aihub.search.query.SearchHit;
import dev.aihub.search.query.SearchMode;
import dev.aihub.search.query.SearchService;

@Component
public class LocalSearchServer implements AutoCloseable, DisposableBean {

	private static final Set<String> HOSTS = Set.of("127.0.0.1", "localhost", "::1", "[::1]");
	private static final Set<String> PARAMETERS = Set.of("q", "k", "mode", "includeCompany");
	private final SearchService service;
	private HttpServer server;
	private ExecutorService executor;

	public LocalSearchServer(SearchService service) {
		this.service = service;
	}

	public synchronized void start(String bind, int port) throws IOException {
		start(bind, port, false);
	}

	public synchronized void start(String bind, int port, boolean allowCompany) throws IOException {
		if (!HOSTS.contains(bind) || port < 0 || port > 65535) {
			throw new IllegalArgumentException("서버는 로컬 호스트와 0~65535 포트만 허용합니다");
		}
		if (server != null) {
			throw new IllegalArgumentException("검색 서버가 이미 실행 중입니다");
		}
		var address = new InetSocketAddress(bind, port);
		if (address.isUnresolved() || !address.getAddress().isLoopbackAddress()) {
			throw new IllegalArgumentException("서버는 루프백에만 바인딩할 수 있습니다");
		}
		HttpServer candidate = HttpServer.create(address, 16);
		ExecutorService workers = Executors.newFixedThreadPool(4);
		try {
			candidate.setExecutor(workers);
			candidate.createContext("/", exchange -> handle(exchange, allowCompany));
			candidate.start();
			server = candidate;
			executor = workers;
		} catch (RuntimeException exception) {
			candidate.stop(0);
			workers.shutdownNow();
			throw new IOException("로컬 검색 서버를 시작하지 못했습니다");
		}
	}

	public synchronized boolean isRunning() {
		return server != null;
	}

	public synchronized int port() {
		if (server == null) {
			throw new IllegalStateException("검색 서버가 실행 중이 아닙니다");
		}
		return server.getAddress().getPort();
	}

	private void handle(HttpExchange exchange, boolean allowCompany) throws IOException {
		try (exchange) {
			if (!exchange.getRemoteAddress().getAddress().isLoopbackAddress()
					|| !allowedHeader(exchange, "Host", false) || !allowedHeader(exchange, "Origin", true)) {
				send(exchange, 403, "{\"error\":\"로컬 요청만 허용합니다\"}");
				return;
			}
			if (!exchange.getRequestMethod().equals("GET")) {
				exchange.getResponseHeaders().set("Allow", "GET");
				send(exchange, 405, "{\"error\":\"GET만 허용합니다\"}");
				return;
			}
			try {
				URI uri = exchange.getRequestURI();
				if (uri.isAbsolute() || uri.getRawFragment() != null) {
					throw new BadRequest();
				}
				switch (uri.getRawPath()) {
					case "/health" -> {
						if (uri.getRawQuery() != null) {
							throw new BadRequest();
						}
						send(exchange, 200, "{\"status\":\"ok\"}");
					}
					case "/search" -> {
						var request = parse(uri.getRawQuery());
						if (request.company() && !allowCompany) {
							send(exchange, 403, "{\"error\":\"회사 노트 조회는 허용되지 않습니다\"}");
							return;
						}
						send(exchange, 200, json(request.mode().search(service, request.query(), request.k(), request.company())));
					}
					default -> send(exchange, 404, "{\"error\":\"경로가 없습니다\"}");
				}
			} catch (BadRequest exception) {
				send(exchange, 400, "{\"error\":\"검색 인자를 확인하세요\"}");
			} catch (IOException exception) {
				send(exchange, 503, "{\"error\":\"검색을 완료하지 못했습니다. 색인·설정을 확인하세요\"}");
			} catch (RuntimeException exception) {
				send(exchange, 500, "{\"error\":\"검색을 완료하지 못했습니다\"}");
			}
		}
	}

	private boolean allowedHeader(HttpExchange exchange, String name, boolean origin) {
		var values = exchange.getRequestHeaders().get(name);
		if (values == null) {
			return origin;
		}
		if (values.size() != 1 || values.getFirst().length() > 128) {
			return false;
		}
		try {
			var uri = URI.create((origin ? "" : "http://") + values.getFirst());
			int targetPort = uri.getPort() == -1 ? 80 : uri.getPort();
			return "http".equals(uri.getScheme()) && uri.getHost() != null && HOSTS.contains(uri.getHost())
					&& targetPort == exchange.getLocalAddress().getPort()
					&& uri.getRawUserInfo() == null && uri.getRawQuery() == null && uri.getRawFragment() == null
					&& uri.getRawPath().isEmpty();
		} catch (IllegalArgumentException exception) {
			return false;
		}
	}

	private Request parse(String raw) {
		try {
			return parameters(raw);
		} catch (IllegalArgumentException exception) {
			throw new BadRequest();
		}
	}

	private Request parameters(String raw) {
		if (raw == null || raw.length() > 16384) {
			throw new IllegalArgumentException();
		}
		var parameters = new HashMap<String, String>();
		for (String pair : raw.split("&", -1)) {
			String[] parts = pair.split("=", 2);
			if (parts.length != 2) {
				throw new IllegalArgumentException();
			}
			String key = decode(parts[0]);
			String value = decode(parts[1]);
			if (!PARAMETERS.contains(key) || parameters.putIfAbsent(key, value) != null) {
				throw new IllegalArgumentException();
			}
		}
		String query = parameters.get("q");
		if (query == null || query.isBlank() || query.length() > 2048 || query.codePoints().anyMatch(Character::isISOControl)) {
			throw new IllegalArgumentException();
		}
		String rawK = parameters.getOrDefault("k", "5");
		if (!rawK.matches("[0-9]{1,3}")) {
			throw new IllegalArgumentException();
		}
		int k = Integer.parseInt(rawK);
		if (k < 1 || k > 100) {
			throw new IllegalArgumentException();
		}
		var mode = SearchMode.parse(parameters.getOrDefault("mode", "hybrid"));
		String company = parameters.getOrDefault("includeCompany", "false");
		if (!company.equals("true") && !company.equals("false")) {
			throw new IllegalArgumentException();
		}
		return new Request(query, k, mode, Boolean.parseBoolean(company));
	}

	private String decode(String value) {
		String decoded = URLDecoder.decode(value, StandardCharsets.UTF_8);
		if (decoded.indexOf('\ufffd') >= 0) {
			throw new IllegalArgumentException();
		}
		return decoded;
	}

	private String json(List<SearchHit> hits) {
		var json = new StringBuilder("{\"hits\":[");
		for (var hit : hits) {
			if (!Float.isFinite(hit.score())) {
				throw new IllegalStateException("검색 점수를 표시할 수 없습니다");
			}
			if (json.length() > 9) {
				json.append(',');
			}
			json.append("{\"path\":").append(quoted(hit.path()))
					.append(",\"noteTitle\":").append(quoted(hit.noteTitle()))
					.append(",\"heading\":").append(quoted(hit.heading()))
					.append(",\"score\":").append(hit.score())
					.append(",\"snippet\":").append(quoted(hit.snippet()))
					.append(",\"sensitivity\":").append(quoted(hit.sensitivity())).append('}');
		}
		return json.append("]}").toString();
	}

	private String quoted(String value) {
		if (value == null) {
			return "null";
		}
		var text = new StringBuilder("\"");
		for (char character : value.toCharArray()) {
			if (character == '\\' || character == '"') {
				text.append('\\').append(character);
			} else if (character < 32 || Character.isSurrogate(character)) {
				text.append(String.format("\\u%04x", (int) character));
			} else {
				text.append(character);
			}
		}
		return text.append('"').toString();
	}

	private void send(HttpExchange exchange, int status, String json) throws IOException {
		byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
		exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
		exchange.getResponseHeaders().set("Cache-Control", "no-store");
		exchange.getResponseHeaders().set("X-Content-Type-Options", "nosniff");
		exchange.sendResponseHeaders(status, bytes.length);
		exchange.getResponseBody().write(bytes);
	}

	@Override
	public void close() {
		HttpServer running;
		ExecutorService workers;
		synchronized (this) {
			running = server;
			workers = executor;
			server = null;
			executor = null;
		}
		if (running != null) {
			running.stop(0);
			workers.shutdownNow();
			try {
				workers.awaitTermination(3, TimeUnit.SECONDS);
			} catch (InterruptedException exception) {
				Thread.currentThread().interrupt();
			}
		}
	}

	@Override
	public void destroy() {
		close();
	}

	private record Request(String query, int k, SearchMode mode, boolean company) {
	}

	private static class BadRequest extends RuntimeException {
	}
}
