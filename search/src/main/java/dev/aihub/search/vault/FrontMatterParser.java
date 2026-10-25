package dev.aihub.search.vault;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

public class FrontMatterParser {

	private static final Pattern DELIMITER = Pattern.compile("(?m)^---\\r?$");
	private static final Set<String> KEYS = Set.of("type", "sensitivity", "created", "tags");
	private static final String UNREADABLE_HEADER_SENSITIVITY = "company";

	public Parsed parse(String text) {
		var delimiter = DELIMITER.matcher(text);
		if (!delimiter.find() || delimiter.start() != 0) {
			return empty(text);
		}
		int start = delimiter.end();
		if (!delimiter.find()) {
			return empty(text);
		}
		int end = delimiter.end();
		if (end < text.length() && text.charAt(end) == '\r') {
			end++;
		}
		if (end < text.length() && text.charAt(end) == '\n') {
			end++;
		}
		String body = text.substring(end);
		try {
			return parseHeader(text.substring(start, delimiter.start()), body);
		} catch (IllegalArgumentException exception) {
			// 머리말이 깨졌으면 회사 노트인지 알 수 없으므로 회사로 간주해 기본 검색에서 뺀다
			return new Parsed(null, UNREADABLE_HEADER_SENSITIVITY, null, List.of(), text);
		}
	}

	private Parsed parseHeader(String header, String body) {
		Map<String, String> values = new HashMap<>();
		Set<String> seen = new HashSet<>();
		List<String> tags = new ArrayList<>();
		boolean tagList = false;
		for (String line : header.lines().toList()) {
			String item = line.strip();
			if (item.isEmpty() || item.startsWith("#")) {
				continue;
			}
			if (tagList && (item.equals("-") || item.startsWith("- "))) {
				tags.add(tag(item.substring(1)));
				continue;
			}
			tagList = false;
			int colon = item.indexOf(':');
			if (colon < 1 || Character.isWhitespace(line.charAt(0))) {
				continue;
			}
			String key = item.substring(0, colon);
			String raw = item.substring(colon + 1).strip();
			if (!KEYS.contains(key)) {
				continue;
			}
			if (!seen.add(key)) {
				throw new IllegalArgumentException();
			}
			if (key.equals("tags")) {
				tagList = raw.isEmpty();
				if (!tagList) {
					tags.addAll(inlineTags(raw));
				}
			} else {
				String value = scalar(raw);
				values.put(key, key.equals("sensitivity") && value != null ? value.toLowerCase(Locale.ROOT) : value);
			}
		}
		return new Parsed(values.get("type"), values.get("sensitivity"), values.get("created"), tags, body);
	}

	private List<String> inlineTags(String raw) {
		if (!raw.startsWith("[") || !raw.endsWith("]")) {
			throw new IllegalArgumentException();
		}
		String content = raw.substring(1, raw.length() - 1).strip();
		if (content.isEmpty()) {
			return List.of();
		}
		List<String> tags = new ArrayList<>();
		char quote = 0;
		int start = 0;
		for (int i = 0; i < content.length(); i++) {
			char c = content.charAt(i);
			if (c == quote) {
				quote = 0;
			} else if (quote == 0 && (c == '\'' || c == '"')) {
				quote = c;
			} else if (quote == 0 && c == ',') {
				tags.add(tag(content.substring(start, i)));
				start = i + 1;
			}
		}
		if (quote != 0) {
			throw new IllegalArgumentException();
		}
		tags.add(tag(content.substring(start)));
		return tags;
	}

	private String tag(String raw) {
		String value = scalar(raw);
		if (value == null) {
			throw new IllegalArgumentException();
		}
		return value;
	}

	private String scalar(String raw) {
		String value = raw.strip();
		if (value.isEmpty() || value.equals("null") || value.equals("~")) {
			return null;
		}
		char first = value.charAt(0);
		if (first == '\'' || first == '"') {
			if (value.length() < 2 || value.charAt(value.length() - 1) != first) {
				throw new IllegalArgumentException();
			}
			return value.substring(1, value.length() - 1);
		}
		if (value.matches(".*[\\[\\]{}'\"].*")) {
			throw new IllegalArgumentException();
		}
		return value;
	}

	private Parsed empty(String body) {
		return new Parsed(null, null, null, List.of(), body);
	}

	public record Parsed(String type, String sensitivity, String created, List<String> tags, String body) {
		public Parsed {
			tags = List.copyOf(tags);
		}
	}
}
