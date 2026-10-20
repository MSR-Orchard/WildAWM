"""Run one OSWorld v2 trial inside the sandbox: QEMU guest, native setup, OpenCode agent, native evaluation."""
from __future__ import annotations

import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import agent_runner

from . import patches
from .desktop_mcp import DesktopMCP, TaskToolPolicy, task_tool_policy
from .embedded import LOG as QEMU_LOG, patch_embedded_qemu_provider

PLAYWRIGHT_CLI = "/opt/awmeval-node/node_modules/@playwright/mcp/cli.js"
AUX_MCP_TIMEOUT_MS = 240_000
AGENT_PATH = "/opt/awmeval/bin:/workspace/bin:/opt/opencode-web/bin:/opt/osworld-v2/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MARKER = ".awmeval-complete.md"
AUDIT = "Perform one final evidence audit against every requirement in the original task and every explicit instruction in its named context sources. Do not merely summarize. If any required action was deferred, listed as remaining, converted into a recommendation, or not verified, continue now and complete it through the permitted tools. If and only if all requirements are already complete, rewrite .awmeval-complete.md with the verified evidence and stop without changing the completed state."
_JUDGE_PATCHED = False


@dataclass(frozen=True)
class EvaluationValue:
    score: float
    details: Any


class AgentRunner(agent_runner.Runner):
    """Core runner whose OpenCode process sees only its own allowlisted environment, never root credentials."""

    def command(self, *args: str) -> tuple[list[str], dict]:
        return ["/usr/sbin/runuser", "-u", self.spec["user"], "--", "env", "-i", *[f"{key}={value}" for key, value in self.env.items()], self.spec["opencode"], *args], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


def instructions(policy: TaskToolPolicy) -> str:
    constraints = "\n".join(f"- {value}" for value in policy.constraints) or "- No additional interaction constraint was extracted; follow every interface requirement in the task verbatim."
    if policy.browser_automation:
        browser_guidance = "The evaluated Chrome session is already open. The browser-use CLI and Playwright MCP both attach to this same evaluated session. There is no general priority between them; keep a stateful flow in one interface unless the current task requires a capability from the other. Run browser-use through the built-in bash tool with forms such as `browser-use tabs`, `browser-use state`, `browser-use click <index>`, `browser-use input <index> \"text\"`, `browser-use keys \"Enter\"`, `browser-use scroll down --amount 800`, and `browser-use screenshot /workspace/page.png`. Playwright tools are named `browser_browser_*`, including `browser_browser_tabs`, `browser_browser_snapshot`, `browser_browser_click`, `browser_browser_type`, `browser_browser_evaluate`, and `browser_browser_take_screenshot`; pass the bare current snapshot reference in the `target` field. Refresh browser state after navigation or interaction instead of reusing stale indices or references."
    else:
        browser_guidance = "Browser-use, Playwright MCP, curl, and wget are disabled because the task explicitly requires visible or manual browser interaction. Use OSWorld screenshot, accessibility, mouse, and keyboard tools for that interaction."
    if policy.guest_execution:
        guest_guidance = "Use osworld_guest_bash, osworld_guest_python, and osworld_guest_write_file when work must change files or state inside the evaluated Ubuntu guest. Use osworld_guest_start_bash plus osworld_guest_process_status for long renders, exports, builds, and simulations."
    else:
        guest_guidance = "Guest shell and Python execution are disabled because the task explicitly requires visible or application-only interaction. Use the requested GUI application and OSWorld desktop tools."
    return f"""# OSWorld v2 evaluation

Complete the user's task in the evaluated OSWorld v2 Ubuntu VM and verify the final state. There is no general priority between the available desktop, browser, guest shell, harness shell, and file interfaces; choose based on the operation's required capability, existing state, and every explicit interface requirement in the task.

OpenCode's built-in bash, Python, read, write, edit, grep, glob, and related tools operate in the separate harness workspace at `/workspace`. They are available for browser-use, temporary files, parsing, and other harness-local work. They do not directly modify the evaluated Ubuntu guest. {guest_guidance}

Use `osworld_screenshot`, `osworld_accessibility_tree`, and the `osworld_*` mouse and keyboard tools for desktop applications. The tested policy model is text-only. If visual contents are needed, save a screenshot under `/workspace` and call `vision_describe_image` with the absolute path and a focused question. {browser_guidance}

OSWorld v2 may score guest files, application state, website state, rendered output, and multi-application workflows. The official native evaluator is the sole reward source. Read every context source named by the user. Preserve required workflow order and explicit interaction constraints. Do not inspect task source, evaluator source, ground truth, private cache files, control APIs, model APIs, or hidden task routes. If a task names an exact resource, account, region, tab, application, or selected item, verify that identity in the permitted interface before an irreversible action.

Explicit interaction constraints extracted from this task:
{constraints}

Call `osworld_done` only after the requested final state is complete and verified. Call `osworld_fail` only when the task is genuinely infeasible. The completion marker ends the agent loop; official native evaluation remains authoritative."""


def runtime_environment(config: dict[str, Any], private_dir: Path) -> dict[str, str]:
    osworld, judge = config["osworld"], config["judge"]
    values = {
        "OSWORLD_FILE_BASE_URL": str(config["assets_dir"]),
        "WEBSITE_HOST_SUFFIX": str(osworld["website_host_suffix"]),
        "OSWORLD_EVAL_MODEL_PROVIDER": "openai_compatible",
        "OSWORLD_EVAL_MODEL_NAME": str(judge["model"]),
        "OSWORLD_EVAL_MODEL_API_KEY": str(judge["api_key"]),
        "OSWORLD_EVAL_MODEL_BASE_URL": str(judge["base_url"]),
        "OSWORLD_EVAL_MODEL_MAX_OUTPUT_TOKENS": str(judge["max_output_tokens"]),
        "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS": "3",
        "OSWORLD_EVAL_MODEL_RETRY_DELAY": "2",
        "OSWORLD_EVAL_SAVE_RAW_DIR": str(private_dir / "model_eval_file_log"),
        "EVAL_LLM_BASE_URL": str(judge["base_url"]),
        "OPENAI_API_KEY": str(judge["api_key"]),
        "OPENAI_BASE_URL": str(judge["base_url"]),
        "OSWORLD_USER_SIM_PROVIDER": "openai_compatible",
        "OSWORLD_USER_SIM_MODEL": str(judge["model"]),
        "OSWORLD_USER_SIM_API_KEY": str(judge["api_key"]),
        "OSWORLD_USER_SIM_BASE_URL": str(judge["base_url"]),
        "OSWORLD_USER_SIM_MAX_TOKENS": str(judge["max_output_tokens"]),
    }
    if judge.get("reasoning_effort") is not None:
        values["OSWORLD_EVAL_MODEL_REASONING_EFFORT"] = str(judge["reasoning_effort"])
    for name, value in (osworld.get("environment") or {}).items():
        if name in values:
            raise ValueError(f"benchmarks.osworld2.environment cannot override adapter-controlled variable {name}")
        values[str(name)] = str(value)
    if config.get("gitlab"):
        values["GITLAB_URL"] = config["gitlab"]["url"]
        values["GITLAB_PRIVATE_TOKEN"] = config["gitlab"]["token"]
    return values


def enforce_judge_identity(judge: dict[str, Any]) -> None:
    """Force every native LLM evaluator and user-simulator backend onto the configured judge."""
    global _JUDGE_PATCHED
    if _JUDGE_PATCHED:
        return
    from desktop_env.evaluators import model_client

    original = model_client._build_config

    def build_config(options, *, default_provider, default_model, default_api_key_env, default_max_tokens):
        forced = dict(options or {})
        forced.update({
            "provider": "openai_compatible",
            "model": str(judge["model"]),
            "api_key": str(judge["api_key"]),
            "base_url": str(judge["base_url"]),
            "max_tokens": int(judge["max_output_tokens"]),
            "reasoning_effort": None if judge.get("reasoning_effort") is None else str(judge["reasoning_effort"]),
        })
        return original(forced, default_provider=default_provider, default_model=default_model, default_api_key_env=default_api_key_env, default_max_tokens=default_max_tokens)

    model_client._build_config = build_config
    _JUDGE_PATCHED = True


def load_task(source_dir: Path, task_id: str) -> Any:
    task_loader = importlib.import_module("task_loader")
    return task_loader.load_task_config(None, task_id=task_id, base_dir=str(source_dir / "evaluation_examples"), eval_version="v2", prefer_class=True)


def evaluate_value(value: Any) -> EvaluationValue:
    details = value if isinstance(value, dict) else None
    score = float(value.get("score", 0.0) if isinstance(value, dict) else value)
    if not math.isfinite(score):
        raise ValueError(f"OSWorld v2 evaluator returned a non-finite score: {score}")
    return EvaluationValue(score=max(0.0, min(1.0, score)), details=details)


def evaluate(call: Any) -> EvaluationValue:
    """Run a native (phase) evaluator; malformed agent artifacts score 0 instead of crashing it."""
    try:
        return evaluate_value(call())
    except AttributeError as error:
        if "'CT_Slide' object has no attribute 'rId'" in str(error):
            return EvaluationValue(0.0, {"evaluator_error": f"invalid agent-produced PPTX: {error}"})
        raise
    except TypeError as error:
        frames = traceback.extract_tb(error.__traceback__)
        if str(error) == "float() argument must be a string or a real number, not 'NoneType'" and any(frame.name == "read_cell_value" and frame.filename.endswith("desktop_env/evaluators/metrics/utils.py") for frame in frames):
            return EvaluationValue(0.0, {"evaluator_error": f"invalid agent-produced spreadsheet with uncached formula value: {error}"})
        raise
    except KeyError as error:
        frames = traceback.extract_tb(error.__traceback__)
        if re.fullmatch(r"'Worksheet .+ does not exist\.'", str(error)) and any(frame.name == "__getitem__" and frame.filename.endswith("openpyxl/workbook/workbook.py") for frame in frames):
            return EvaluationValue(0.0, {"evaluator_error": f"invalid agent-produced spreadsheet missing a required worksheet: {error}"})
        raise
    except ET.ParseError as error:
        frames = traceback.extract_tb(error.__traceback__)
        if any(frame.filename.endswith("xml/etree/ElementTree.py") for frame in frames):
            return EvaluationValue(0.0, {"evaluator_error": f"invalid agent-produced XML artifact: {error}"})
        raise


def tool_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    values: Counter[str] = Counter()
    for event in events:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if part.get("type") == "tool" and state.get("status") in {"completed", "error"}:
            values[str(part.get("tool") or "unknown")] += 1
    return dict(values)


def capture_best_effort(env: Any, path: Path, record: dict[str, Any], error_key: str) -> None:
    try:
        patches.capture_file(env, path)
    except Exception as error:
        record[error_key] = f"{type(error).__name__}: {error}"


def terminal_action(env: Any, action: str) -> None:
    last = env.action_history[-1] if env.action_history else None
    last_type = last.get("action_type") if isinstance(last, dict) else last
    if last_type not in {"DONE", "FAIL"}:
        env.step(action, pause=0)


def run_agent_phase(config: dict[str, Any], env: Any, server: DesktopMCP, policy: TaskToolPolicy, workspace: Path, artifact_dir: Path, task_text: str) -> dict[str, Any]:
    user = config["agent_user"]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    instruction_path = workspace / "EVALUATION.md"
    if instruction_path.exists():
        instruction_path.chmod(0o644)
    instruction_path.write_text(instructions(policy).rstrip() + "\n", encoding="utf-8")
    instruction_path.chmod(0o444)
    (workspace / "playwright").mkdir(exist_ok=True)
    browser = policy.browser_automation
    cdp = f"http://127.0.0.1:{env.chromium_port if browser else 1}"
    opencode = json.loads(json.dumps(config["opencode"]))
    opencode["instructions"] = ["/workspace/EVALUATION.md"]
    opencode["mcp"] = {"osworld": server.config, **opencode.get("mcp", {})}
    if browser:
        opencode["mcp"]["browser"] = {"type": "local", "command": ["node", PLAYWRIGHT_CLI, "--cdp-endpoint", f"http://127.0.0.1:{env.chromium_port}", "--shared-browser-context", "--image-responses", "omit", "--output-dir", "/workspace/playwright", "--save-session"], "enabled": True, "timeout": AUX_MCP_TIMEOUT_MS}
    state = artifact_dir / "state"
    for path in (state / "tmp", state / "browser-use", state / "browser-harness" / "home", state / "browser-harness" / "config", state / "browser-harness" / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
    config_path = artifact_dir / "opencode.json"
    config_path.write_text(json.dumps(opencode, indent=2), encoding="utf-8")
    config_path.chmod(0o644)
    prompts = config["prompts"]
    spec = dict(config["runner"])
    spec.update({
        "config": str(config_path), "workdir": str(workspace), "out": str(artifact_dir / "agent"), "state": str(state), "marker": str(workspace / MARKER),
        "prompt": task_text.rstrip() + "\n\n" + prompts["completion"].format(marker=MARKER),
        "nudge": prompts["continuation"].format(task=task_text.strip(), marker=MARKER),
        "env": {
            "TMPDIR": str(state / "tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
            "AWMEVAL_CDP_URL": cdp, "BU_CDP_URL": cdp, "BROWSER_USE_CONFIG_DIR": str(state / "browser-use"),
            "BH_AGENT_WORKSPACE": "/workspace", "BH_HOME": str(state / "browser-harness" / "home"), "BH_CONFIG_DIR": str(state / "browser-harness" / "config"),
            "BH_RUNTIME_DIR": str(state / "browser-harness" / "runtime"), "BH_TMP_DIR": "/tmp/browser-harness", "BH_TELEMETRY": "0", "BH_DOMAIN_SKILLS": "0",
            "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers", "PATH": AGENT_PATH,
        },
    })
    subprocess.run(["install", "-d", "-m", "0700", "-o", user, "-g", user, "/tmp/opencode"], check=True, timeout=60)
    subprocess.run(["chown", "-R", f"{user}:{user}", str(workspace)], check=True, timeout=120)
    runner = AgentRunner(spec)

    def completion_audit(events: list[dict[str, Any]]) -> str | None:
        if not (workspace / MARKER).is_file():
            return None
        if agent_runner.summarize(events)["turns"] >= int(spec["max_turns"]):
            return None
        return AUDIT

    try:
        result = runner.run(followup=completion_audit, max_followups=1)
    finally:
        config_path.unlink(missing_ok=True)
    for provider in opencode["provider"].values():
        for key in ("apiKey", "authToken"):
            if key in provider.get("options", {}):
                provider["options"][key] = "[redacted]"
    (artifact_dir / "agent" / "opencode.json").write_text(json.dumps(opencode, indent=2), encoding="utf-8")
    events = result.pop("events", [])
    if result["stop_reason"] == "harness_error":
        raise RuntimeError(f"OpenCode harness failed after {result['turns']} policy turn(s): {result.get('error') or 'see agent/stderr.log'}")
    if result["stop_reason"] == "timeout" and not events:
        raise RuntimeError("OpenCode startup timeout: no model events were emitted")
    if result["completed"]:
        terminal_action(env, "DONE")
    return {
        "tool_counts": tool_counts(events),
        "opencode_turns": result["turns"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "agent_wall_time_seconds": result["wall_time_s"],
        "returncode": result["returncode"],
        "stop_reason": result["stop_reason"],
        "completion_marker": result["completed"],
        "completion_summary": result["completion_summary"],
        "followups": result["followups"],
        "model_calls": result["model_calls"],
        "session_id": result["session_id"],
    }


def run_trial(config: dict[str, Any]) -> dict[str, Any]:
    task_id = config["task_id"]
    osworld = config["osworld"]
    output = Path(config["output"])
    artifact_dir = output / "trial"
    shutil.rmtree(artifact_dir, ignore_errors=True)
    artifact_dir.mkdir(parents=True)
    Path("/tmp").chmod(0o1777)
    private_dir = Path("/tmp/osworld-v2-private") / f"{task_id}-{os.getpid()}"
    private_dir.parent.mkdir(parents=True, exist_ok=True)
    private_dir.parent.chmod(0o700)
    shutil.rmtree(private_dir, ignore_errors=True)
    private_dir.mkdir(mode=0o700)
    os.environ.update(runtime_environment(config, private_dir))
    source_dir = Path(config["source_dir"])
    assets_dir = Path(config["assets_dir"])
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    os.chdir(source_dir)
    patch_embedded_qemu_provider(ram_size=str(osworld["guest_ram"]), cpu_cores=int(osworld["guest_cpus"]), startup_timeout_seconds=int(osworld["qemu_startup_timeout"]), expose_task_service=True)
    enforce_judge_identity(config["judge"])
    task = load_task(source_dir, task_id)
    instruction = str(patches.task_value(task, "instruction", "")).strip()
    if not instruction:
        raise ValueError(f"OSWorld v2 task {task_id} produced an empty instruction")
    respect = bool(osworld["respect_task_tool_constraints"])
    policy = task_tool_policy(instruction, respect_constraints=respect)
    browser_declared_by_task = patches.task_expects_browser(task)
    workspace = Path(config["workspace"])
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.parent.chmod(0o755)
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    link = Path("/workspace")
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.exists():
        shutil.rmtree(link)
    link.symlink_to(workspace, target_is_directory=True)
    patches.install_restricted_wrappers(policy, task_id)
    record: dict[str, Any] = {
        "task_id": task_id,
        "task_instance_id": str(patches.task_value(task, "id", task_id)),
        "instruction": instruction,
        "related_apps": list(patches.task_value(task, "related_apps", []) or []),
        "task_tool_policy": asdict(policy),
        "status": "infrastructure_error",
    }
    env: Any = None
    server: DesktopMCP | None = None
    recording_started = False
    started = time.monotonic()
    try:
        from desktop_env.desktop_env import DesktopEnv

        env = DesktopEnv(provider_name="docker", path_to_vm=str(config["disk_path"]), os_type="Ubuntu", action_space="pyautogui", cache_dir=str(private_dir / "cache"), headless=True, require_a11y_tree=False,
                         client_password=str(config["client_password"]), volume_size=patches.requested_volume_size(task, task_id, osworld.get("default_volume_size")))
        patches.patch_task_service_url(task, env)
        patches.patch_stateful_task_asset_urls(task, task_id, assets_dir)
        patches.patch_task_055_reference_setup(env, task_id, assets_dir, private_dir)
        patches.patch_unctad_guest_access(task, task_id, str(config["client_password"]), config.get("unctad_forward"))
        patches.patch_gitlab_guest_access(task, env, task_id, str(config["client_password"]), config.get("gitlab"))
        patches.patch_guest_accessibility_server(task, env)
        if getattr(env.provider, "software_virgl", False):
            patches.enable_qemu_virgl_chrome(env)
        env.reset(task_config=task)
        if float(osworld["startup_wait_seconds"]):
            time.sleep(float(osworld["startup_wait_seconds"]))
        if policy.browser_automation:
            try:
                patches.wait_for_chrome_cdp(env.chromium_port, timeout_seconds=300 if browser_declared_by_task else 5)
            except TimeoutError as initial_cdp_error:
                patches.recover_chrome_cdp(env, initial_cdp_error)
                record["chrome_cdp_recovered"] = True
        patches.capture_file(env, artifact_dir / "initial.png")
        if bool(osworld["record_video"]):
            env.controller.start_recording()
            recording_started = True
        max_actions = osworld.get("max_actions")
        server = DesktopMCP(env, workspace, max_actions=int(max_actions) if max_actions is not None else None, policy=policy, timeout_ms=int(osworld["mcp_timeout_ms"]))
        server.start()
        patches.prepare_task_visual_state(env, task_id, artifact_dir)
        phases_getter = getattr(task, "get_phases", None)
        phases = phases_getter() if callable(phases_getter) else []
        if phases:
            if not isinstance(phases, list):
                raise TypeError("OSWorld v2 get_phases() must return a list")
            phase_records: list[dict[str, Any]] = []
            total_score = 0.0
            installed_policy = policy
            for phase_index, phase in enumerate(phases, start=1):
                if phase_index > 1:
                    env._step_no = 0
                    env.action_history.clear()
                    env._traj_no += 1
                    phase["setup"](env.setup_controller, use_proxy=bool(patches.task_value(task, "proxy", False) and env.enable_proxy))
                    env.is_environment_used = True
                    if float(osworld["phase_setup_wait_seconds"]):
                        time.sleep(float(osworld["phase_setup_wait_seconds"]))
                phase_instruction = str(phase["instruction"])
                env.instruction = phase_instruction
                phase_policy = task_tool_policy(phase_instruction, respect_constraints=respect)
                server.set_policy(phase_policy)
                if phase_policy != installed_policy:
                    patches.install_restricted_wrappers(phase_policy, task_id)
                    installed_policy = phase_policy
                agent_result = run_agent_phase(config, env, server, phase_policy, workspace, artifact_dir / f"phase-{phase_index:02d}", phase_instruction)
                evaluation = evaluate(lambda: phase["evaluate"](env))
                total_score += evaluation.score
                phase_record = {"phase_index": phase_index, "phase_name": str(phase.get("name") or f"Phase {phase_index}"), "instruction": phase_instruction, "score": evaluation.score, "evaluation_details": evaluation.details, **agent_result}
                phase_records.append(phase_record)
                gate_min = phase.get("gate_min_score")
                if gate_min is not None and evaluation.score < float(gate_min):
                    phase_record["gate_stop"] = "gate_min_score"
                    break
                if phase.get("gate") and evaluation.score <= 0.0:
                    phase_record["gate_stop"] = "gate"
                    break
            evaluation = EvaluationValue(round(max(0.0, min(1.0, total_score)), 4), {"phase_results": phase_records})
            counts: Counter[str] = Counter()
            for phase_record in phase_records:
                counts.update(phase_record["tool_counts"])
            aggregate = {
                "tool_counts": dict(counts),
                "opencode_turns": sum(int(item["opencode_turns"]) for item in phase_records),
                "input_tokens": sum(int(item["input_tokens"]) for item in phase_records),
                "output_tokens": sum(int(item["output_tokens"]) for item in phase_records),
                "agent_wall_time_seconds": sum(float(item["agent_wall_time_seconds"]) for item in phase_records),
                "phase_results": phase_records,
            }
        else:
            aggregate = run_agent_phase(config, env, server, policy, workspace, artifact_dir, instruction)
            if float(osworld["settle_seconds"]):
                time.sleep(float(osworld["settle_seconds"]))
            capture_best_effort(env, artifact_dir / "final-before-evaluate.png", record, "final_before_capture_error")
            if task_id == "055" and recording_started:
                try:
                    patches.stop_recording_and_release_guest_space(env, artifact_dir / "recording.mp4")
                finally:
                    recording_started = False
            evaluation = evaluate(env.evaluate)
        capture_best_effort(env, artifact_dir / "final-after-evaluate.png", record, "final_capture_error")
        score = evaluation.score
        record.update({
            "status": "passed" if score >= 1.0 - 1e-9 else "failed",
            "success": score >= 1.0 - 1e-9,
            "score": score,
            "evaluation_details": evaluation.details,
            "actions": server.action_count,
            "captures": server.capture_count,
            "action_log": server.action_log,
            "user_simulator_history": env.user_simulator.get_history() if env.user_simulator is not None else [],
            **aggregate,
        })
    except BaseException as error:
        record.update({"status": "infrastructure_error", "success": None, "score": None, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()})
    finally:
        if server is not None:
            server.stop()
        if env is not None and recording_started:
            try:
                env.controller.end_recording(str(artifact_dir / "recording.mp4"))
            except Exception as error:
                record.setdefault("recording_error", f"{type(error).__name__}: {error}")
        if env is not None:
            try:
                env.close()
            except Exception as error:
                record.setdefault("cleanup_error", f"{type(error).__name__}: {error}")
        shutil.rmtree(artifact_dir / "workspace", ignore_errors=True)
        if workspace.exists():
            shutil.copytree(workspace, artifact_dir / "workspace", symlinks=True)
        if QEMU_LOG.is_file():
            shutil.copy2(QEMU_LOG, artifact_dir / "embedded-qemu.log")
        shutil.rmtree(artifact_dir / "private_artifacts", ignore_errors=True)
        if private_dir.exists():
            shutil.copytree(private_dir, artifact_dir / "private_artifacts", symlinks=True, ignore=shutil.ignore_patterns("cache"))
        shutil.rmtree(private_dir, ignore_errors=True)
        record["wall_time_seconds"] = round(time.monotonic() - started, 3)
    return record


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    output = Path(config["output"])
    try:
        record = run_trial(config)
    except BaseException as error:
        record = {"task_id": config["task_id"], "status": "infrastructure_error", "success": None, "score": None, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    temporary = output / "result.json.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, output / "result.json")
    print(f"osworld2 task={record['task_id']} status={record['status']} score={record.get('score')}", flush=True)
    return 0 if record["status"] != "infrastructure_error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
