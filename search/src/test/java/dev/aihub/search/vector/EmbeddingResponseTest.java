package dev.aihub.search.vector;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.List;

import org.junit.jupiter.api.Test;

class EmbeddingResponseTest {

	@Test
	void readsVectorsInOrderAndSkipsOllamaMetadata() throws Exception {
		String json = " {\"model\":\"xyz\",\"extra\":{\"embeddings\":\"acme\",\"data\":[true,false,null,12]},"
				+ "\"embeddings\":[[1e0,-2.5e-1],[0,1]],\"total_duration\":123} \n";
		var vectors = EmbeddingResponse.parse(json, 2, 2);
		assertThat(vectors).hasSize(2);
		assertThat(vectors.get(0)).containsExactly(1, -0.25f);
		assertThat(vectors.get(1)).containsExactly(0, 1);
	}

	@Test
	void acceptsUnicodeEscapedKeysAndStringEscapes() throws Exception {
		String json = "{\"extra\":\"\\\"\\\\\\/\\b\\f\\n\\r\\t\\u0061\",\"\\u0065mbeddings\":[[1,0]]}";
		assertThat(EmbeddingResponse.parse(json, 1, 2)).singleElement()
				.satisfies(vector -> assertThat(vector).containsExactly(1, 0));
	}

	@Test
	void rejectsMissingOrDuplicateEmbeddingsAndMalformedJson() {
		for (String json : List.of("", "[]", "{}", "{\"extra\":{\"embeddings\":[[1,0]]}}",
				"{\"embeddings\":[[1,0]],\"embeddings\":[[1,0]]}", "{\"embeddings\":[[1,0]]}xyz",
				"{\"embeddings\":[[1,0]],}", "{\"embeddings\":null}", "{\"embeddings\":[[1,0,]]}",
				"{\"extra\":\"\\u00xz\",\"embeddings\":[[1,0]]}",
				"{\"extra\":\"\\x\",\"embeddings\":[[1,0]]}")) {
			assertThatThrownBy(() -> EmbeddingResponse.parse(json, 1, 2)).isInstanceOf(EmbeddingException.class)
					.hasMessage("임베딩 응답 JSON 형식이 올바르지 않습니다").hasNoCause();
		}
	}

	@Test
	void rejectsWrongCountsAndDimensions() {
		assertThatThrownBy(() -> EmbeddingResponse.parse("{\"embeddings\":[]}", 1, 2))
				.isInstanceOf(EmbeddingException.class).hasMessageContaining("개수");
		for (String vector : List.of("[]", "[1]", "[1,0,0]")) {
			assertThatThrownBy(() -> EmbeddingResponse.parse("{\"embeddings\":[" + vector + "]}", 1, 2))
					.isInstanceOf(EmbeddingException.class).hasMessageContaining("차원");
		}
	}

	@Test
	void rejectsNonFiniteValuesInvalidNumbersAndZeroVectors() {
		for (String number : List.of("NaN", "Infinity", "1e100", "01", ".1", "+1", "1.", "1e")) {
			assertThatThrownBy(() -> EmbeddingResponse.parse("{\"embeddings\":[[" + number + ",0]]}", 1, 2))
					.isInstanceOf(EmbeddingException.class).hasNoCause();
		}
		assertThatThrownBy(() -> EmbeddingResponse.parse("{\"embeddings\":[[0,0]]}", 1, 2))
				.isInstanceOf(EmbeddingException.class).hasMessageContaining("길이가 0");
	}

	@Test
	void neverIncludesSensitiveInputOrResponseInException() {
		String text = "acme-private-p01";
		String response = "{\"extra\":\"" + text + "\",\"embeddings\":[[1e100,0]]}";
		assertThatThrownBy(() -> EmbeddingResponse.parse(response, 1, 2)).isInstanceOf(EmbeddingException.class)
				.hasMessageNotContaining(text).hasMessageNotContaining(response).hasNoCause();
	}
}
