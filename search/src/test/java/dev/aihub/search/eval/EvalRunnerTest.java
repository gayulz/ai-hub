package dev.aihub.search.eval;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

@Disabled("사용자 과제")
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
		EvalRunner runner = new UnimplementedEvalRunner();

		EvalReport report = runner.run(queries, searcher, 5);

		assertThat(report.recallAt5()).isCloseTo(0.75, within(1e-9));
		assertThat(report.mrr()).isCloseTo((1 + 0.5 + 0.2 + 0) / 4, within(1e-9));
	}
}
