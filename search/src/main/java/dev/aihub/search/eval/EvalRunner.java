package dev.aihub.search.eval;

import java.util.List;

public interface EvalRunner {

	EvalReport run(List<EvalQuery> queries, NoteSearcher searcher, int k);
}
