"""바이트 산출물을 새 이름으로만 등록하고 승인 묶음을 갱신한다."""

import hashlib

from . import approvals, files, schema, slot, states
from .errors import Conflict, StoreError, TransitionError

# 동일한 정렬·직렬화 규칙을 승인 확인에서도 쓴다.
manifest_digest = states.manifest


def register(store, intake_id, number, name, kind, content):
	schema.filename(name)
	schema.token(kind, states.ARTIFACT_KINDS)
	if type(content) is not bytes:
		raise StoreError("산출물 내용은 바이트여야 합니다")
	with store.locked(intake_id, number) as record:
		run = record.run
		if run["status"] in ("cancelled", "cancelling", "needs_check"):
			raise TransitionError("종료 또는 결과 대조가 필요한 회차에는 등록할 수 없습니다")
		if any(row["name"].casefold() == name.casefold() for row in run["artifacts"]):
			raise Conflict("이미 등록한 산출물 이름입니다. 새 이름을 사용해 주세요")
		row = {"name": name, "kind": kind, "sha256": hashlib.sha256(content).hexdigest(),
			"size": len(content), "registered_at": schema.now()}
		run["artifacts"].append(row)
		approvals.invalidate(run, kind)
		if run["status"] == "awaiting_user":
			slot.release_settled(store, run)
		# 파일을 쓰기 전에 형식·승인 롤백까지 검사한다.
		schema.validate(run)
		try:
			with files.child_directory(record.folder, "artifacts") as folder:
				files.write_new(folder, name, content)
		except FileExistsError:
			raise Conflict("저장된 파일과 충돌합니다. 새 이름을 사용해 주세요") from None
		except OSError:
			raise StoreError("산출물을 안전하게 저장할 수 없습니다") from None
		return record.save()
