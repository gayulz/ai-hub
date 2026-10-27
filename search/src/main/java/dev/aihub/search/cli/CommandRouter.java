package dev.aihub.search.cli;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Locale;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.eval.EvalLog;
import dev.aihub.search.eval.EvalRunnerImpl;
import dev.aihub.search.eval.EvalSetReader;
import dev.aihub.search.http.LocalSearchServer;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchMode;
import dev.aihub.search.query.SearchService;

@Component
public class CommandRouter {

	private static final String USAGE = "사용법: index [--search.vault=<경로>] [--no-vector]"
			+ " | query \"질의\" [--k 5] [--include-company] [--mode bm25|vector|hybrid]"
			+ " | eval [--set 경로] [--mode bm25|vector|hybrid] [--k 5] [--log 경로]"
			+ " | serve [--bind 127.0.0.1] [--port 8791]";
	private final NoteIndexer indexer;
	private final SearchService searchService;
	private final SearchSettings settings;
	private final LocalSearchServer server;

	public CommandRouter(NoteIndexer indexer, SearchService searchService, SearchSettings settings) {
		this(indexer, searchService, settings, new LocalSearchServer(searchService));
	}

	@Autowired
	public CommandRouter(NoteIndexer indexer, SearchService searchService, SearchSettings settings,
			LocalSearchServer server) {
		this.indexer = indexer;
		this.searchService = searchService;
		this.settings = settings;
		this.server = server;
	}

	public int run(String[] args, PrintWriter out, PrintWriter err) {
		try {
			if (args.length == 0) {
				throw new IllegalArgumentException("명령을 주세요");
			}
			return switch (args[0]) {
				case "index" -> index(args, out);
				case "query" -> query(args, out);
				case "eval" -> eval(args, out);
				case "serve" -> serve(args, out);
				default -> throw new IllegalArgumentException("알 수 없는 명령입니다");
			};
		} catch (IllegalArgumentException exception) {
			err.println(exception instanceof InvalidPathException ? "파일 경로 형식을 확인하세요" : exception.getMessage());
			err.println(USAGE);
			return 2;
		} catch (IOException exception) {
			err.println("작업을 완료하지 못했습니다: " + exception.getMessage());
			return 1;
		} finally {
			out.flush();
			err.flush();
		}
	}

	private int index(String[] args, PrintWriter out) throws IOException {
		Path vault = settings.getVault();
		boolean hasVault = false;
		boolean noVector = false;
		for (int i = 1; i < args.length; i++) {
			if (args[i].startsWith("--search.vault=") && !hasVault) {
				String value = args[i].substring("--search.vault=".length());
				vault = value.isBlank() ? null : Path.of(value);
				hasVault = true;
			} else if (args[i].equals("--no-vector") && !noVector) {
				noVector = true;
			} else {
				throw new IllegalArgumentException("index 인자를 확인하세요");
			}
		}
		if (vault == null || vault.toString().isBlank()) {
			throw new IllegalArgumentException("볼트 경로를 --search.vault=... 로 주세요");
		}
		out.println(indexer.index(vault, !noVector));
		return 0;
	}

	private int query(String[] args, PrintWriter out) throws IOException {
		if (args.length < 2 || args[1].isBlank() || args[1].startsWith("--")) {
			throw new IllegalArgumentException("검색 질의를 주세요");
		}
		int k = 5;
		boolean hasK = false;
		boolean includeCompany = false;
		SearchMode mode = SearchMode.BM25;
		boolean hasMode = false;
		for (int i = 2; i < args.length; i++) {
			if (args[i].equals("--k") && !hasK && i + 1 < args.length) {
				k = parseK(args[++i]);
				hasK = true;
			} else if (args[i].equals("--include-company") && !includeCompany) {
				includeCompany = true;
			} else if (args[i].equals("--mode") && !hasMode && i + 1 < args.length) {
				mode = SearchMode.parse(args[++i]);
				hasMode = true;
			} else {
				throw new IllegalArgumentException("query 인자를 확인하세요");
			}
		}
		int rank = 0;
		var hits = mode.search(searchService, args[1], k, includeCompany);
		for (var hit : hits) {
			out.printf(Locale.ROOT, "%d · %.3f · %s · %s · %s%n", ++rank, hit.score(), hit.path(),
					hit.heading(), hit.snippet());
		}
		return 0;
	}

	private int parseK(String raw) {
		try {
			int k = Integer.parseInt(raw);
			if (k > 0) {
				return k;
			}
		} catch (NumberFormatException exception) {
			throw new IllegalArgumentException("검색 건수는 1 이상의 정수여야 합니다");
		}
		throw new IllegalArgumentException("검색 건수는 1 이상의 정수여야 합니다");
	}

	private int eval(String[] args, PrintWriter out) throws IOException {
		Path source = Path.of(System.getProperty("user.home"), ".ai-hub", "search-eval.toml");
		Path log = Path.of(System.getProperty("user.home"), ".ai-hub", "search-eval-log.md");
		SearchMode mode = SearchMode.BM25;
		int k = 5;
		var seen = new HashSet<String>();
		for (int i = 1; i < args.length; i++) {
			String option = args[i];
			if (!seen.add(option) || i + 1 >= args.length) {
				throw new IllegalArgumentException("eval 인자를 확인하세요");
			}
			String value = args[++i];
			if (value.isBlank() || value.startsWith("--")) {
				throw new IllegalArgumentException("eval 인자를 확인하세요");
			}
			switch (option) {
				case "--set" -> source = Path.of(value);
				case "--log" -> log = Path.of(value);
				case "--mode" -> mode = SearchMode.parse(value);
				case "--k" -> k = parseK(value);
				default -> throw new IllegalArgumentException("eval 인자를 확인하세요");
			}
		}
		try {
			if (source.toAbsolutePath().normalize().equals(log.toAbsolutePath().normalize())
					|| (Files.exists(source) && Files.exists(log) && Files.isSameFile(source, log))) {
				throw new IllegalArgumentException("평가 원본과 로그는 다른 파일이어야 합니다");
			}
		} catch (IOException exception) {
			throw new IOException("평가 원본·로그 파일을 확인하지 못했습니다");
		}
		var queries = new EvalSetReader().read(source);
		SearchMode selected = mode;
		try {
			var report = new EvalRunnerImpl().run(queries, (text, limit, company) -> {
				try {
					return selected.search(searchService, text, limit, company).stream().map(hit -> hit.path()).toList();
				} catch (IOException exception) {
					throw new UncheckedIOException(exception);
				}
			}, k);
			new EvalLog().append(log, mode.toString(), k, settings,
					searchService.analyzerFingerprint(), report);
			out.printf(Locale.ROOT, "모드 %s · Hit@%d (recallAt5) %.6f · MRR@%d %.6f%n",
					mode, k, report.recallAt5(), k, report.mrr());
			for (var row : report.rows()) {
				out.printf(Locale.ROOT, "%s · 적중 %.0f · 역순위 %.6f%n", row.id(), row.recallAt5(), row.reciprocalRank());
			}
			return 0;
		} catch (RuntimeException | IOException exception) {
			throw new IOException("평가 검색 또는 점수 로그 기록에 실패했습니다. 색인·설정을 확인하세요");
		}
	}

	private int serve(String[] args, PrintWriter out) throws IOException {
		String bind = "127.0.0.1";
		int port = 8791;
		var seen = new HashSet<String>();
		for (int i = 1; i < args.length; i++) {
			String option = args[i];
			if (!seen.add(option) || i + 1 >= args.length) {
				throw new IllegalArgumentException("serve 인자를 확인하세요");
			}
			String value = args[++i];
			switch (option) {
				case "--bind" -> bind = value;
				case "--port" -> {
					try {
						port = Integer.parseInt(value);
					} catch (NumberFormatException exception) {
						throw new IllegalArgumentException("서버 포트는 0~65535 정수여야 합니다");
					}
				}
				default -> throw new IllegalArgumentException("serve 인자를 확인하세요");
			}
		}
		server.start(bind, port);
		out.println("로컬 검색 서버 실행 중 · 포트 " + server.port());
		return 0;
	}
}
