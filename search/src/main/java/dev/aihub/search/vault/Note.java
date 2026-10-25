package dev.aihub.search.vault;

import java.util.List;

public record Note(String path, String title, String body, String type, String sensitivity,
		String created, List<String> tags, String hash) {

	public Note {
		tags = List.copyOf(tags);
	}
}
