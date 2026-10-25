package dev.aihub.search.eval;

import java.util.List;

public record EvalRow(String id, List<String> rankedPaths, double recallAt5, double reciprocalRank) {

	public EvalRow {
		rankedPaths = List.copyOf(rankedPaths);
	}
}
