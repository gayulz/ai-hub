package dev.aihub.search.vector;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

// 응답에서 embeddings만 수치 배열로 읽고, Ollama의 부가 필드는 저장하지 않는다.
final class EmbeddingResponse {

	private static final Pattern NUMBER = Pattern.compile("-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?");
	private final String json;
	private int offset;

	private EmbeddingResponse(String json) {
		this.json = json;
	}

	static List<float[]> parse(String json, int count, int dimension) throws EmbeddingException {
		var parser = new EmbeddingResponse(json);
		parser.expect('{');
		List<float[]> vectors = null;
		if (!parser.take('}')) {
			do {
				String key = parser.string();
				parser.expect(':');
				if (key.equals("embeddings")) {
					if (vectors != null) {
						throw invalid();
					}
					vectors = parser.vectors(dimension);
				} else {
					parser.skipValue(0);
				}
			} while (parser.take(','));
			parser.expect('}');
		}
		parser.space();
		if (parser.offset != json.length() || vectors == null) {
			throw invalid();
		}
		if (vectors.size() != count) {
			throw new EmbeddingException("임베딩 응답 개수가 입력 개수와 다릅니다");
		}
		return List.copyOf(vectors);
	}

	private List<float[]> vectors(int dimension) throws EmbeddingException {
		expect('[');
		List<float[]> vectors = new ArrayList<>();
		if (!take(']')) {
			do {
				expect('[');
				float[] vector = new float[dimension];
				int size = 0;
				double norm = 0;
				if (!take(']')) {
					do {
						float value;
						try {
							value = Float.parseFloat(number());
						} catch (NumberFormatException exception) {
							throw invalid();
						}
						if (!Float.isFinite(value)) {
							throw invalid();
						}
						if (size == dimension) {
							throw wrongDimension();
						}
						vector[size++] = value;
						norm += (double) value * value;
					} while (take(','));
					expect(']');
				}
				if (size != dimension) {
					throw wrongDimension();
				}
				if (norm == 0) {
					throw new EmbeddingException("임베딩 벡터의 길이가 0입니다");
				}
				vectors.add(vector);
			} while (take(','));
			expect(']');
		}
		return vectors;
	}

	private void skipValue(int depth) throws EmbeddingException {
		if (depth > 32) {
			throw invalid();
		}
		space();
		if (offset < json.length() && json.charAt(offset) == '"') {
			string();
		} else if (take('{')) {
			if (!take('}')) {
				do {
					string();
					expect(':');
					skipValue(depth + 1);
				} while (take(','));
				expect('}');
			}
		} else if (take('[')) {
			if (!take(']')) {
				do {
					skipValue(depth + 1);
				} while (take(','));
				expect(']');
			}
		} else if (json.startsWith("true", offset)) {
			offset += 4;
		} else if (json.startsWith("false", offset)) {
			offset += 5;
		} else if (json.startsWith("null", offset)) {
			offset += 4;
		} else {
			number();
		}
	}

	private String number() throws EmbeddingException {
		space();
		var match = NUMBER.matcher(json).region(offset, json.length());
		if (!match.lookingAt()) {
			throw invalid();
		}
		offset = match.end();
		return match.group();
	}

	private String string() throws EmbeddingException {
		expect('"');
		StringBuilder result = new StringBuilder();
		while (offset < json.length()) {
			char value = json.charAt(offset++);
			if (value == '"') {
				return result.toString();
			}
			if (value < 0x20) {
				throw invalid();
			}
			if (value == '\\') {
				if (offset == json.length()) {
					throw invalid();
				}
				value = switch (json.charAt(offset++)) {
					case '"' -> '"';
					case '\\' -> '\\';
					case '/' -> '/';
					case 'b' -> '\b';
					case 'f' -> '\f';
					case 'n' -> '\n';
					case 'r' -> '\r';
					case 't' -> '\t';
					case 'u' -> unicode();
					default -> throw invalid();
				};
			}
			result.append(value);
		}
		throw invalid();
	}

	private char unicode() throws EmbeddingException {
		int value = 0;
		for (int i = 0; i < 4; i++) {
			if (offset == json.length()) {
				throw invalid();
			}
			char digit = json.charAt(offset++);
			int hex = Character.digit(digit, 16);
			if (digit > 0x7f || hex < 0) {
				throw invalid();
			}
			value = value * 16 + hex;
		}
		return (char) value;
	}

	private boolean take(char expected) {
		space();
		if (offset < json.length() && json.charAt(offset) == expected) {
			offset++;
			return true;
		}
		return false;
	}

	private void expect(char expected) throws EmbeddingException {
		if (!take(expected)) {
			throw invalid();
		}
	}

	private void space() {
		while (offset < json.length() && " \t\r\n".indexOf(json.charAt(offset)) >= 0) {
			offset++;
		}
	}

	private static EmbeddingException invalid() {
		return new EmbeddingException("임베딩 응답 JSON 형식이 올바르지 않습니다");
	}

	private static EmbeddingException wrongDimension() {
		return new EmbeddingException("임베딩 벡터 차원이 설정과 다릅니다");
	}
}
