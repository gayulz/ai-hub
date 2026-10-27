"""검토를 통과한 실행의 트리를 프로젝트 기준 가지에 한 커밋으로 쌓는다."""

from __future__ import annotations

from . import checks, targets
from .record import Run


def commit_message(run: Run, selected: targets.Target | None) -> str:
	message = run.request.get("commit_message") or run.request["goal"]
	if selected and selected.claude_trailer and any(c.get("role") == "implement" and c.get("tool") == "claude"
			and c.get("ok") is True for c in run.calls):
		message = message.rstrip() + "\n\n" + selected.claude_trailer
	return message


def apply_to_branch(run_id: str, branch: str) -> str:
	"""본 작업 폴더를 바꾸지 않고 기준 가지의 이전 끝을 확인해 반영한다."""
	from .__main__ import finish_outputs

	run = Run.load(run_id)
	if run.kind != "code" or run.status != "승인 필요":
		raise RuntimeError(f"반영할 수 없는 상태입니다: {run.kind} · {run.status}")
	if run.request.get("base_branch") != branch:
		raise RuntimeError("요청서의 기준 가지와 반영할 가지가 다릅니다")
	ref = f"refs/heads/{branch}"
	current = checks.git(run.repo, "rev-parse", "--verify", ref + "^{commit}", check=False).strip()
	if current != run.base_commit:
		raise RuntimeError("기준 가지가 실행 뒤에 바뀌어 반영하지 않습니다")
	# 체크아웃된 가지를 옮기면 본 작업 폴더의 HEAD도 바뀐다.
	if checks.git(run.repo, "symbolic-ref", "-q", "HEAD", check=False).strip() == ref:
		raise RuntimeError("현재 작업 폴더에서 사용 중인 기준 가지에는 반영하지 않습니다")
	tree = checks.git(run.repo, "rev-parse", "--verify", f"refs/heads/{run.branch}^{{tree}}").strip()
	if tree == checks.git(run.repo, "rev-parse", run.base_commit + "^{tree}").strip():
		raise RuntimeError("합칠 변경이 없습니다")
	selected = next((item for item in targets.targets().values() if str(item.path) == run.repo), None)
	commit = checks.git(run.repo, "commit-tree", tree, "-p", run.base_commit, "-m", commit_message(run, selected)).strip()
	try:
		checks.git(run.repo, "update-ref", ref, commit, run.base_commit)
	except RuntimeError:
		if checks.git(run.repo, "rev-parse", "--verify", ref + "^{commit}", check=False).strip() != run.base_commit:
			raise RuntimeError("기준 가지가 실행 뒤에 바뀌어 반영하지 않습니다") from None
		raise
	checks.git(run.repo, "worktree", "remove", "--force", run.worktree, check=False)
	checks.git(run.repo, "branch", "-D", run.branch, check=False)
	run.finish("완료", f"가지 {branch}에 반영 {commit[:12]}(push 안 함)")
	finish_outputs(run)
	return commit
