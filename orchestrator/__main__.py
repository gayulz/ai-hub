"""AI 개발 오케스트레이션(A안) 실행기.

사용:
	python3 -m orchestrator run <요청서.toml>          새 실행(구현 → 검증 → 독립 검토 → 보완)
	python3 -m orchestrator resume <run_id> [--calls N] [--minutes M]   끊긴 실행을 이어 간다(예산을 늘릴 수 있다)
	python3 -m orchestrator status <run_id>             상태 보기
	python3 -m orchestrator apply <run_id>              승인 필요 상태의 변경을 본 저장소에 한 커밋으로 반영한다(push 안 함)

요청서는 [request] goal·done·owned_paths·forbidden·details·test_command·commit_message·kind, [budget] calls·minutes·fixes.
역할 담당은 ~/.ai-hub/plans.toml 의 [orchestration] implementer·reviewer 목록을 따른다.
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector import vault
from collector.config import home, load_plans, load_sources

from . import checks
from .flow import Flow
from .record import DEFAULT_BUDGET, Run, new_run_id

USAGE = __doc__


def orchestration_config() -> dict:
	with open(home() / "plans.toml", "rb") as fh:
		section = tomllib.load(fh).get("orchestration")
	if not section or not section.get("implementer") or not section.get("reviewer"):
		raise ValueError("plans.toml 에 [orchestration] implementer·reviewer 가 없습니다")
	return section


@contextmanager
def locked(run: Run):
	run.folder.mkdir(parents=True, exist_ok=True)
	with open(run.folder / ".lock", "w") as fh:
		try:
			fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			raise RuntimeError(f"{run.run_id} 는 이미 실행 중입니다") from None
		yield


def _usage(value) -> str:
	"""공급자마다 다른 사용량 칸을 입력·캐시 읽기·출력 토큰으로 줄인다. 모르면 원래 값(미측정 등)."""
	if not isinstance(value, dict):
		return str(value)
	cached = value.get("cached_input_tokens") or value.get("cache_read_input_tokens") or value.get("cache_read_tokens") or 0
	return f"입력 {value.get('input_tokens', '?')} · 캐시 {cached} · 출력 {value.get('output_tokens', '?')}"


def report(run: Run) -> str:
	lines = [f"# 오케스트레이션 실행 {run.run_id}", "", f"- 목표: {run.request['goal']}",
		f"- 상태: {run.status} — {run.end_reason}", f"- 종류: {run.kind} · 보완 {run.fixes}회 · 시간 {round(run.elapsed)}초",
		"", "## 호출", "", "| 역할 | 요금제 | 결과 | 초 | 사용량 |", "|---|---|---|---|---|"]
	for c in run.calls:
		result = "성공" if c.get("ok") else f"실패({c.get('error', '')})"
		lines.append(f"| {c['role']} | {c['plan']} | {result} | {c.get('seconds', '-')} | {_usage(c.get('usage', '미측정'))} |")
	lines += ["", "## 기계 검증", ""] + [f"- {'통과' if c['ok'] else '실패'} {c['name']} {c.get('detail', '')[:200]}" for c in run.checks]
	lines += ["", "## 검토", "", run.review_summary or "(없음)"]
	if run.open_findings:
		lines += ["", "## 열린 지적", ""] + [f"- [{f['severity']}] {f['file']}:{f['line']} {f['problem']}" for f in run.open_findings]
	if run.handoff.get("minor_findings"):
		lines += ["", "## 참고 지적(minor)", ""] + [f"- {f['file']}:{f['line']} {f['problem']}" for f in run.handoff["minor_findings"]]
	if run.kind == "code" and run.worktree and Path(run.worktree).exists():
		lines += ["", "## 변경", "", "```", checks.git(run.worktree, "diff", "--stat", run.base_commit, "HEAD").strip(), "```"]
	if run.kind == "analysis":
		lines += ["", f"문서: `{run.folder / 'document.md'}`"]
	if run.status == "승인 필요" and run.kind == "code":
		lines += ["", f"반영: `python3 -m orchestrator apply {run.run_id}`"]
	return "\n".join(lines) + "\n"


def finish_outputs(run: Run) -> None:
	"""보고서를 run 폴더에 쓰고, 볼트 일지(정해진 착지점)에 한 덩어리로 남긴다."""
	text = report(run)
	(run.folder / "report.md").write_text(text, encoding="utf-8")
	answered = sorted({c["tool"] for c in run.calls if c.get("ok")})
	body = (f"- 목표: {run.request['goal']}\n- 상태: {run.status} — {run.end_reason}\n"
		f"- 실제 응답한 AI: {', '.join(answered) or '없음'} · 호출 {len(run.calls)}회\n- 보고서: `{run.folder / 'report.md'}`\n")
	today = datetime.now().date()
	vault.upsert_block(vault.daily_path(load_sources().vault, "personal", today), f"run:{run.run_id}", body,
		"AI 오케스트레이션", today, "personal")
	print(text)


def cmd_run(request_path: str) -> int:
	with open(request_path, "rb") as fh:
		data = tomllib.load(fh)
	request = data["request"]
	if not request.get("goal") or not request.get("done"):
		raise ValueError("요청서에 goal 과 done 이 필요합니다")
	kind = request.get("kind", "code")
	if kind == "code" and not request.get("owned_paths"):
		raise ValueError("코드 작업은 owned_paths(고쳐도 되는 경로)가 필요합니다")
	budget = dict(DEFAULT_BUDGET)
	extra = data.get("budget", {})
	budget.update({k: extra[k] for k in ("calls", "fixes") if k in extra})
	if "minutes" in extra:
		budget["seconds"] = int(extra["minutes"] * 60)
	run = Run(run_id=new_run_id(), request=request, repo=str(Path(request.get("repo", ".")).resolve()), kind=kind, budget=budget)
	shutil.copy(request_path, run.folder / "request.toml")
	run.save()
	run.event("start", goal=request["goal"], kind=kind)
	return execute(run)


def execute(run: Run) -> int:
	with locked(run):
		Flow(run, load_plans(), orchestration_config()).run_all()
	finish_outputs(run)
	return 0 if run.status in ("승인 필요", "완료") else 1


def cmd_resume(run_id: str, options: list[str]) -> int:
	run = Run.load(run_id)
	if run.status in ("승인 필요", "완료", "취소"):
		print(f"{run_id} 는 이미 끝났습니다: {run.status}")
		return 0
	for flag, value in zip(options[::2], options[1::2]):
		if flag == "--calls":
			run.budget["calls"] += int(value)
		elif flag == "--minutes":
			run.budget["seconds"] += int(value) * 60
	run.status, run.end_reason = "작업 중", ""
	run.event("resume", budget=run.budget)
	run.save()
	return execute(run)


def cmd_apply(run_id: str) -> int:
	"""검증을 통과한 변경을 본 저장소에 한 커밋으로 합친다. 사용자 승인 뒤에만 부른다(O3 A)."""
	run = Run.load(run_id)
	if run.kind != "code" or run.status != "승인 필요":
		raise RuntimeError(f"반영할 수 없는 상태입니다: {run.kind} · {run.status}")
	if checks.git(run.repo, "diff", "--cached", "--name-only").strip():
		raise RuntimeError("본 저장소에 스테이징된 변경이 있어 반영하지 않습니다")
	merged = subprocess.run(["git", "-C", run.repo, "merge", "--squash", run.branch], capture_output=True, text=True)
	if merged.returncode != 0 or not checks.git(run.repo, "diff", "--cached", "--name-only").strip():
		checks.git(run.repo, "reset", "-q", "--merge", check=False)  # 반쯤 합친 상태를 남기지 않는다
		raise RuntimeError(f"합칠 변경이 없거나 충돌했습니다: {(merged.stdout + merged.stderr).strip()[-200:]}")
	message = run.request.get("commit_message") or run.request["goal"]
	checks.git(run.repo, "commit", "-q", "-m", message)
	commit = checks.git(run.repo, "rev-parse", "--short", "HEAD").strip()
	checks.git(run.repo, "worktree", "remove", "--force", run.worktree, check=False)
	checks.git(run.repo, "branch", "-D", run.branch, check=False)
	run.finish("완료", f"본 저장소에 반영 {commit}(push 안 함)")
	finish_outputs(run)
	return 0


def main(argv: list[str]) -> int:
	try:
		if argv[:1] == ["run"] and len(argv) == 2:
			return cmd_run(argv[1])
		if argv[:1] == ["resume"] and len(argv) >= 2:
			return cmd_resume(argv[1], argv[2:])
		if argv[:1] == ["status"] and len(argv) == 2:
			print(report(Run.load(argv[1])))
			return 0
		if argv[:1] == ["apply"] and len(argv) == 2:
			return cmd_apply(argv[1])
	except (OSError, ValueError, RuntimeError, KeyError) as exc:
		print(f"orchestrator 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
		return 1
	print(USAGE, file=sys.stderr)
	return 2


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
