package dev.aihub.search.eval;

import java.util.List;

public record EvalReport(double recallAt5, double mrr, List<EvalRow> rows) {

	public EvalReport {
		rows = List.copyOf(rows);
	}
}
