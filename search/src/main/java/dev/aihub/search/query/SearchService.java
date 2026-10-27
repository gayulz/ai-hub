package dev.aihub.search.query;

import java.io.IOException;
import java.nio.file.Files;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.Term;
import org.apache.lucene.search.BooleanClause.Occur;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.BoostQuery;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.KnnFloatVectorQuery;
import org.apache.lucene.search.MatchAllDocsQuery;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.similarities.BM25Similarity;
import org.apache.lucene.store.Directory;
import org.apache.lucene.store.FSDirectory;
import org.apache.lucene.util.QueryBuilder;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.AnalyzerFactory;
import dev.aihub.search.index.IndexConfiguration;
import dev.aihub.search.vector.Embedder;
import dev.aihub.search.vector.EmbeddingException;
import dev.aihub.search.vector.OllamaEmbedder;

@Service
public class SearchService {

	private final AnalyzerFactory analyzerFactory;
	private final SearchSettings settings;
	private final Embedder embedder;

	public SearchService(AnalyzerFactory analyzerFactory, SearchSettings settings) {
		this(analyzerFactory, settings, new OllamaEmbedder(settings));
	}

	@Autowired
	public SearchService(AnalyzerFactory analyzerFactory, SearchSettings settings, Embedder embedder) {
		this.analyzerFactory = analyzerFactory;
		this.settings = settings;
		this.embedder = embedder;
	}

	public List<SearchHit> searchChunks(String query, int k, boolean includeCompany) throws IOException {
		return search(query, k, includeCompany, false);
	}

	public List<SearchHit> searchNotes(String query, int k, boolean includeCompany) throws IOException {
		return search(query, k, includeCompany, true);
	}

	public String analyzerFingerprint() {
		return analyzerFactory.fingerprint();
	}

	public List<SearchHit> searchVector(String query, int k, boolean includeCompany) throws IOException {
		if (query == null || k <= 0) {
			throw new IllegalArgumentException("질의와 1 이상의 검색 건수를 주세요");
		}
		if (!Files.isDirectory(settings.getIndexDir())) {
			throw missingIndex();
		}
		var configuration = IndexConfiguration.capture(analyzerFactory, settings);
		try (Directory directory = FSDirectory.open(settings.getIndexDir());
				DirectoryReader reader = openReader(directory)) {
			configuration.requireCompatible(reader.getIndexCommit().getUserData(), true);
			var vectors = embedder.embed(List.of(Normalizer.normalize(query, Normalizer.Form.NFC)));
			if (vectors.size() != 1) {
				throw new EmbeddingException("질의 임베딩 응답 개수가 1이 아닙니다");
			}
			float[] vector = vectors.getFirst();
			if (vector == null || vector.length != settings.getEmbeddingDimension()) {
				throw new EmbeddingException("질의 임베딩 벡터 차원이 설정과 다릅니다");
			}
			Query filter = includeCompany ? null : new BooleanQuery.Builder()
					.add(new MatchAllDocsQuery(), Occur.MUST)
					.add(new TermQuery(new Term("sensitivity", "company")), Occur.MUST_NOT).build();
			int candidates = (int) Math.min((long) k * 5, 100);
			var vectorQuery = new KnnFloatVectorQuery("vector", vector, candidates, filter);
			var hits = collect(new IndexSearcher(reader), vectorQuery, k, true);
			requireUnchanged(configuration);
			return hits;
		}
	}

	public List<SearchHit> searchHybrid(String query, int k, boolean includeCompany) throws IOException {
		if (query == null || k <= 0) {
			throw new IllegalArgumentException("질의와 1 이상의 검색 건수를 주세요");
		}
		var configuration = IndexConfiguration.capture(analyzerFactory, settings);
		var bm25 = searchNotes(query, 100, includeCompany);
		var vector = searchVector(query, 100, includeCompany);
		requireUnchanged(configuration);
		return fuse(bm25, vector, k);
	}

	static List<SearchHit> fuse(List<SearchHit> bm25, List<SearchHit> vector, int k) {
		var scores = new HashMap<String, Double>();
		var notes = new HashMap<String, SearchHit>();
		for (var ranking : List.of(bm25, vector)) {
			var seen = new HashSet<String>();
			int rank = 0;
			for (var hit : ranking) {
				if (!seen.add(hit.path())) {
					continue;
				}
				if (++rank > 100) {
					break;
				}
				notes.putIfAbsent(hit.path(), hit);
				scores.merge(hit.path(), 1.0 / (60 + rank), Double::sum);
			}
		}
		return scores.entrySet().stream()
				.sorted(java.util.Map.Entry.<String, Double>comparingByValue(Comparator.reverseOrder())
						.thenComparing(java.util.Map.Entry.comparingByKey()))
				.limit(k).map(entry -> {
					var hit = notes.get(entry.getKey());
					return new SearchHit(hit.path(), hit.noteTitle(), hit.heading(), entry.getValue().floatValue(),
							hit.snippet(), hit.sensitivity());
				}).toList();
	}

	private List<SearchHit> search(String text, int k, boolean includeCompany, boolean groupNotes) throws IOException {
		if (text == null || k <= 0) {
			throw new IllegalArgumentException("질의와 1 이상의 검색 건수를 주세요");
		}
		if (!Files.isDirectory(settings.getIndexDir())) {
			throw missingIndex();
		}
		var configuration = IndexConfiguration.capture(analyzerFactory, settings);
		try (Directory directory = FSDirectory.open(settings.getIndexDir());
				Analyzer analyzer = analyzerFactory.analyzer();
				DirectoryReader reader = openReader(directory)) {
			configuration.requireCompatible(reader.getIndexCommit().getUserData(), false);
			Query query = buildQuery(Normalizer.normalize(text, Normalizer.Form.NFC), includeCompany, analyzer);
			if (query == null) {
				requireUnchanged(configuration);
				return List.of();
			}
			IndexSearcher searcher = new IndexSearcher(reader);
			searcher.setSimilarity(new BM25Similarity(settings.getBm25K1(), settings.getBm25B()));
			var hits = collect(searcher, query, k, groupNotes);
			requireUnchanged(configuration);
			return hits;
		}
	}

	private void requireUnchanged(IndexConfiguration configuration) throws IOException {
		if (!configuration.equals(IndexConfiguration.capture(analyzerFactory, settings))) {
			throw new IOException("검색 중 설정이 바뀌었습니다. index 로 재색인하세요");
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
