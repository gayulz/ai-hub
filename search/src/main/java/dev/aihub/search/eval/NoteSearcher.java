package dev.aihub.search.eval;

import java.util.List;

@FunctionalInterface
public interface NoteSearcher {

	List<String> topNotes(String query, int k, boolean includeCompany);
}
