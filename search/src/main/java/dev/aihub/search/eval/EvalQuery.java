package dev.aihub.search.eval;

import java.util.List;

public record EvalQuery(String id, String text, boolean includeCompany, List<String> answers) {

	public EvalQuery {
		answers = List.copyOf(answers);
	}
}
