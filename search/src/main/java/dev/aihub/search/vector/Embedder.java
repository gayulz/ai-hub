package dev.aihub.search.vector;

import java.util.List;

public interface Embedder {

	List<float[]> embed(List<String> texts) throws EmbeddingException;
}
