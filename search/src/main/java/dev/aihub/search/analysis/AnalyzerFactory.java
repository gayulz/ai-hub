package dev.aihub.search.analysis;

import org.apache.lucene.analysis.Analyzer;

public interface AnalyzerFactory {

	// 반환한 분석기는 호출자가 닫는다.
	Analyzer analyzer();

	default String fingerprint() {
		return getClass().getName();
	}
}
