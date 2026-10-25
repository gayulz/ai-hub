package dev.aihub.search.chunk;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.NavigableMap;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.springframework.stereotype.Component;

import dev.aihub.search.SearchSettings;
import dev.aihub.search.vault.Note;

@Component
public class Chunker {

	private static final Pattern HEADING = Pattern.compile("^(#{2,6}) (.*)$");
	private static final Pattern FENCE = Pattern.compile("^ {0,3}(`{3,}|~{3,})(.*)$");
	private final int softLimit;
	private final int hardLimit;

	public Chunker(SearchSettings settings) {
		softLimit = settings.getChunkSoftLimit();
		hardLimit = settings.getChunkHardLimit();
		if (softLimit <= 0 || hardLimit < softLimit) {
			throw new IllegalArgumentException("조각 상한은 0 < 소프트 상한 <= 하드 상한이어야 합니다");
		}
	}

	public List<Chunk> chunk(Note note) {
		List<Chunk> chunks = new ArrayList<>();
		List<Paragraph> section = new ArrayList<>();
		StringBuilder paragraph = new StringBuilder();
		var paths = new TreeMap<Integer, String>();
		String[] headings = new String[5];
		char fence = 0;
		int fenceLength = 0;
		for (String line : note.body().lines().toList()) {
			var fenceMatch = FENCE.matcher(line);
			var heading = HEADING.matcher(line);
			if (fence != 0) {
				paths.put(paragraph.length(), headingPath(headings));
				paragraph.append(line).append('\n');
				if (fenceMatch.matches() && fenceMatch.group(1).charAt(0) == fence
						&& fenceMatch.group(1).length() >= fenceLength && fenceMatch.group(2).isBlank()) {
					fence = 0;
				}
				continue;
			}
			if (fenceMatch.matches()) {
				fence = fenceMatch.group(1).charAt(0);
				fenceLength = fenceMatch.group(1).length();
			} else if (heading.matches()) {
				if (heading.group(1).length() == 2) {
					flushParagraph(paragraph, section, paths);
					emit(note, section, chunks);
					section.clear();
				}
				updateHeading(heading, headings);
			} else if (line.isBlank()) {
				flushParagraph(paragraph, section, paths);
				continue;
			}
			paths.put(paragraph.length(), headingPath(headings));
			paragraph.append(line).append('\n');
		}
		flushParagraph(paragraph, section, paths);
		emit(note, section, chunks);
		return List.copyOf(chunks);
	}

	private void updateHeading(Matcher match, String[] headings) {
		int depth = match.group(1).length() - 2;
		if (depth > 0 && headings[0] == null) {
			return;
		}
		headings[depth] = match.group(2).strip();
		Arrays.fill(headings, depth + 1, headings.length, null);
	}

	private String headingPath(String[] headings) {
		return String.join(" > ", Arrays.stream(headings).filter(value -> value != null && !value.isBlank())
				.toList());
	}

	private void flushParagraph(StringBuilder paragraph, List<Paragraph> section, NavigableMap<Integer, String> paths) {
		String raw = paragraph.toString();
		String text = raw.strip();
		if (!text.isEmpty()) {
			int removed = raw.length() - raw.stripLeading().length();
			var adjusted = new TreeMap<Integer, String>();
			paths.forEach((offset, heading) -> adjusted.put(Math.max(0, offset - removed), heading));
			section.add(new Paragraph(text, adjusted));
		}
		paragraph.setLength(0);
		paths.clear();
	}

	private void emit(Note note, List<Paragraph> section, List<Chunk> chunks) {
		StringBuilder body = new StringBuilder();
		int length = 0;
		String heading = "";
		for (Paragraph paragraph : section) {
			String text = paragraph.body();
			int offset = 0;
			int remaining = text.codePointCount(0, text.length());
			while (offset < text.length()) {
				int size = Math.min(hardLimit, remaining);
				int end = text.offsetByCodePoints(offset, size);
				if (!body.isEmpty() && length + 2 + size > softLimit) {
					addChunk(note, chunks, body.toString(), heading);
					body.setLength(0);
					length = 0;
				}
				if (!body.isEmpty()) {
					body.append("\n\n");
					length += 2;
				}
				body.append(text, offset, end);
				length += size;
				heading = paragraph.paths().floorEntry(end - 1).getValue();
				offset = end;
				remaining -= size;
			}
		}
		addChunk(note, chunks, body.toString(), heading);
	}

	private void addChunk(Note note, List<Chunk> chunks, String body, String heading) {
		if (!body.isBlank()) {
			chunks.add(new Chunk(note.path(), note.title(), heading, note.type(), note.sensitivity(),
					note.created(), note.tags(), chunks.size(), body, note.hash()));
		}
	}

	private record Paragraph(String body, NavigableMap<Integer, String> paths) {
	}
}
