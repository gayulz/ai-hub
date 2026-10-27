package dev.aihub.search.eval;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.text.Normalizer;

import org.junit.jupiter.api.Test;

class EvalRunnerTest {

	@Test
	void calculatesRecallAt5AndMrrForFourSyntheticQueries() {
		List<EvalQuery> queries = List.of(
				new EvalQuery("q1", "임시저장", false, List.of("acme/p01.md")),
				new EvalQuery("q2", "스프링부트", false, List.of("acme/p02.md")),
				new EvalQuery("q3", "설정", true, List.of("acme/p03.md")),
				new EvalQuery("q4", "색인", false, List.of("acme/p04.md")));
		Map<String, List<String>> rankings = Map.of(
				"임시저장", List.of("acme/p01.md", "xyz/p01.md", "xyz/p02.md", "xyz/p03.md", "xyz/p04.md"),
				"스프링부트", List.of("xyz/p01.md", "acme/p02.md", "xyz/p02.md", "xyz/p03.md", "xyz/p04.md"),
				"설정", List.of("xyz/p01.md", "xyz/p02.md", "xyz/p03.md", "xyz/p04.md", "acme/p03.md"),
				"색인", List.of("xyz/p01.md", "xyz/p02.md", "xyz/p03.md", "xyz/p04.md", "xyz/p05.md"));
		NoteSearcher searcher = (query, k, includeCompany) -> rankings.get(query).stream().limit(k).toList();
		EvalRunner runner = new EvalRunnerImpl();

		EvalReport report = runner.run(queries, searcher, 5);

		assertThat(report.recallAt5()).isCloseTo(0.75, within(1e-9));
		assertThat(report.mrr()).isCloseTo((1 + 0.5 + 0.2 + 0) / 4, within(1e-9));
		assertThat(report.mrr()).isCloseTo(0.425, within(1e-9));
	}

	@Test
	void limitsOverlongRankingsAndUsesFirstOfMultipleNormalizedAnswers() {
		String decomposed = Normalizer.normalize("설정", Normalizer.Form.NFD);
		var queries = List.of(new EvalQuery("q1", decomposed, true, List.of("xyz/p01.md", "acme/설정.md")),
				new EvalQuery("q2", "색인", false, List.of("acme/p01.md")));
		var report = new EvalRunnerImpl().run(queries, (query, k, company) -> {
			assertThat(k).isEqualTo(2);
			if (company) {
				assertThat(query).isEqualTo("설정");
				return List.of("xyz/p02.md", "acme/" + decomposed + ".md", "xyz/p01.md");
			}
			return List.of("xyz/p02.md", "xyz/p03.md", "acme/p01.md");
		}, 2);
		assertThat(report.recallAt5()).isEqualTo(0.5);
		assertThat(report.mrr()).isEqualTo(0.25);
		assertThat(report.rows().getFirst().rankedPaths()).containsExactly("xyz/p02.md", "acme/설정.md");
		assertThat(report.rows().get(1).reciprocalRank()).isZero();
	}

	@Test
	void handlesShortEmptyResultsAndNoQueriesWithoutDividingByZero() {
		var query = new EvalQuery("q1", "임시저장", false, List.of("p01.md", "p02.md"));
		var runner = new EvalRunnerImpl();
		assertThat(runner.run(List.of(query), (text, k, company) -> List.of("p02.md"), 5).mrr()).isEqualTo(1);
		var emptyHits = runner.run(List.of(query), (text, k, company) -> List.of(), 5);
		assertThat(emptyHits.recallAt5()).isZero();
		assertThat(emptyHits.mrr()).isZero();
		var emptyQueries = runner.run(List.of(), (text, k, company) -> {
			throw new AssertionError("빈 질의를 검색했습니다");
		}, 5);
		assertThat(emptyQueries.rows()).isEmpty();
		assertThat(emptyQueries.recallAt5()).isZero();
		assertThat(emptyQueries.mrr()).isZero();
		for (int k : new int[] {0, -1}) {
			assertThatThrownBy(() -> runner.run(List.of(), (text, limit, company) -> List.of(), k))
					.isInstanceOf(IllegalArgumentException.class);
		}
	}

	@Test
	void copiesAnswersRankingsAndRowsAsImmutableResults() {
		var answers = new ArrayList<>(List.of("p01.md"));
		var ranked = new ArrayList<>(List.of("p01.md"));
		var query = new EvalQuery("q1", "색인", false, answers);
		var report = new EvalRunnerImpl().run(List.of(query), (text, k, company) -> ranked, 5);
		answers.clear();
		ranked.clear();
		assertThat(query.answers()).containsExactly("p01.md");
		assertThat(report.rows().getFirst().rankedPaths()).containsExactly("p01.md");
		assertThatThrownBy(() -> report.rows().clear()).isInstanceOf(UnsupportedOperationException.class);
		assertThatThrownBy(() -> report.rows().getFirst().rankedPaths().clear())
				.isInstanceOf(UnsupportedOperationException.class);
	}
}
