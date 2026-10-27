package dev.aihub.search.query;

import java.io.IOException;
import java.util.List;
import java.util.Locale;

public enum SearchMode {
	BM25, VECTOR, HYBRID;

	public static SearchMode parse(String value) {
		return switch (value) {
			case "bm25" -> BM25;
			case "vector" -> VECTOR;
			case "hybrid" -> HYBRID;
			default -> throw new IllegalArgumentException("검색 모드는 bm25, vector, hybrid 중 하나여야 합니다");
		};
	}

	public List<SearchHit> search(SearchService service, String query, int k, boolean includeCompany) throws IOException {
		return switch (this) {
			case BM25 -> service.searchNotes(query, k, includeCompany);
			case VECTOR -> service.searchVector(query, k, includeCompany);
			case HYBRID -> service.searchHybrid(query, k, includeCompany);
		};
	}

	@Override
	public String toString() {
		return name().toLowerCase(Locale.ROOT);
	}
}
