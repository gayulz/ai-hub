package dev.aihub.search.query;

public record SearchHit(String path, String noteTitle, String heading, float score, String snippet,
		String sensitivity) {
}
