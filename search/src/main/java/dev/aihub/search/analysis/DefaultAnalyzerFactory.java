package dev.aihub.search.analysis;

import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.analysis.ko.KoreanAnalyzer;
import org.springframework.stereotype.Component;

@Component
public class DefaultAnalyzerFactory implements AnalyzerFactory {

	// 사용자 과제: 사용자 사전·복합어 처리(MIXED·DISCARD)·영문 소문자 필터는 6-2c 에서 사용자가 구현한다.
	// 참고: UserDictionary, KoreanTokenizer.DecompoundMode, LowerCaseFilter
	@Override
	public Analyzer analyzer() {
		return new KoreanAnalyzer();
	}
}
