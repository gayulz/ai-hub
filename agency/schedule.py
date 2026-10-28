"""부서별 정기 점검을 프로젝트 원장에 한 번씩 접수한다."""

from datetime import datetime

from . import project


def target(row, department):
	repo = row.get("schedule_repo")
	if repo is None:
		return department.repos[0] if department.repos else ""
	if not isinstance(repo, str) or repo not in department.repos:
		raise ValueError("예약 저장소가 부서의 허용 목록에 없습니다")
	return repo


def receive(departments, rows, now):
	created = []
	for department in departments.values():
		for item in department.schedule:
			matches = [row for row in rows if row["department"] == department.id
				and row["title"] == item["title"] and target(row, department) == item["repo"]]
			if any(row["status"] not in ("완료", "취소") for row in matches):
				continue
			if matches:
				try:
					finished = [datetime.fromisoformat(row.get("finished_at", row["updated"])) for row in matches]
					if any(stamp.tzinfo is None or stamp.utcoffset() is None for stamp in finished):
						raise ValueError
					elapsed = (now - max(finished)).total_seconds()
				except (TypeError, ValueError, OverflowError):
					raise ValueError("정기 점검 완료 시각을 확인할 수 없습니다") from None
				if elapsed < item["every_days"] * 86400:
					continue
			row = project.new(department, item["title"], item["request"], schedule_repo=item["repo"])
			rows.append(row)
			created.append(row)
			break
	return created
