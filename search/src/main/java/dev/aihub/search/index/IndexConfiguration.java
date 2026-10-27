package dev.aihub.search.index;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.analysis.AnalyzerFactory;

public record IndexConfiguration(String analysis, String model, String dimension) {

	public static IndexConfiguration capture(AnalyzerFactory factory, SearchSettings settings) {
		return new IndexConfiguration(factory.fingerprint(), settings.getEmbeddingModel(),
				Integer.toString(settings.getEmbeddingDimension()));
	}

	public List<String> rebuildReasons(Map<String, String> previous, boolean withVector) {
		List<String> reasons = new ArrayList<>();
		if (!(withVector ? "2" : "1").equals(previous.get("schema"))) {
			reasons.add("schema");
		}
		if (!analysis.equals(previous.get("analysis"))) {
			reasons.add("analysis");
		}
		if (withVector && !model.equals(previous.get("vector-model"))) {
			reasons.add("vector-model");
		}
		if (withVector && !dimension.equals(previous.get("vector-dimension"))) {
			reasons.add("vector-dimension");
		}
		return List.copyOf(reasons);
	}

	public void writeTo(Map<String, String> metadata, boolean withVector) {
		metadata.put("schema", withVector ? "2" : "1");
		metadata.put("analysis", analysis);
		metadata.remove("vector-model");
		metadata.remove("vector-dimension");
		if (withVector) {
			metadata.put("vector-model", model);
			metadata.put("vector-dimension", dimension);
		}
	}

	public void requireCompatible(Map<String, String> metadata, boolean vector) throws IOException {
		if (!"1".equals(metadata.get("schema")) && !"2".equals(metadata.get("schema"))) {
			throw new IOException("색인 스키마를 확인할 수 없습니다. index 로 재색인하세요");
		}
		if (!analysis.equals(metadata.get("analysis"))) {
			throw new IOException("분석기 설정이 색인과 다르거나 기록이 없습니다. index 로 재색인하세요");
		}
		if (vector && !"2".equals(metadata.get("schema"))) {
			throw new IOException("벡터 색인이 없습니다. --no-vector 없이 index 를 실행하세요");
		}
		if ("2".equals(metadata.get("schema"))) {
			if (!model.equals(metadata.get("vector-model")) || !dimension.equals(metadata.get("vector-dimension"))) {
				throw new IOException("벡터 모델·차원이 색인과 다르거나 기록이 없습니다. index 로 재색인하세요");
			}
		}
	}
}
