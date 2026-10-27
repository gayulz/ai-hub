package dev.aihub.search;

import java.net.URI;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Set;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;
import org.apache.lucene.analysis.ko.KoreanTokenizer.DecompoundMode;

@Component
@ConfigurationProperties(prefix = "search")
public class SearchSettings {

	private Path indexDir = Path.of(System.getProperty("user.home"), ".ai-hub", "index");
	private Path vault;
	private float bm25K1 = 1.2f;
	private float bm25B = 0.3f;
	private int chunkSoftLimit = 1200;
	private int chunkHardLimit = 2000;
	private float bodyBoost = 1.0f;
	private float headingBoost = 1.5f;
	private float noteTitleBoost = 2.0f;
	private String embeddingModel = "bge-m3";
	private String ollamaUrl = "http://127.0.0.1:11434";
	private int embeddingBatch = 8;
	private int embeddingDimension = 1024;
	private DecompoundMode decompoundMode = DecompoundMode.DISCARD;
	private Path userDictionary;

	public String getDecompoundMode() {
		return decompoundMode.name();
	}

	public void setDecompoundMode(String mode) {
		try {
			decompoundMode = DecompoundMode.valueOf(mode.toUpperCase(Locale.ROOT));
		} catch (IllegalArgumentException | NullPointerException exception) {
			throw new IllegalArgumentException("Nori 모드는 DISCARD, MIXED, NONE 중 하나여야 합니다");
		}
	}

	public Path getUserDictionary() {
		return userDictionary;
	}

	public void setUserDictionary(Path userDictionary) {
		this.userDictionary = userDictionary;
	}

	public String getEmbeddingModel() {
		return embeddingModel;
	}

	public void setEmbeddingModel(String embeddingModel) {
		if (embeddingModel == null || embeddingModel.isBlank()) {
			throw new IllegalArgumentException("임베딩 모델을 지정하세요");
		}
		this.embeddingModel = embeddingModel;
	}

	public String getOllamaUrl() {
		return ollamaUrl;
	}

	public void setOllamaUrl(String ollamaUrl) {
		URI uri;
		try {
			uri = URI.create(ollamaUrl);
		} catch (IllegalArgumentException | NullPointerException exception) {
			throw new IllegalArgumentException("Ollama 주소는 로컬 HTTP 주소여야 합니다");
		}
		String host = uri.getHost();
		if (host == null || !Set.of("127.0.0.1", "localhost", "::1", "[::1]").contains(host.toLowerCase(Locale.ROOT))
				|| !("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
				|| uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null) {
			throw new IllegalArgumentException("Ollama 주소는 127.0.0.1·localhost·::1 호스트만 허용합니다");
		}
		this.ollamaUrl = ollamaUrl;
	}

	public int getEmbeddingBatch() {
		return embeddingBatch;
	}

	public void setEmbeddingBatch(int embeddingBatch) {
		if (embeddingBatch <= 0) {
			throw new IllegalArgumentException("임베딩 배치 크기는 1 이상이어야 합니다");
		}
		this.embeddingBatch = embeddingBatch;
	}

	public int getEmbeddingDimension() {
		return embeddingDimension;
	}

	public void setEmbeddingDimension(int embeddingDimension) {
		if (embeddingDimension <= 0) {
			throw new IllegalArgumentException("임베딩 차원은 1 이상이어야 합니다");
		}
		this.embeddingDimension = embeddingDimension;
	}

	public Path getIndexDir() {
		return indexDir;
	}

	public void setIndexDir(Path indexDir) {
		this.indexDir = indexDir;
	}

	public Path getVault() {
		return vault;
	}

	public void setVault(Path vault) {
		this.vault = vault;
	}

	public float getBm25K1() {
		return bm25K1;
	}

	public void setBm25K1(float bm25K1) {
		if (!Float.isFinite(bm25K1) || bm25K1 < 0) {
			throw new IllegalArgumentException("BM25 k1은 0 이상의 유한한 값이어야 합니다");
		}
		this.bm25K1 = bm25K1;
	}

	public float getBm25B() {
		return bm25B;
	}

	public void setBm25B(float bm25B) {
		if (!Float.isFinite(bm25B) || bm25B < 0 || bm25B > 1) {
			throw new IllegalArgumentException("BM25 b는 0 이상 1 이하이어야 합니다");
		}
		this.bm25B = bm25B;
	}

	public int getChunkSoftLimit() {
		return chunkSoftLimit;
	}

	public void setChunkSoftLimit(int chunkSoftLimit) {
		this.chunkSoftLimit = chunkSoftLimit;
	}

	public int getChunkHardLimit() {
		return chunkHardLimit;
	}

	public void setChunkHardLimit(int chunkHardLimit) {
		this.chunkHardLimit = chunkHardLimit;
	}

	public float getBodyBoost() {
		return bodyBoost;
	}

	public void setBodyBoost(float bodyBoost) {
		this.bodyBoost = checkedBoost(bodyBoost);
	}

	public float getHeadingBoost() {
		return headingBoost;
	}

	public void setHeadingBoost(float headingBoost) {
		this.headingBoost = checkedBoost(headingBoost);
	}

	public float getNoteTitleBoost() {
		return noteTitleBoost;
	}

	public void setNoteTitleBoost(float noteTitleBoost) {
		this.noteTitleBoost = checkedBoost(noteTitleBoost);
	}

	private float checkedBoost(float value) {
		if (!Float.isFinite(value) || value <= 0) {
			throw new IllegalArgumentException("필드 가중치는 0보다 큰 유한한 값이어야 합니다");
		}
		return value;
	}
}
