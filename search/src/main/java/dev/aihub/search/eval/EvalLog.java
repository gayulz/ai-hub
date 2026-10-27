package dev.aihub.search.eval;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.OffsetDateTime;
import java.util.Locale;

import dev.aihub.search.SearchSettings;

public final class EvalLog {

	public void append(Path log, String mode, int k, SearchSettings settings, String analysis,
			EvalReport report) throws IOException {
		StringBuilder text = new StringBuilder();
		text.append("\n## ").append(OffsetDateTime.now()).append("\n\n");
		text.append("- 모드: ").append(mode).append(" · k: ").append(k).append('\n');
		text.append("- Nori: ").append(settings.getDecompoundMode()).append(" · fingerprint: ")
				.append(analysis).append('\n');
		text.append(String.format(Locale.ROOT, "- BM25: k1=%s · b=%s · body=%s · heading=%s · title=%s%n",
				settings.getBm25K1(), settings.getBm25B(), settings.getBodyBoost(), settings.getHeadingBoost(),
				settings.getNoteTitleBoost()));
		text.append("- 모델: ").append(settings.getEmbeddingModel().replaceAll("[\\r\\n`|]", "?"))
				.append(" · 차원: ").append(settings.getEmbeddingDimension()).append('\n');
		text.append(String.format(Locale.ROOT, "- Hit@%d (recallAt5): %.6f · MRR@%d: %.6f%n%n",
				k, report.recallAt5(), k, report.mrr()));
		text.append("| ID | 적중 | 역순위 |\n|---|---:|---:|\n");
		for (var row : report.rows()) {
			text.append(String.format(Locale.ROOT, "| %s | %.0f | %.6f |%n", row.id(), row.recallAt5(),
					row.reciprocalRank()));
		}
		try {
			Path parent = log.toAbsolutePath().getParent();
			Files.createDirectories(parent);
			Files.writeString(log, text, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
		} catch (IOException exception) {
			throw new IOException("평가 점수 로그를 기록하지 못했습니다");
		}
	}
}
