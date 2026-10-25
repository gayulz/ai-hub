package dev.aihub.search.eval;

import java.util.List;

public class UnimplementedEvalRunner implements EvalRunner {

	@Override
	public EvalReport run(List<EvalQuery> queries, NoteSearcher searcher, int k) {
		throw new UnsupportedOperationException("사용자 과제");
	}
}
