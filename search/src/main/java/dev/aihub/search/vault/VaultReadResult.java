package dev.aihub.search.vault;

import java.util.List;
import java.util.Set;

public record VaultReadResult(List<Note> notes, Set<String> unreadablePaths) {

	public VaultReadResult {
		notes = List.copyOf(notes);
		unreadablePaths = Set.copyOf(unreadablePaths);
	}

	public int skippedFiles() {
		return unreadablePaths.size();
	}
}
