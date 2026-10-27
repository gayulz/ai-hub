package dev.aihub.search.vector;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.stream.Collectors;

import org.springframework.stereotype.Component;

import dev.aihub.search.SearchSettings;

@Component
public class OllamaEmbedder implements Embedder {

	private static final char[] HEX = "0123456789abcdef".toCharArray();
	private final SearchSettings settings;
	private final HttpClient client;

	public OllamaEmbedder(SearchSettings settings) {
		this.settings = settings;
		client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5))
				.followRedirects(HttpClient.Redirect.NEVER).proxy(ProxySelector.of(null)).build();
	}

	@Override
	public List<float[]> embed(List<String> texts) throws EmbeddingException {
		List<float[]> vectors = new ArrayList<>();
		for (int start = 0; start < texts.size();) {
			int end = (int) Math.min((long) start + settings.getEmbeddingBatch(), texts.size());
			vectors.addAll(batch(texts.subList(start, end)));
			start = end;
		}
		return List.copyOf(vectors);
	}

	private List<float[]> batch(List<String> texts) throws EmbeddingException {
		String input = texts.stream().map(this::quoted).collect(Collectors.joining(","));
		String body = "{\"model\":" + quoted(settings.getEmbeddingModel()) + ",\"input\":[" + input + "]}";
		var request = HttpRequest.newBuilder(URI.create(settings.getOllamaUrl().replaceAll("/+$", "") + "/api/embed"))
				.timeout(Duration.ofSeconds(120)).header("Content-Type", "application/json")
				.POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
		var pending = client.sendAsync(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
		HttpResponse<String> response;
		try {
			// 헤더가 먼저 도착해도 응답 본문을 읽는 시간까지 제한한다.
			response = pending.get(120, TimeUnit.SECONDS);
		} catch (InterruptedException exception) {
			pending.cancel(true);
			Thread.currentThread().interrupt();
			throw new EmbeddingException("Ollama 임베딩 요청이 중단되었습니다");
		} catch (ExecutionException | TimeoutException exception) {
			pending.cancel(true);
			throw new EmbeddingException("로컬 Ollama에 연결하거나 임베딩 응답을 읽지 못했습니다");
		}
		if (response.statusCode() < 200 || response.statusCode() >= 300) {
			throw new EmbeddingException("Ollama 임베딩 요청 실패: HTTP " + response.statusCode());
		}
		return EmbeddingResponse.parse(response.body(), texts.size(), settings.getEmbeddingDimension());
	}

	private String quoted(String text) {
		StringBuilder json = new StringBuilder("\"");
		for (int i = 0; i < text.length(); i++) {
			char value = text.charAt(i);
			switch (value) {
				case '"' -> json.append("\\\"");
				case '\\' -> json.append("\\\\");
				case '\b' -> json.append("\\b");
				case '\f' -> json.append("\\f");
				case '\n' -> json.append("\\n");
				case '\r' -> json.append("\\r");
				case '\t' -> json.append("\\t");
				default -> {
					if (value < 0x20 || value > 0x7e) {
						json.append("\\u").append(HEX[value >> 12]).append(HEX[(value >> 8) & 15])
								.append(HEX[(value >> 4) & 15]).append(HEX[value & 15]);
					} else {
						json.append(value);
					}
				}
			}
		}
		return json.append('"').toString();
	}
}
