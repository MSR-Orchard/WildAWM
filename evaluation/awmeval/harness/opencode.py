from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path

from wildawm_common import DESCRIBE_IMAGE_MCP
from wildawm_common.opencode_config import model_ref, provider_block

from ..config import ROOT
from ..sandbox import Sandbox, endpoint
from .assets.agent_runner import Runner

ASSETS = Path(__file__).with_name("assets")
REMOTE = "/tmp/awmeval"
RUNTIME = ROOT / "runtime" / "opencode"
PROVIDER = "awmeval"
DISABLED_TOOLS = ("task", "question", "plan_enter", "plan_exit", "todowrite", "webfetch", "websearch")
LIVE_TOOL_MARGIN = 32_768
MIN_PROMPT_BUDGET = 8_192

AGENT_PROMPT = "You are an autonomous personal assistant operating inside one disposable evaluation environment. Complete the user's task using the available native, MCP, browser, shell, and file tools. Follow the configured benchmark instructions for environment-specific tool interfaces. No browser, shell, or service interface has general priority; choose based on the operation's required capability and existing session state. Do not ask the user questions because no user is available during the run."
INTERACTIVE_AGENT_PROMPT = "You are an autonomous personal assistant operating inside one disposable evaluation environment. Complete the user's task using the available native, MCP, browser, shell, and file tools. Follow the configured benchmark instructions for environment-specific tool interfaces. No browser, shell, or service interface has general priority; choose based on the operation's required capability and existing session state. Follow the benchmark instructions when a simulated user is available."
VISION_NOTE = "Your current model is text-only. The optional `vision_describe_image` tool can read an image or screenshot saved to an absolute path under the task workspace or /tmp and return a text description. Browser and shell interfaces are documented in the configured benchmark instructions; no interface has general priority.\n"
COMPLETION = """Completion protocol:
- Continue working until the original task is complete.
- Only after completion, write a concise completion summary to this exact file:
{marker}
- Do not write or modify that file before completion.
- After writing the completion summary, give the final response and stop."""
CONTINUATION = """Continue the original task from the current session state.

The original task is repeated below verbatim and remains authoritative.
<original_task>
{task}
</original_task>

Do not restart or re-plan the task.

When the original task is complete, write a concise completion summary to this exact file, then stop:
{marker}

Do not write or modify that file before the original task is complete."""


class HarnessError(RuntimeError):
    pass


def initial_prompt(task: str, marker: str) -> str:
    return task.rstrip() + "\n\n" + COMPLETION.format(marker=marker)


def continuation_prompt(task: str, marker: str) -> str:
    return CONTINUATION.format(task=task.strip(), marker=marker)


def opencode_config(cfg, *, base_url: str, mcp: dict | None = None, instructions: list[str] = (), disabled_tools=(), agent_prompt: str | None = None) -> dict:
    """OpenCode config for the neutral `wildawm` primary agent."""
    agent, options = cfg.agent, cfg.opencode
    disabled = tuple(dict.fromkeys((*DISABLED_TOOLS, *disabled_tools)))
    context, output = int(agent.context_window), int(agent.max_output_tokens)
    primary = {"description": "Autonomous personal assistant for one evaluation environment", "mode": "primary", "prompt": agent_prompt or AGENT_PROMPT,
               "permission": {name: "deny" for name in disabled}}
    for key in ("temperature", "top_p"):
        if agent.get(key) is not None:
            primary[key] = float(agent[key])
    extra = {key: (int if key == "top_k" else float)(agent[key]) for key in ("top_k", "min_p", "presence_penalty", "repetition_penalty") if agent.get(key) is not None}
    if extra:
        primary["options"] = extra
    compaction = {"auto": bool(options.get("auto_compact", True))}
    if compaction["auto"]:
        compaction["reserved"] = output + min(LIVE_TOOL_MARGIN, output, max(0, context - output - MIN_PROMPT_BUDGET))
    timeout = int(agent.get("request_timeout", 1800)) * 1000
    model_variants = None
    if agent.get("api") == "anthropic":
        model_variants = {"variants": {"high": {"thinking": {"type": "adaptive", "display": "summarized"}, "effort": "high"}}}
    # Provider entry shared with the synthesis component.
    provider = provider_block(
        provider_id=PROVIDER, model=agent.model, base_url=base_url.rstrip("/"),
        api_key=str(agent.api_key), api=agent.get("api"), timeout_ms=timeout,
        model_fields={
            "temperature": agent.get("temperature") is not None,
            "reasoning": agent.get("reasoning_effort") not in (None, "none"),
        },
        limit={"context": context, "input": context, "output": output},
        model_extra=model_variants,
    )
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": provider,
        "model": model_ref(PROVIDER, agent.model),
        "default_agent": "wildawm",
        "instructions": list(instructions),
        "tools": {name: False for name in disabled},
        "permission": {"*": "allow", **{name: "deny" for name in disabled}},
        "mcp": mcp or {},
        "agent": {"wildawm": primary, **{name: {"disable": True} for name in ("title", "summary", "plan", "general", "explore")}},
        "snapshot": False,
        "share": "disabled",
        "autoupdate": False,
        "compaction": compaction,
    }


def vision_mcp(cfg, base_url: str, command: list[str] | None = None) -> dict:
    """Local MCP entry for the `describe_image` vision tool, pinned to the stdlib-only `raw` transport."""
    vision = cfg.vision
    return {
        "type": "local",
        "command": command or ["python3", f"{REMOTE}/vision_mcp.py"],
        "environment": {
            "AWM_VISION_BASE_URL": base_url,
            "AWM_VISION_MODEL": str(vision.model),
            "AWM_VISION_API_KEY": str(vision.api_key),
            "AWM_VISION_REASONING_EFFORT": str(vision.get("reasoning_effort") or "none"),
            "AWM_VISION_MAX_TOKENS": str(vision.get("max_output_tokens", 4096)),
            "AWM_VISION_TIMEOUT": str(vision.get("request_timeout", 180)),
            "AWM_VISION_RETRIES": str(vision.get("retries", 3)),
            "AWM_VISION_MCP_TRANSPORT": "raw",
        },
        "enabled": True,
        "timeout": int(cfg.opencode.mcp_timeout_ms),
    }


def remote_mcp(url: str, cfg, **extra) -> dict:
    return {"type": "remote", "url": url, "enabled": True, "timeout": int(cfg.opencode.mcp_timeout_ms), **extra}


def spec(cfg, *, opencode: str, config: str, workdir: str, out: str, task_text: str, marker: str, base_url: str, config_home: str | None,
         user: str | None = None, env: dict | None = None, prompt: str | None = None, nudge: str | None = None, timeout: int | None = None,
         stop_on_finish=()) -> dict:
    """Runner spec shared by in-sandbox and host-side episodes."""
    agent, options = cfg.agent, cfg.opencode
    proxy = None
    if options.get("policy_proxy", True) and agent.get("api", "openai") == "openai":
        sampling = {key: agent.get(key) for key in ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")}
        proxy = {"upstream": base_url, "sampling": {**sampling, "max_tokens": int(agent.max_output_tokens)}, "reasoning_effort": agent.get("reasoning_effort"), "max_calls": int(options.max_turns)}
    return {
        "opencode": opencode, "config": config, "config_home": config_home, "workdir": workdir, "out": out, "state": f"{out}/../state",
        "user": user, "env": env or {}, "model": f"{PROVIDER}/{agent.model}", "agent": "wildawm", "variant": agent.get("reasoning_effort") or "none",
        "prompt": prompt or initial_prompt(task_text, marker), "nudge": nudge or continuation_prompt(task_text, marker), "marker": marker,
        "timeout": int(timeout or cfg.run.task_timeout), "max_turns": int(options.max_turns), "completion_nudges": int(options.get("completion_nudges", 0)),
        "stream_retries": int(options.get("stream_retries", 2)), "stall_timeout": options.get("stall_timeout"), "proxy": proxy, "stop_on_finish": list(stop_on_finish),
    }


def upload_assets(sb: Sandbox) -> None:
    """Upload the runner assets and the single-file describe_image MCP server (as `vision_mcp.py`)."""
    sb.upload(ASSETS, REMOTE)
    sb.upload(DESCRIBE_IMAGE_MCP, f"{REMOTE}/vision_mcp.py")


def install(sb: Sandbox, cfg, binary: str = "opencode") -> tuple[str, str | None]:
    """Upload runner assets, plus the pinned OpenCode binary and plugin runtime when the image lacks them."""
    upload_assets(sb)
    version = str(cfg.opencode.version)
    found = sb.exec(f"command -v {shlex.quote(binary)} && {shlex.quote(binary)} --version", timeout=60).stdout.split()
    if len(found) >= 2 and found[-1] == version:
        executable = found[0]
    else:
        executable = f"{REMOTE}/bin/opencode"
        if not sb.exists(executable):
            sb.upload(RUNTIME / "bin" / "opencode", executable)
            sb.exec(["chmod", "755", executable], timeout=60).check("chmod opencode")
    config_home = None
    if cfg.opencode.get("output_guard", True):
        config_home = f"{REMOTE}/config-home"
        if not sb.exists(f"{config_home}/opencode/node_modules/@opencode-ai/plugin"):
            sb.upload(RUNTIME / "config", config_home)
    sb.exec(f"chmod -R a+rX {REMOTE}", timeout=120)
    return executable, config_home


def run_agent(sb: Sandbox, cfg, *, task_text: str, workdir: str, out: Path, mcp: dict | None = None, instructions: str = "", instruction_files=(),
              user: str | None = None, env: dict | None = None, prompt: str | None = None, nudge: str | None = None, marker: str | None = None,
              disabled_tools=(), agent_prompt: str | None = None, timeout: int | None = None, stop_on_finish=()) -> dict:
    """Run one OpenCode episode in `sb` and copy its artifacts to `out`; `instruction_files` are sandbox paths."""
    executable, config_home = install(sb, cfg)
    run_dir = f"{REMOTE}/runs/{uuid.uuid4().hex[:12]}"
    marker = marker or f"{workdir.rstrip('/')}/.awmeval-complete.md"
    base_url = endpoint(sb, cfg.agent)
    files = [*instruction_files, *([f"{run_dir}/EVALUATION.md"] if instructions else [])]
    config = opencode_config(cfg, base_url=base_url, mcp=mcp, instructions=files, disabled_tools=disabled_tools, agent_prompt=agent_prompt)
    if config_home:
        config["plugin"] = [f"{REMOTE}/output_guard.mjs"]
    runner = spec(cfg, opencode=executable, config=f"{run_dir}/opencode.json", workdir=workdir, out=f"{run_dir}/out", task_text=task_text, marker=marker,
                  base_url=base_url, config_home=config_home, user=user, env=env, prompt=prompt, nudge=nudge, timeout=timeout, stop_on_finish=stop_on_finish)
    if instructions:
        sb.write(files[-1], instructions.rstrip() + "\n")
    sb.write(f"{run_dir}/opencode.json", json.dumps(config, indent=2))
    sb.write(f"{run_dir}/spec.json", json.dumps(runner, indent=2))
    sb.exec(f"chmod -R a+rX {run_dir}", timeout=60)
    done = sb.exec(f"python3 {REMOTE}/agent_runner.py {shlex.quote(run_dir)}/spec.json", timeout=runner["timeout"] + 900)
    out.mkdir(parents=True, exist_ok=True)
    (out / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        sb.download(f"{run_dir}/out", out)
    except Exception as error:
        raise HarnessError(f"agent runner produced no artifacts (exit {done.exit_code}): {done.stderr[-1500:] or error}") from error
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    if result.get("stop_reason") == "harness_error":
        raise HarnessError(result.get("error") or f"opencode exited {result.get('returncode')}: {(out / 'stderr.log').read_text(errors='replace')[-1500:] if (out / 'stderr.log').exists() else ''}")
    return result


def run_local(cfg, *, opencode: str, workdir: Path, out: Path, task_text: str, mcp: dict | None = None, instructions: str = "", instruction_files=(),
              env: dict | None = None, base_url: str | None = None, marker: str | None = None, on_event=None, timeout: int | None = None, disabled_tools=(),
              prompt: str | None = None, nudge: str | None = None) -> dict:
    """Run one OpenCode episode on the host; `instruction_files` are loaded before `instructions`."""
    out.mkdir(parents=True, exist_ok=True)
    config_home = str(RUNTIME / "config") if cfg.opencode.get("output_guard", True) else None
    base_url = base_url or cfg.agent.base_url
    instructions_path = out / "EVALUATION.md"
    files = [*instruction_files, *([str(instructions_path)] if instructions else [])]
    config = opencode_config(cfg, base_url=base_url, mcp=mcp, instructions=files, disabled_tools=disabled_tools)
    if config_home:
        config["plugin"] = [str(ASSETS / "output_guard.mjs")]
    marker = marker or str(workdir / ".awmeval-complete.md")
    if instructions:
        instructions_path.write_text(instructions.rstrip() + "\n", encoding="utf-8")
    (out / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    runner = spec(cfg, opencode=opencode, config=str(out / "opencode.json"), workdir=str(workdir), out=str(out / "agent"), task_text=task_text, marker=marker,
                  base_url=base_url, config_home=config_home, env=env, prompt=prompt, nudge=nudge, timeout=timeout)
    runner["state"] = str(out / "state")
    result = Runner(runner).run(on_event=on_event)
    if result.get("stop_reason") == "harness_error":
        raise HarnessError(result.get("error") or f"opencode exited {result.get('returncode')}")
    return result
