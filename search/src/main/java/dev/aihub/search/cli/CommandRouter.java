package dev.aihub.search.cli;

import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Path;
import java.util.Locale;

import org.springframework.stereotype.Component;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.index.NoteIndexer;
import dev.aihub.search.query.SearchService;

@Component
public class CommandRouter {

	private static final String USAGE = "사용법: index [--search.vault=<경로>] | query \"질의\" [--k 5] [--include-company] | eval";
	private final NoteIndexer indexer;
	private final SearchService searchService;
	private final SearchSettings settings;

	public CommandRouter(NoteIndexer indexer, SearchService searchService, SearchSettings settings) {
		this.indexer = indexer;
		this.searchService = searchService;
		this.settings = settings;
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
				default -> throw new IllegalArgumentException("알 수 없는 명령입니다");
			};
		} catch (IllegalArgumentException exception) {
			err.println(exception.getMessage());
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
		if (args.length == 2 && args[1].startsWith("--search.vault=")) {
			String value = args[1].substring("--search.vault=".length());
			vault = value.isBlank() ? null : Path.of(value);
		} else if (args.length != 1) {
			throw new IllegalArgumentException("index 인자를 확인하세요");
		}
		if (vault == null || vault.toString().isBlank()) {
			throw new IllegalArgumentException("볼트 경로를 --search.vault=... 로 주세요");
		}
		out.println(indexer.index(vault));
		return 0;
	}

	private int query(String[] args, PrintWriter out) throws IOException {
		if (args.length < 2 || args[1].isBlank() || args[1].startsWith("--")) {
			throw new IllegalArgumentException("검색 질의를 주세요");
		}
		int k = 5;
		boolean hasK = false;
		boolean includeCompany = false;
		for (int i = 2; i < args.length; i++) {
			if (args[i].equals("--k") && !hasK && i + 1 < args.length) {
				k = parseK(args[++i]);
				hasK = true;
			} else if (args[i].equals("--include-company") && !includeCompany) {
				includeCompany = true;
			} else {
				throw new IllegalArgumentException("query 인자를 확인하세요");
			}
		}
		int rank = 0;
		for (var hit : searchService.searchNotes(args[1], k, includeCompany)) {
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

	private int eval(String[] args, PrintWriter out) {
		if (args.length != 1) {
			throw new IllegalArgumentException("eval 인자를 확인하세요");
		}
		out.println("평가 실행기는 사용자 과제입니다(6-2c)");
		return 2;
	}
}
