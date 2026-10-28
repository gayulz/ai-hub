"""AI 개발 오케스트레이션(A안) 실행기.

사용:
	python3 -m orchestrator run <요청서.toml>          새 실행(구현 → 검증 → 독립 검토 → 보완)
	python3 -m orchestrator draft --role <역할> --target <대상> --text-file <글> --out <초안.toml> [--previous <이전.toml>]
	python3 -m orchestrator meeting --help               읽기 전용 회의(분석·확인·문답·재개)
	python3 -m orchestrator chat plans                  읽기 전용 PM 대화(요금제·새 대화·전송·문답·종료)
	python3 -m orchestrator resume <run_id> [--calls N] [--minutes M]   끊긴 실행을 이어 간다(예산을 늘릴 수 있다)
	python3 -m orchestrator status <run_id>             상태 보기
	python3 -m orchestrator apply <run_id>              승인 필요 상태의 변경을 본 저장소에 한 커밋으로 반영한다(push 안 함)
	python3 -m orchestrator apply <run_id> --onto <가지>  프로젝트 기준 가지에 한 커밋으로 반영한다(push 안 함)

요청서는 [request] goal·done·owned_paths·forbidden·details·test_command·commit_message·kind·role·base_branch, [budget] calls·minutes·fixes.
role은 plan·backend·frontend·design 중 하나이며 생략하면 backend다.
역할 담당은 ~/.ai-hub/plans.toml 의 [orchestration] implementer·reviewer 목록을 따른다.
"""

from __future__ import annotations

import fcntl
import math
import shlex
import shutil
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from collector import vault
from collector.config import ConfigError, home, load_plans, load_sources

from . import checks, targets
from .branch_apply import apply_to_branch, commit_message
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
	with open(run.folder / ".lock", "a") as fh:
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
		onto = " --onto " + shlex.quote(run.request["base_branch"]) if run.request.get("base_branch") else ""
		lines += ["", f"반영: `python3 -m orchestrator apply {run.run_id}{onto}`"]
	return "\n".join(lines) + "\n"


def finish_outputs(run: Run, *, print_report: bool = True) -> None:
	"""보고서와 볼트 일지를 저장하고, 요청한 경우 본문을 출력한다."""
	text = report(run)
	(run.folder / "report.md").write_text(text, encoding="utf-8")
	answered = sorted({c["tool"] for c in run.calls if c.get("ok")})
	body = (f"- 목표: {run.request['goal']}\n- 상태: {run.status} — {run.end_reason}\n"
		f"- 실제 응답한 AI: {', '.join(answered) or '없음'} · 호출 {len(run.calls)}회\n- 보고서: `{run.folder / 'report.md'}`\n")
	today = datetime.now().date()
	vault.upsert_block(vault.daily_path(load_sources().vault, "personal", today), f"run:{run.run_id}", body,
		"AI 오케스트레이션", today, "personal")
	if print_report:
		print(text)


def read_request(request_path: str) -> dict:
	with open(request_path, "rb") as fh:
		data = tomllib.load(fh)
	request = data["request"]
	if not isinstance(request, dict):
		raise ValueError("request는 요청서 표여야 합니다")
	if request.get("role", "backend") not in ("plan", "backend", "frontend", "design"):
		raise ValueError("role은 plan·backend·frontend·design 중 하나여야 합니다")
	if not request.get("goal") or not request.get("done"):
		raise ValueError("요청서에 goal 과 done 이 필요합니다")
	kind = request.get("kind", "code")
	if kind == "code" and not request.get("owned_paths"):
		raise ValueError("코드 작업은 owned_paths(고쳐도 되는 경로)가 필요합니다")
	if "base_branch" in request:
		branch = request["base_branch"]
		if not isinstance(branch, str):
			raise ValueError("base_branch는 올바른 가지 이름 문자열이어야 합니다")
		try:
			if checks.git(Path(request_path).parent, "check-ref-format", "--branch", branch).strip() != branch:
				raise ValueError
		except (RuntimeError, ValueError):
			raise ValueError("base_branch는 올바른 가지 이름 문자열이어야 합니다") from None
	return data


def start_run(request_path: str, *, on_created=None) -> Run:
	data = read_request(request_path)
	request = data["request"]
	kind = request.get("kind", "code")
	budget = dict(DEFAULT_BUDGET)
	extra = data.get("budget", {})
	budget.update({k: extra[k] for k in ("calls", "fixes") if k in extra})
	if "minutes" in extra:
		budget["seconds"] = int(extra["minutes"] * 60)
	run = Run(run_id=new_run_id(), request=request, repo=str(Path(request.get("repo", ".")).resolve()), kind=kind, budget=budget)
	def record():
		shutil.copy(request_path, run.folder / "request.toml")
		run.save()
		run.event("start", goal=request["goal"], kind=kind)
	if on_created is None:
		record()
		run._exit_code = execute(run)
	else:
		with locked(run):
			record()
			try:
				on_created(run)
			except Exception:
				run.finish("실패", "실행 번호 연결 실패")
				raise
			run._exit_code = _execute_locked(run)
	return run


def cmd_run(request_path: str) -> int:
	return start_run(request_path)._exit_code


def execute(run: Run) -> int:
	with locked(run):
		return _execute_locked(run)


def _execute_locked(run: Run) -> int:
	Flow(run, load_plans(), orchestration_config()).run_all()
	finish_outputs(run)
	return 0 if run.status in ("승인 필요", "완료") else 1


def unmeasured_call_time(calls: list) -> bool:
	return any(not isinstance(call, dict) or "started" in call and (
		type(call.get("seconds")) not in (int, float) or call["seconds"] < 0
		or type(call["seconds"]) is float and not math.isfinite(call["seconds"])) for call in calls)


def _check_agency_owner(run: Run, owner) -> None:
	from agency import project
	from collector.backfill_common import safe
	from .flow import worktrees_dir
	if (not isinstance(owner, dict) or set(owner) != {"project", "task"}
			or not isinstance(owner["project"], str) or not project.ID.fullmatch(owner["project"])
			or not isinstance(owner["task"], str)):
		raise RuntimeError("재개할 실행의 프로젝트 소유 관계가 잘못됐습니다")
	row = project.load(owner["project"])
	matches = [task for task in row["tasks"] if task["run_id"] == run.run_id]
	active = len(matches) == 1 and matches[0]["status"] in ("실행 중", "대기", "실패") and (
		row["status"] == "실행 중" or row["status"] == "막힘" and row["blocked_from"] == "실행 중")
	if (not active or row["branch"] != run.request.get("base_branch") or matches[0]["id"] != owner["task"]):
		raise RuntimeError("다른 실행으로 교체된 작업은 재개할 수 없습니다")
	path = Path(matches[0]["request_path"])
	if not path.is_absolute() or not safe(path) or not path.resolve().is_relative_to(project._folder(owner["project"]).resolve()):
		raise RuntimeError("재개할 실행의 요청서 경로가 잘못됐습니다")
	data = read_request(str(path))
	copy = run.folder / "request.toml"
	if not safe(copy):
		raise RuntimeError("재개할 실행의 기록 경로가 잘못됐습니다")
	with copy.open("rb") as stream:
		copied = tomllib.load(stream)
	if data != copied or run.request != data["request"] or str(Path(run.request.get("repo", ".")).resolve()) != run.repo:
		raise RuntimeError("재개할 실행의 요청서 또는 저장소가 바뀌었습니다")
	if run.worktree:
		path = Path(run.worktree)
		if path != worktrees_dir() / run.run_id or not safe(path) or run.branch != "orch/" + run.run_id:
			raise RuntimeError("재개할 실행의 작업 폴더 소유 관계가 다릅니다")


def cmd_resume(run_id: str, options: list[str]) -> int:
	run = Run.load(run_id)
	if run.run_id != run_id:
		raise ValueError("실행 기록의 번호가 다릅니다")
	with locked(run):
		run = Run.load(run_id)
		if run.run_id != run_id:
			raise ValueError("실행 기록의 번호가 다릅니다")
		if run.status in ("승인 필요", "완료", "취소"):
			print(f"{run_id} 는 이미 끝났습니다: {run.status}")
			return 0
		if run.end_reason == "실행 번호 연결 실패":
			raise RuntimeError("연결 실패 기록을 확인하기 전에는 재개할 수 없습니다")
		if run.worktree and not Path(run.worktree).is_dir():
			raise RuntimeError("재개할 실행의 작업 폴더가 없습니다")
		project_id = ""
		if "agency_owner" in run.handoff:
			_check_agency_owner(run, run.handoff["agency_owner"])
			project_id = run.handoff["agency_owner"]["project"]
		elif "agency_usage" in run.handoff or (isinstance(run.request.get("base_branch"), str)
				and run.request["base_branch"].startswith("agency/p-")):
			from agency import project
			branch = run.request.get("base_branch", "")
			identity = branch.removeprefix("agency/") if isinstance(branch, str) and branch.startswith("agency/") else ""
			if not project.ID.fullmatch(identity):
				raise RuntimeError("재개할 실행의 프로젝트를 확인할 수 없습니다")
			row = project.load(identity)
			matches = [task for task in row["tasks"] if task.get("run_id") == run_id]
			if row["branch"] != branch or len(matches) != 1:
				raise RuntimeError("다른 실행으로 교체된 작업은 재개할 수 없습니다")
			_check_agency_owner(run, {"project": identity, "task": matches[0]["id"]})
			project_id = identity
		if ("agency_owner" in run.handoff or isinstance(run.request.get("base_branch"), str)
				and run.request["base_branch"].startswith("agency/p-")) and unmeasured_call_time(run.calls):
			raise RuntimeError("중단된 호출의 사용 시간을 확인하기 전에는 재개할 수 없습니다")
		for flag, value in zip(options[::2], options[1::2]):
			if flag == "--calls":
				run.budget["calls"] += int(value)
			elif flag == "--minutes":
				run.budget["seconds"] += int(value) * 60
		if project_id:
			from agency import project
			project.cap_run_budget(project.load(project_id), run)
		run.status, run.end_reason = "작업 중", ""
		run.event("resume", budget=run.budget)
		run.save()
		return _execute_locked(run)


def cmd_apply(run_id: str) -> int:
	"""검증을 통과한 변경을 본 저장소에 한 커밋으로 합친다. 사용자 승인 뒤에만 부른다(O3 A)."""
	run = Run.load(run_id)
	if run.kind != "code" or run.status != "승인 필요":
		raise RuntimeError(f"반영할 수 없는 상태입니다: {run.kind} · {run.status}")
	if run.request.get("base_branch"):
		raise RuntimeError("프로젝트 가지 기준 실행은 apply <run_id> --onto <가지> 로만 반영합니다")
	if checks.git(run.repo, "diff", "--cached", "--name-only").strip():
		raise RuntimeError("본 저장소에 스테이징된 변경이 있어 반영하지 않습니다")
	paths = [p for p in checks.git(run.repo, "diff", "--name-only", "-z", "--no-renames", run.base_commit, run.branch).split("\0") if p]
	dirty = checks.changed_paths(Path(run.repo), include_artifacts=True)
	overlaps = [p for p in paths if any(p == d or p.startswith(d + "/") or d.startswith(p + "/") for d in dirty)]
	if overlaps:
		raise RuntimeError(f"작업 중인 파일과 겹쳐 반영하지 않습니다: {len(overlaps)}개")
	selected = next((item for item in targets.targets().values() if str(item.path) == run.repo), None)
	message = commit_message(run, selected)
	merged = subprocess.run(["git", "-C", run.repo, "merge", "--squash", run.branch], capture_output=True, text=True)
	if merged.returncode != 0 or not checks.git(run.repo, "diff", "--cached", "--name-only").strip():
		checks.git(run.repo, "reset", "-q", "--merge", check=False)  # 반쯤 합친 상태를 남기지 않는다
		raise RuntimeError(f"합칠 변경이 없거나 충돌했습니다: {(merged.stdout + merged.stderr).strip()[-200:]}")
	checks.git(run.repo, "commit", "-q", "-m", message)
	commit = checks.git(run.repo, "rev-parse", "--short", "HEAD").strip()
	checks.git(run.repo, "worktree", "remove", "--force", run.worktree, check=False)
	checks.git(run.repo, "branch", "-D", run.branch, check=False)
	run.finish("완료", f"본 저장소에 반영 {commit}(push 안 함)")
	finish_outputs(run)
	if selected and any(path.startswith(prefix) for path in paths for prefix in selected.restart_paths):
		print("재기동 필요: " + selected.restart_notice)
	return 0


def main(argv: list[str]) -> int:
	if argv[:1] == ["chat"]:
		from .chat_cli import main as chat_main
		return chat_main(argv[1:])
	if argv[:1] == ["meeting"]:
		from .meeting_cli import main as meeting_main
		return meeting_main(argv[1:])
	if argv[:1] == ["draft"]:
		from .draft import main as draft_main
		return draft_main(argv[1:])
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
		if argv[:1] == ["apply"] and len(argv) == 4 and argv[2] == "--onto":
			print(apply_to_branch(argv[1], argv[3])[:12])
			return 0
	except (OSError, ValueError, RuntimeError, KeyError, ConfigError) as exc:
		print(f"orchestrator 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
		return 1
	print(USAGE, file=sys.stderr)
	return 2


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
