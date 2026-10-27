package dev.aihub.search.eval;

import java.io.DataInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import dev.aihub.search.vault.VaultReader;

public class EvalSetReader {

	private final Duration timeout;

	public EvalSetReader() {
		this(Duration.ofSeconds(10));
	}

	EvalSetReader(Duration timeout) {
		this.timeout = timeout;
	}

	public List<EvalQuery> read(Path source) throws IOException {
		Path directory = null;
		Process process = null;
		try {
			rejectProtected(source.toAbsolutePath());
			Path resolvedSource = source.toRealPath();
			rejectProtected(resolvedSource);
			directory = Files.createTempDirectory("aihub-eval-");
			Path script = directory.resolve("eval_set.py");
			Path result = directory.resolve("result.bin");
			try (var input = getClass().getResourceAsStream("/eval_set.py")) {
				if (input == null) {
					throw new IOException();
				}
				Files.copy(input, script);
			}
			process = new ProcessBuilder("python3", "-I", script.toString(), resolvedSource.toString(),
					result.toString()).redirectOutput(ProcessBuilder.Redirect.DISCARD)
					.redirectError(ProcessBuilder.Redirect.DISCARD).start();
			process.getOutputStream().close();
			if (!process.waitFor(timeout.toNanos(), TimeUnit.NANOSECONDS)) {
				throw new IOException("평가 TOML 읽기 제한 시간을 넘었습니다");
			}
			if (process.exitValue() == 2) {
				throw new IllegalArgumentException("평가 TOML 형식·타입·ID·정답 경로를 확인하세요");
			}
			if (process.exitValue() == 3) {
				throw new IOException("평가에는 Python 3.11 이상과 tomllib가 필요합니다");
			}
			if (process.exitValue() != 0) {
				throw new IOException("평가 파일을 읽지 못했습니다. Python 3.11 이상과 파일을 확인하세요");
			}
			return decode(result);
		} catch (InterruptedException exception) {
			Thread.currentThread().interrupt();
			throw new IOException("평가 TOML 읽기가 중단되었습니다");
		} catch (IOException exception) {
			// 경로나 파서 출력은 호출자에게 전파하지 않는다.
			throw new IOException("평가 TOML을 읽지 못했습니다. Python 3.11 이상·파일·제한 시간을 확인하세요");
		} finally {
			try {
				if (process != null) {
					stop(process);
				}
			} finally {
				if (directory != null) {
					try {
						Files.deleteIfExists(directory.resolve("result.bin"));
						Files.deleteIfExists(directory.resolve("eval_set.py"));
						Files.deleteIfExists(directory);
					} catch (IOException exception) {
						throw new IOException("평가 임시 파일을 정리하지 못했습니다");
					}
				}
			}
		}
	}

	private void stop(Process process) throws IOException {
		boolean interrupted = Thread.interrupted();
		try {
			process.descendants().forEach(ProcessHandle::destroyForcibly);
			process.destroyForcibly();
			if (!process.waitFor(2, TimeUnit.SECONDS)) {
				throw new IOException("평가 보조 프로세스를 종료하지 못했습니다");
			}
		} catch (InterruptedException exception) {
			interrupted = true;
			throw new IOException("평가 보조 프로세스 종료가 중단되었습니다");
		} finally {
			if (interrupted) {
				Thread.currentThread().interrupt();
			}
		}
	}

	private void rejectProtected(Path path) throws IOException {
		for (Path part : path) {
			if (part.toString().startsWith(VaultReader.SECRET_PREFIX)) {
				throw new IOException("보호 경로는 읽을 수 없습니다");
			}
		}
	}

	private List<EvalQuery> decode(Path result) throws IOException {
		try (var input = new DataInputStream(Files.newInputStream(result))) {
			int count = bounded(input.readInt(), 0, 10000);
			var queries = new ArrayList<EvalQuery>();
			for (int i = 0; i < count; i++) {
				String id = string(input);
				String text = string(input);
				boolean company = input.readBoolean();
				int answers = bounded(input.readInt(), 1, 1000);
				var paths = new ArrayList<String>();
				for (int j = 0; j < answers; j++) {
					paths.add(string(input));
				}
				queries.add(new EvalQuery(id, text, company, paths));
			}
			if (input.read() != -1) {
				throw new IOException();
			}
			return List.copyOf(queries);
		}
	}

	private String string(DataInputStream input) throws IOException {
		int length = bounded(input.readInt(), 1, 65536);
		byte[] bytes = input.readNBytes(length);
		if (bytes.length != length) {
			throw new IOException();
		}
		return new String(bytes, StandardCharsets.UTF_8);
	}

	private int bounded(int number, int minimum, int maximum) throws IOException {
		if (number < minimum || number > maximum) {
			throw new IOException();
		}
		return number;
	}
}
