package dev.aihub.search.index;

public record IndexReport(int addedNotes, int updatedNotes, int deletedNotes, int unchangedNotes,
		int chunks, int skippedFiles) {

	@Override
	public String toString() {
		return "추가 %d · 수정 %d · 삭제 %d · 변경 없음 %d · 조각 %d · 건너뜀 %d"
				.formatted(addedNotes, updatedNotes, deletedNotes, unchangedNotes, chunks, skippedFiles);
	}
}
