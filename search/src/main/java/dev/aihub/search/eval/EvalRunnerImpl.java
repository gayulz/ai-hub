package dev.aihub.search.eval;

import java.util.ArrayList;
import java.util.List;
import java.text.Normalizer;

public class EvalRunnerImpl implements EvalRunner {

	@Override
	public EvalReport run(List<EvalQuery> queries, NoteSearcher searcher, int k) {
		if (k <= 0) {
			throw new IllegalArgumentException("평가 k는 1 이상이어야 합니다");
		}
		List<EvalRow> rows = new ArrayList<>();

		for (EvalQuery query : queries) {
			List<String> ranked = searcher.topNotes(nfc(query.text()), k, query.includeCompany())
					.stream().limit(k).map(this::nfc).toList();
			var answers = query.answers().stream().map(this::nfc).toList();
			int rankIndex = -1;
			for (int i = 0; i < ranked.size(); i++) {
				if (answers.contains(ranked.get(i))) {
					rankIndex = i;
					break;
				}
			}

			double recall = rankIndex == -1 ? 0.0 : 1.0;
			double reciprocalRank = rankIndex == -1 ? 0.0 : 1.0 / (rankIndex + 1);
			rows.add(new EvalRow(nfc(query.id()), ranked, recall, reciprocalRank));
		}

		double recallAt5 = rows.stream().mapToDouble(EvalRow::recallAt5).average().orElse(0.0);
		double mrr = rows.stream().mapToDouble(EvalRow::reciprocalRank).average().orElse(0.0);
		return new EvalReport(recallAt5, mrr, rows);
	}

	private String nfc(String text) {
		return Normalizer.normalize(text, Normalizer.Form.NFC);
	}
}
