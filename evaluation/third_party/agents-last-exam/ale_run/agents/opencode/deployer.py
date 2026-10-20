from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from ale_run.agents._bootstrap import cua_bridge_env, ensure_cua_mcp_server, ensure_node_npm
from ale_run.base_interface import AgentRunResult, BaseAgentDeployer, TrajectoryBuilder

from .config import OpencodeConfig
from .transcript import parse_events_into

MARKER = ".awmeval-complete.md"
ASSETS = ("agent_runner.py", "vision_mcp.py", "output_guard.mjs")
REDACTED = "***"

_COMPLETION = """Completion protocol:
- Continue working until the original task is complete.
- Only after completion, write a concise completion summary to this exact file:
{marker}
- Do not write or modify that file before completion.
- After writing the completion summary, give the final response and stop."""

_CONTINUATION = """Continue the original task from the current session state.

The original task is repeated below verbatim and remains authoritative.
<original_task>
{task}
</original_task>

Do not restart or re-plan the task.

When the original task is complete, write a concise completion summary to this exact file, then stop:
{marker}

Do not write or modify that file before the original task is complete."""

_INSTRUCTIONS = """# Agents' Last Exam evaluation

Complete the user's professional task autonomously inside the current disposable ALE Linux sandbox. Follow every absolute input, software, and output path in the user request. The native ALE evaluator is the sole reward source.

## Tools

There is no general priority between the available shell, file, browser, and desktop interfaces. Choose the interface whose capability and existing state fit the operation.

- OpenCode's built-in `bash`, `read`, `write`, `edit`, `grep`, `glob`, and related tools run as the evaluated sandbox user in this same Linux environment. They directly affect the files and processes evaluated by ALE.
- The `desktop_*` MCP tools control the visible XFCE desktop. The tested policy model is text-only. Use `desktop_screenshot` to save a screenshot, then call `vision_describe_image` with the returned absolute PNG path and a focused question before choosing visual coordinates.
- The `browser-use` CLI is available through built-in bash with forms including `browser-use open URL`, `browser-use state`, `browser-use click INDEX`, `browser-use input INDEX TEXT`, `browser-use keys KEY`, `browser-use scroll down --amount 800`, and `browser-use screenshot PATH`.
- Playwright MCP is available through `browser_browser_*` tools. Browser-use and Playwright have independent browser state, cookies, and login sessions. Keep one stateful browser flow in the same interface.

Do not inspect hidden references, evaluator implementation, benchmark control files, model APIs, or policy request logs. Write final deliverables only to the output paths required by the task. Verify the final artifacts before finishing.
"""


def _load_runner(path: Path):
    """Import the shared awmeval agent runner uploaded next to ale_run."""
    spec = importlib.util.spec_from_file_location("awmeval_agent_runner", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _with_keys(config: dict, api_key: str, vision_api_key: str) -> dict:
    config = copy.deepcopy(config)
    for provider in config.get("provider", {}).values():
        options = provider.setdefault("options", {})
        options["authToken" if "authToken" in options else "apiKey"] = api_key
    vision = config.get("mcp", {}).get("vision")
    if isinstance(vision, dict):
        vision.setdefault("environment", {})["AWM_VISION_API_KEY"] = vision_api_key
    return config


class OpencodeDeployer(BaseAgentDeployer):
    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = ("events.jsonl", "stderr.log", "proxy.jsonl")

    @property
    def version(self) -> str:
        return "1.17.7"

    async def install(self) -> None:
        cfg: OpencodeConfig = self.config
        if not self.executor.sandbox.is_linux:
            raise RuntimeError("ALE OpenCode currently supports Linux sandboxes only")
        executable = Path(cfg.opencode_executable)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FileNotFoundError(f"OpenCode runtime is missing: {executable}")
        completed = await asyncio.to_thread(subprocess.run, [str(executable), "--version"], capture_output=True, text=True, timeout=60)
        if completed.returncode != 0 or completed.stdout.strip() != self.version:
            raise RuntimeError(f"OpenCode version mismatch: expected {self.version}, got {completed.stdout.strip() or completed.stderr.strip()}")
        missing = [str(Path(cfg.assets_dir) / name) for name in ASSETS if not (Path(cfg.assets_dir) / name).is_file()]
        if missing:
            raise FileNotFoundError(f"awmeval harness assets are missing: {missing}")
        for path in (cfg.browser_use_executable, cfg.playwright_mcp_cli, cfg.browser_python):
            if not Path(path).exists():
                raise FileNotFoundError(f"OpenCode browser runtime is missing: {path}")
        browser_probe = await asyncio.to_thread(
            subprocess.run,
            [cfg.browser_python, "-c", 'import importlib.metadata as m; assert m.version("browser-use") == "0.11.13"; assert m.version("playwright") == "1.61.0"; assert m.version("mcp") == "1.29.1"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if browser_probe.returncode != 0:
            raise RuntimeError(f"OpenCode Python browser runtime mismatch: {browser_probe.stderr.strip()}")
        node, _ = await ensure_node_npm()
        playwright_probe = await asyncio.to_thread(
            subprocess.run,
            [node, "-e", f'if(require({json.dumps(str(Path(cfg.playwright_mcp_cli).parent / "package.json"))}).version!=="0.0.78") process.exit(1)'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if playwright_probe.returncode != 0:
            raise RuntimeError("Playwright MCP version mismatch; expected 0.0.78")
        await ensure_cua_mcp_server(self.executor.sandbox)
        bridge_source = Path(__file__).resolve().parents[1] / "_assets" / "cua_mcp_server" / "src" / "index.js"
        bridge_target = Path(self.executor.sandbox.mcp_server_dir) / "src" / "index.js"
        shutil.copyfile(bridge_source, bridge_target)
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: OpencodeConfig = self.config
        work_dir = Path(self.executor.work_dir)
        workspace = work_dir / "workspace"
        state = work_dir / "state"
        short_tmp = Path("/tmp/ale-opencode")
        for path in (workspace, state / "home", state / "data", state / "cache", state / "config", state / "browser-use", short_tmp, state / "config" / "google-chrome" / "Crash Reports", state / "home" / ".config" / "google-chrome" / "Crash Reports", workspace / "playwright", workspace / "screenshots"):
            path.mkdir(parents=True, exist_ok=True)
        (state / "config" / "opencode").symlink_to(Path(cfg.opencode_config_home) / "opencode", target_is_directory=True)
        instruction_path = workspace / "EVALUATION.md"
        instruction_path.write_text(_INSTRUCTIONS, encoding="utf-8")
        instruction_path.chmod(0o444)

        assets = Path(cfg.assets_dir)
        node = shutil.which("node") or "/usr/local/bin/node"
        mcp = {
            "desktop": {
                "type": "local",
                "command": [node, str(Path(self.executor.sandbox.mcp_server_dir) / "src" / "index.js")],
                "environment": {
                    **cua_bridge_env(self.executor),
                    "CUA_MCP_IMAGE_RESPONSES": "omit",
                    "CUA_MCP_SCREENSHOT_DIR": str(workspace / "screenshots"),
                },
                "enabled": True,
                "timeout": cfg.mcp_timeout_ms,
            },
            "browser": {
                "type": "local",
                "command": [node, cfg.playwright_mcp_cli, "--executable-path", self._playwright_chrome(), "--headless", "--no-sandbox", "--ignore-https-errors", "--image-responses", "omit", "--output-dir", str(workspace / "playwright")],
                "environment": {
                    "HOME": str(state / "home"),
                    "XDG_CONFIG_HOME": str(state / "config"),
                    "XDG_CACHE_HOME": str(state / "cache"),
                    "TMPDIR": str(short_tmp),
                    "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers",
                },
                "enabled": True,
                "timeout": cfg.mcp_timeout_ms,
            },
            "vision": copy.deepcopy(cfg.vision),
        }
        config = {**copy.deepcopy(cfg.opencode), "mcp": mcp, "instructions": [str(instruction_path)], "plugin": [str(assets / "output_guard.mjs")]}
        config_path = work_dir / "opencode.json"
        spec = {
            **copy.deepcopy(cfg.runner),
            "opencode": cfg.opencode_executable,
            "config": str(config_path),
            "config_home": None,
            "workdir": str(workspace),
            "out": str(work_dir),
            "state": tempfile.mkdtemp(prefix="awmeval-opencode-", dir=short_tmp),
            "user": None,
            "env": {
                "PATH": f"/opt/opencode-web/bin:/usr/local/bin:/usr/bin:/bin:{os.environ.get('PATH', '')}",
                "XDG_CONFIG_HOME": str(state / "config"),
                "BROWSER_USE_CONFIG_DIR": str(state / "browser-use"),
                "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers",
                "TMPDIR": str(short_tmp),
            },
            "prompt": prompt.rstrip() + "\n\n" + _COMPLETION.format(marker=MARKER),
            "nudge": _CONTINUATION.format(task=prompt.strip(), marker=MARKER),
            "marker": str(workspace / MARKER),
        }
        (work_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        config_path.write_text(json.dumps(_with_keys(config, cfg.api_key, cfg.vision_api_key), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            runner = _load_runner(assets / "agent_runner.py").Runner(spec)
            result = await asyncio.to_thread(runner.run)
        finally:
            redacted = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else config
            config_path.write_text(json.dumps(_with_keys(redacted, REDACTED, REDACTED), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        stop, code = result.get("stop_reason"), result.get("returncode")
        if spec.get("proxy") and not result.get("model_calls"):
            return AgentRunResult(status="failed", duration_s=result.get("wall_time_s"), error="policy proxy recorded no model requests", transcript_path=str(work_dir / "events.jsonl"), stderr_path=str(work_dir / "stderr.log"))
        status = "timeout" if stop in {"timeout", "stall_timeout"} else "failed" if stop == "harness_error" or code not in {0, -15, 143} else "completed"
        error = f"OpenCode stopped with {stop or 'returncode'} ({code})" + (f": {result['error']}" if result.get("error") else "") if status == "failed" else None
        return AgentRunResult(status=status, duration_s=result.get("wall_time_s"), exit_code=code, error=error, transcript_path=str(work_dir / "events.jsonl"), stderr_path=str(work_dir / "stderr.log"))

    @classmethod
    def parse_artifacts(cls, *, work_dir: Path, config: OpencodeConfig, run_result: AgentRunResult, builder: TrajectoryBuilder) -> None:
        parse_events_into(work_dir, builder)
        builder.trajectory.extra.setdefault("opencode", {}).update({"run_status": run_result.status, "exit_code": run_result.exit_code, "error": run_result.error})

    @staticmethod
    def _playwright_chrome() -> str:
        candidates = sorted(Path("/opt/pw-browsers").glob("**/chrome"))
        executable = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
        if executable is None:
            raise FileNotFoundError("Playwright Chromium executable is missing under /opt/pw-browsers")
        return str(executable)
