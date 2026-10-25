package dev.aihub.search.query;

import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.Term;
import org.apache.lucene.search.BooleanClause.Occur;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.BoostQuery;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.Directory;
import org.apache.lucene.store.FSDirectory;
import org.apache.lucene.util.QueryBuilder;
import org.springframework.stereotype.Service;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.AnalyzerFactory;

@Service
public class SearchService {

	private final AnalyzerFactory analyzerFactory;
	private final SearchSettings settings;

	public SearchService(AnalyzerFactory analyzerFactory, SearchSettings settings) {
		this.analyzerFactory = analyzerFactory;
		this.settings = settings;
	}

	public List<SearchHit> searchChunks(String query, int k, boolean includeCompany) throws IOException {
		return search(query, k, includeCompany, false);
	}

	public List<SearchHit> searchNotes(String query, int k, boolean includeCompany) throws IOException {
		return search(query, k, includeCompany, true);
	}

	private List<SearchHit> search(String text, int k, boolean includeCompany, boolean groupNotes) throws IOException {
		if (text == null || k <= 0) {
			throw new IllegalArgumentException("질의와 1 이상의 검색 건수를 주세요");
		}
		if (!Files.isDirectory(settings.getIndexDir())) {
			throw missingIndex();
		}
		try (Directory directory = FSDirectory.open(settings.getIndexDir());
				Analyzer analyzer = analyzerFactory.analyzer();
				DirectoryReader reader = openReader(directory)) {
			Query query = buildQuery(text, includeCompany, analyzer);
			if (query == null) {
				return List.of();
			}
			IndexSearcher searcher = new IndexSearcher(reader);
			searcher.setSimilarity(new BM25Similarity(settings.getBm25K1(), settings.getBm25B()));
			return collect(searcher, query, k, groupNotes);
		}
	}

	private List<SearchHit> collect(IndexSearcher searcher, Query query, int k, boolean groupNotes) throws IOException {
		List<SearchHit> hits = new ArrayList<>();
		var paths = new HashSet<String>();
		ScoreDoc after = null;
		int pageSize = groupNotes ? Math.max(k, 32) : k;
		var fields = searcher.storedFields();
		while (hits.size() < k) {
			var page = searcher.searchAfter(after, query, pageSize);
			if (page.scoreDocs.length == 0) {
				break;
			}
			for (ScoreDoc scoreDoc : page.scoreDocs) {
				var document = fields.document(scoreDoc.doc);
				String path = document.get("path");
				if (!groupNotes || paths.add(path)) {
					hits.add(new SearchHit(path, document.get("noteTitle"), document.get("heading"),
							scoreDoc.score, snippet(document.get("body")), document.get("sensitivity")));
					if (hits.size() == k) {
						break;
					}
				}
			}
			if (page.scoreDocs.length < pageSize) {
				break;
			}
			after = page.scoreDocs[page.scoreDocs.length - 1];
		}
		return List.copyOf(hits);
	}

	Query buildQuery(String text, boolean includeCompany, Analyzer analyzer) {
		var query = new BooleanQuery.Builder();
		var builder = new QueryBuilder(analyzer);
		addField(query, builder, "body", text, settings.getBodyBoost());
		addField(query, builder, "heading", text, settings.getHeadingBoost());
		addField(query, builder, "noteTitle", text, settings.getNoteTitleBoost());
		if (query.build().clauses().isEmpty()) {
			return null;
		}
		query.setMinimumNumberShouldMatch(1);
		if (!includeCompany) {
			query.add(new TermQuery(new Term("sensitivity", "company")), Occur.MUST_NOT);
		}
		return query.build();
	}

	private void addField(BooleanQuery.Builder query, QueryBuilder builder, String field, String text, float boost) {
		Query fieldQuery = builder.createBooleanQuery(field, text);
		if (fieldQuery != null) {
			query.add(new BoostQuery(fieldQuery, boost), Occur.SHOULD);
		}
	}

	private String snippet(String body) {
		String line = body.replaceAll("\\s+", " ").strip();
		int end = line.offsetByCodePoints(0, Math.min(120, line.codePointCount(0, line.length())));
		return line.substring(0, end);
	}

	private DirectoryReader openReader(Directory directory) throws IOException {
		if (!DirectoryReader.indexExists(directory)) {
			throw missingIndex();
		}
		return DirectoryReader.open(directory);
	}

	private IOException missingIndex() {
		return new IOException("색인이 없습니다. 먼저 index 를 실행하세요");
	}
}
