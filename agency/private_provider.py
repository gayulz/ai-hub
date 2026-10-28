"""입력 자료를 쓰는 문서 호출에서는 공급자 출력 원문을 오류 로그에 남기지 않는다."""

import json
import subprocess
import tempfile
import time
from pathlib import Path

from orchestrator import providers, readonly_guard


def call(plan, writer, prompt, cwd, schema, timeout):
	if writer:
		raise providers.ProviderError("write", "비공개 입력은 읽기 전용 호출만 허용합니다")
	with tempfile.TemporaryDirectory(prefix="agency-provider-") as temporary:
		schema_file, last_file = Path(temporary) / "schema.json", Path(temporary) / "last.json"
		schema_file.write_text(json.dumps(schema), encoding="utf-8")
		if plan.tool == "codex":
			argv, use_stdin = providers._codex(plan, writer, cwd, schema_file, last_file)
			flags = ["--ignore-user-config", "-c", 'web_search="disabled"']
			for feature in ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "remote_plugin",
					"tool_suggest", "multi_agent", "multi_agent_v2", "memories", "hooks", "view_image",
					"browser_use", "computer_use", "skill_search", "skill_mcp_dependency_install"):
				flags.extend(("--disable", feature))
			argv[-1:-1] = flags
		elif plan.tool == "claude":
			argv, use_stdin = providers._claude(plan, writer, schema)
			argv.extend(("--restricted", "--tools", "", "--strict-mcp-config", "--disable-slash-commands"))
		else:
			raise providers.ProviderError("missing", "비공개 입력에 도구 제한을 확인할 수 없는 요금제")
		before = readonly_guard.snapshot(cwd) if not writer else None
		if not writer and before is None:
			readonly_guard.verify(cwd, before, plan.tool)
		started = time.monotonic()
		try:
			result = subprocess.run(argv, input=prompt if use_stdin else None, cwd=cwd,
				capture_output=True, text=True, timeout=max(30.0, timeout))
		except subprocess.TimeoutExpired:
			raise providers.ProviderError("timeout", f"{plan.id} 시간 초과") from None
		except OSError:
			raise providers.ProviderError("missing", f"{plan.id} 실행 파일 없음") from None
		finally:
			seconds = time.monotonic() - started
			if not writer:
				readonly_guard.verify(cwd, before, plan.tool)
		if result.returncode:
			cause = providers._exit_cause(result.stderr or "", result.stdout or "")
			raise providers.ProviderError("exit", f"{plan.id} 종료 코드 {result.returncode} ({cause})")
		data, usage = providers._unwrap(plan.tool, result.stdout, last_file)
		return providers.Reply(data, seconds, usage)
