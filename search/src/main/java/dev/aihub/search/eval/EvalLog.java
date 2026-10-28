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
			EvalReport report, String inputHash, EvalContext.Executable executable, EvalContext.IndexState index) throws IOException {
		validate(inputHash, executable, index);
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
		text.append("- 평가 입력 SHA-256: ").append(inputHash).append('\n');
		text.append("- 실행 파일: ");
		if ("jar".equals(executable.kind())) {
			text.append("JAR SHA-256 ").append(executable.identifier());
		} else {
			text.append("빌드 ").append(executable.identifier()).append(" · 빌드 시각 ").append(executable.buildTime());
		}
		text.append('\n').append("- 색인 커밋 파일 시각(UTC, 근사): ").append(index.modifiedTime()).append('\n');
		text.append(String.format(Locale.ROOT, "- Hit@%d (recallAt5): %.6f · MRR@%d: %.6f%n%n",
				k, report.recallAt5(), k, report.mrr()));
		text.append("| 순번 | 적중 | 역순위 |\n|---|---:|---:|\n");
		int number = 0;
		for (var row : report.rows()) {
			text.append(String.format(Locale.ROOT, "| %d | %.0f | %.6f |%n", ++number, row.recallAt5(),
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
	private void validate(String hash, EvalContext.Executable executable, EvalContext.IndexState index) throws IOException {
		if (hash == null || !hash.matches("[0-9a-f]{12}") || executable == null || index == null
				|| index.modifiedTime() == null || index.timeSource() != EvalContext.TimeSource.SEGMENTS_FILE_MTIME_APPROXIMATION) {
			throw new IOException("평가 조건을 확인하지 못했습니다");
		}
		String value = executable.identifier();
		boolean jar = "jar".equals(executable.kind()) && value != null && value.matches("[0-9a-f]{12}")
				&& executable.buildTime() == null;
		boolean build = "build".equals(executable.kind()) && value != null
				&& value.matches("[A-Za-z0-9][A-Za-z0-9._+-]{0,79}") && executable.buildTime() != null;
		if (!jar && !build) {
			throw new IOException("평가 조건을 확인하지 못했습니다");
		}
	}

}
