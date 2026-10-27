package dev.aihub.search.vector;

import java.util.List;
import java.util.Random;

public final class FakeEmbedder implements Embedder {

	private final int dimension;

	public FakeEmbedder(int dimension) {
		this.dimension = dimension;
	}

	@Override
	public List<float[]> embed(List<String> texts) {
		return texts.stream().map(this::vector).toList();
	}

	private float[] vector(String text) {
		var random = new Random(text.hashCode());
		float[] vector = new float[dimension];
		double norm = 0;
		for (int i = 0; i < dimension; i++) {
			vector[i] = random.nextFloat() * 2 - 1;
			norm += (double) vector[i] * vector[i];
		}
		for (int i = 0; i < dimension; i++) {
			vector[i] /= (float) Math.sqrt(norm);
		}
		return vector;
	}
}
