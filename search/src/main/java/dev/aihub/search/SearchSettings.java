package dev.aihub.search;

import java.nio.file.Path;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

@Component
@ConfigurationProperties(prefix = "search")
public class SearchSettings {

	private Path indexDir = Path.of(System.getProperty("user.home"), ".ai-hub", "index");
	private Path vault;
	// 사용자 과제: k1·b 조정
	private float bm25K1 = 1.2f;
	private float bm25B = 0.75f;
	private int chunkSoftLimit = 1200;
	private int chunkHardLimit = 2000;
	private float bodyBoost = 1.0f;
	private float headingBoost = 1.5f;
	private float noteTitleBoost = 2.0f;

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
