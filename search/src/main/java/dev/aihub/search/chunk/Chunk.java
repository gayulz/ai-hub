package dev.aihub.search.chunk;

import java.util.List;

public record Chunk(String path, String noteTitle, String heading, String type, String sensitivity,
		String created, List<String> tags, int ordinal, String body, String hash) {

	public Chunk {
		tags = List.copyOf(tags);
	}
}
