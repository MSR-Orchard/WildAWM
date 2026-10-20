"""Pod-side ClawEval agent: mock services, MCP tool servers, one OpenCode episode via the shared runner, trace without TraceEnd."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

POD_AGENT_DIR = Path("/var/lib/claw-eval-agent")
PROGRESS_PATH = POD_AGENT_DIR / "progress.jsonl"
RESULT_PATH = POD_AGENT_DIR / "result.json"
CLAW_EVAL_ROOT = Path(__file__).resolve().parents[3]
SANDBOX_SERVER_URL = "http://127.0.0.1:18080"
MARKER = ".awmeval-complete.md"
TERMINATED = (0, -15, -9, 143)


def _emit_progress(stage: str, **fields: Any) -> None:
    try:
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROGRESS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": stage, "ts": time.time(), **fields}, ensure_ascii=False) + "\n")
            handle.flush()
    except Exception:
        pass


def _write_result(payload: dict[str, Any]) -> None:
    try:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RESULT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RESULT_PATH)
    except Exception:
        pass


def _build_user_agent(values: dict[str, Any]):
    if not values.get("enabled"):
        return None
    from .user_agent import UserAgent

    return UserAgent(
        model_id=values["model_id"],
        api_key=values["api_key"],
        base_url=values["base_url"],
        max_output_tokens=int(values.get("max_output_tokens", 65536)),
        request_timeout_seconds=float(values.get("request_timeout_seconds", 600.0)),
        retry_attempts=int(values.get("retry_attempts", 3)),
    )


def _start_pod_services(profile: dict[str, Any]):
    """Start the task's mock services inside the pod with cwd at the uploaded claw-eval root."""
    values = profile.get("services") or []
    if not values:
        return None
    from ..models.task import ServiceDef
    from .services import ServiceManager

    manager = ServiceManager([ServiceDef.model_validate(item) for item in values], cwd=CLAW_EVAL_ROOT, mock_today=profile.get("mock_today"))
    manager.__enter__()
    return manager


def _write_audit_snapshots(writer, trace_id: str, profile: dict[str, Any]) -> None:
    """Fetch each in-pod service's audit snapshot into the trace (best-effort)."""
    import httpx

    from ..models.trace import AuditSnapshot

    for service in profile.get("services") or []:
        reset = service.get("reset_endpoint")
        if not reset:
            continue
        audit_url = reset.rsplit("/reset", 1)[0] + "/audit"
        try:
            response = httpx.get(audit_url, timeout=5.0, trust_env=False)
            writer.write_event(AuditSnapshot(trace_id=trace_id, service_name=service.get("name", ""), audit_url=audit_url, audit_data=response.json()))
        except Exception:
            pass


def _wait_for_sandbox_server(sandbox_url: str, *, timeout_seconds: float = 120.0) -> None:
    """Block until the in-pod sandbox server answers /health, else fail the trial as an infrastructure error."""
    import httpx

    started = time.monotonic()
    while time.monotonic() < started + timeout_seconds:
        try:
            if httpx.get(sandbox_url.rstrip("/") + "/health", timeout=2.0, trust_env=False).status_code == 200:
                _emit_progress("sandbox_server_ready", waited_s=round(time.monotonic() - started, 1), url=sandbox_url)
                return
        except Exception:
            pass
        time.sleep(0.5)
    _emit_progress("sandbox_server_gate_timeout", waited_s=round(time.monotonic() - started, 1), url=sandbox_url)
    raise RuntimeError(f"In-pod sandbox server never became ready at {sandbox_url} within {timeout_seconds:.0f}s")


def _wait_for_vision_endpoint(vision_url: str, *, timeout_seconds: float = 120.0) -> bool:
    """Soft gate: wait (bounded) for the vision endpoint; a task may not need vision at all."""
    import httpx

    started = time.monotonic()
    while time.monotonic() < started + timeout_seconds:
        try:
            if httpx.get(vision_url + "/models", timeout=2.0, trust_env=False).status_code == 200:
                _emit_progress("vision_endpoint_ready", waited_s=round(time.monotonic() - started, 1), url=vision_url)
                return True
        except Exception:
            pass
        time.sleep(0.5)
    _emit_progress("vision_endpoint_gate_timeout", waited_s=round(time.monotonic() - started, 1), url=vision_url)
    return False


def _conversation_messages(task_text: str, events: list[dict[str, Any]]):
    from ..models.content import TextBlock
    from ..models.message import Message
    from .opencode_runner import _event_part

    messages = [Message(role="user", content=[TextBlock(text=task_text)])]
    pending: list[str] = []
    for event in events:
        part = _event_part(event)
        if part.get("type") in {"text", "reasoning"} and isinstance(part.get("text"), str):
            pending.append(part["text"])
        elif part.get("type") == "step-finish" or event.get("type") == "step_finish":
            if pending:
                messages.append(Message(role="assistant", content=[TextBlock(text="".join(pending))]))
                pending = []
        elif part.get("type") == "user" and isinstance(part.get("text"), str):
            messages.append(Message(role="user", content=[TextBlock(text=part["text"])]))
    return messages


def _loop_error(result: dict[str, Any]) -> str | None:
    """Failure text for an episode the agent side did not finish normally, or None."""
    events = result.get("events") or []
    if result.get("stop_reason") == "max_turns" and (any(event.get("type") == "error" for event in events) or result.get("returncode") not in TERMINATED):
        result["stop_reason"] = "harness_error"
    if result.get("stop_reason") == "harness_error":
        return result.get("error") or f"OpenCode exited {result.get('returncode')}"
    if result.get("stop_reason") == "timeout" and not events:
        return "OpenCode startup timeout: no model events were emitted"
    return None


def _tool_spec_from_dict(values: dict[str, Any]):
    from ..models.tool import ToolSpec

    return ToolSpec.model_validate(
        {
            "name": values["name"],
            "description": values.get("description", ""),
            "input_schema": values.get("input_schema") or {"type": "object", "properties": {}},
        }
    )


def run_profile(profile: dict[str, Any]) -> Path:
    from agent_runner import Runner

    from ..config import VisionToolConfig
    from ..models.content import TextBlock, ToolResultBlock, ToolUseBlock
    from ..models.message import Message
    from ..models.tool import ToolEndpoint
    from ..models.trace import ToolDispatch, TraceMessage, TraceStart
    from ..trace.writer import TraceWriter
    from .deadline import TaskDeadline
    from .dispatcher import ToolDispatcher
    from .opencode_runner import _MCPServer, _result_text, _write_opencode_messages
    from .sandbox_dispatcher import SandboxToolDispatcher
    from .sandbox_tools import get_sandbox_tools

    task = profile["task"]
    spec = profile["runner"]
    vision_values = profile.get("vision_tool") or {}
    user_agent_values = task.get("user_agent") or {}
    sandbox_url = profile.get("sandbox_url", SANDBOX_SERVER_URL)
    _wait_for_sandbox_server(sandbox_url)
    vision_url = (vision_values.get("base_url") or "").rstrip("/")
    if vision_url:
        _wait_for_vision_endpoint(vision_url)

    trace_id = profile["trace_id"]
    trace_path = Path(profile["trace_path"])
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(spec["workdir"])
    workspace.mkdir(parents=True, exist_ok=True)
    task_deadline = TaskDeadline.start(float(profile["timeout_seconds"]))

    endpoint_map = {name: ToolEndpoint.model_validate({**values, "tool_name": name}) for name, values in task.get("endpoint_map", {}).items()}
    vision_tool_cfg = VisionToolConfig.model_construct(
        enabled=bool(vision_values.get("enabled")),
        model_id=vision_values.get("model_id"),
        base_url=vision_values.get("base_url") or "http://127.0.0.1:18003/v1",
        api_key=vision_values.get("api_key"),
        reasoning_effort=vision_values.get("reasoning_effort"),
        request_timeout_seconds=float(vision_values.get("request_timeout_seconds", 60.0)),
        retry_attempts=int(vision_values.get("retry_attempts", 3)),
        max_output_tokens=int(vision_values.get("max_output_tokens", 4096)),
        max_image_bytes=int(vision_values.get("max_image_bytes", 20 * 1024 * 1024)),
    )
    dispatcher = SandboxToolDispatcher(ToolDispatcher(endpoint_map), sandbox_url=sandbox_url, task_deadline=task_deadline, vision_tool_cfg=vision_tool_cfg)
    services_manager = _start_pod_services(profile)
    _emit_progress("services_started", count=len(profile.get("services") or []))
    dispatch_records: list[tuple[str, dict[str, Any], ToolResultBlock, Any]] = []
    dispatch_lock = threading.Lock()
    task_text = task["task_text"]

    async def invoke(canonical_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        normalized_arguments = {key: value for key, value in tool_input.items() if value is not None}
        tool_use = ToolUseBlock(id=str(uuid4()), name=canonical_name, input=normalized_arguments)
        requested_path = normalized_arguments.get("file_path") or normalized_arguments.get("path")
        if canonical_name == "Write" and isinstance(requested_path, str) and Path(requested_path).name == MARKER:
            content = str(normalized_arguments.get("content") or "")
            (workspace / MARKER).write_text(content, encoding="utf-8")
            result = ToolResultBlock(tool_use_id=tool_use.id, content=[TextBlock(text="Completion marker written.")])
            dispatch = ToolDispatch(trace_id=trace_id, tool_use_id=tool_use.id, tool_name=canonical_name, endpoint_url="opencode://completion-marker", request_body=normalized_arguments, response_status=200, response_body={"written": MARKER})
            with dispatch_lock:
                dispatch_records.append((canonical_name, tool_use.input, result, dispatch))
            return {"result": _result_text(result), "is_error": False}

        def dispatch_one():
            with dispatch_lock:
                dispatched = dispatcher.dispatch(tool_use, trace_id)
                result, dispatch = dispatched[0], dispatched[1]
                dispatch_records.append((canonical_name, tool_use.input, result, dispatch))
                return result, dispatch

        result, _dispatch = await asyncio.to_thread(dispatch_one)
        return {"result": _result_text(result), "is_error": result.is_error}

    allow_questions = bool(user_agent_values.get("enabled"))
    sandbox_tools = get_sandbox_tools(enable_browser=False, enable_media=False, enable_describe_image=bool(vision_values.get("enabled")))
    normal_sandbox = [(tool, tool.name) for tool in sandbox_tools if tool.name != "DescribeImage"]
    vision_tools = [(tool.model_copy(update={"name": "describe_image"}), "DescribeImage") for tool in sandbox_tools if tool.name == "DescribeImage"]
    task_tools = [(_tool_spec_from_dict(values), values.get("canonical_name", values.get("name", "task_tool"))) for values in task.get("tools", [])]
    mcp_timeout_ms = max(30_000, int(task_deadline.remaining() * 1000))
    servers = [_MCPServer("sandbox", normal_sandbox, invoke, timeout_ms=mcp_timeout_ms), _MCPServer("task", task_tools, invoke, timeout_ms=mcp_timeout_ms)]
    if vision_tools:
        servers.append(_MCPServer("vision", vision_tools, invoke, timeout_ms=mcp_timeout_ms))
    for server in servers:
        server.start()
    _emit_progress("mcp_servers_started", ports={server.name: server.port for server in servers})

    config_path = Path(spec["config"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({**profile["opencode_config"], "mcp": {server.name: server.config for server in servers}}, indent=2), encoding="utf-8")
    (workspace / "EVALUATION.md").write_text(task["instructions"].rstrip() + "\n", encoding="utf-8")

    user_agent = _build_user_agent(user_agent_values)
    user_agent_done = False
    loop_error: str | None = None
    result: dict[str, Any] | None = None

    def user_followup(events: list[dict[str, Any]]) -> str | None:
        nonlocal user_agent_done
        if user_agent is None:
            return None
        response = user_agent.generate_response(persona=user_agent_values.get("persona", ""), conversation_messages=_conversation_messages(task_text, events))
        if response is None:
            user_agent_done = True
            return None
        return f"[user_agent]\n{response}"

    started = time.monotonic()
    try:
        spec["timeout"] = max(1, int(task_deadline.remaining()))
        result = Runner(spec).run(followup=user_followup if allow_questions else None, max_followups=int(user_agent_values.get("max_rounds", 0)) if allow_questions else 0)
        loop_error = _loop_error(result)
        if loop_error:
            _emit_progress("opencode_failed", error=loop_error, returncode=result.get("returncode"))
    except Exception as error:
        loop_error = f"{type(error).__name__}: {error}"
        _emit_progress("opencode_failed", error=loop_error)
    finally:
        for server in reversed(servers):
            server.stop()
        dispatcher.close()

    turns = input_tokens = output_tokens = 0
    with TraceWriter(trace_path) as writer:
        writer.write_event(TraceStart(trace_id=trace_id, task_id=task["task_id"], model=profile["model_id"], **(profile.get("trace_provenance") or {})))
        writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="user", content=[TextBlock(text=task_text)])))
        if result is not None:
            turns, input_tokens, output_tokens = _write_opencode_messages(writer, trace_id, result.get("events") or [], dispatch_records)
        _write_audit_snapshots(writer, trace_id, profile)
    if services_manager is not None:
        try:
            services_manager.__exit__(None, None, None)
        except Exception:
            pass
    wall = time.monotonic() - started
    stop_reason = result.get("stop_reason") if result is not None else "harness_error"
    payload = {
        "status": "completed" if loop_error is None else ("timeout" if stop_reason == "timeout" else "error"),
        "stop_reason": stop_reason,
        "error": loop_error,
        "opencode_returncode": result.get("returncode") if result is not None else None,
        "opencode_session_id": result.get("session_id") if result is not None else None,
        "trace_path": str(trace_path),
        "artifact_dir": spec["out"],
        "workspace_dir": str(workspace),
        "wall_time_s": round(wall, 2),
        "turns": turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "conversation_followups_used": result.get("followups", 0) if result is not None else 0,
        "user_agent_done": user_agent_done,
        "completion_nudges_used": result.get("nudges", 0) if result is not None else 0,
        "completed": bool(result.get("completed")) if result is not None else False,
    }
    _write_result(payload)
    _emit_progress("finished", status=payload["status"], wall_time_s=payload["wall_time_s"])
    return trace_path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    profile_path = Path(argv[0]) if argv else Path(os.environ.get("CLAWEVAL_POD_PROFILE", str(POD_AGENT_DIR / "profile.json")))
    _emit_progress("entry", profile=str(profile_path))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    try:
        run_profile(profile)
        return 0
    except Exception as error:
        _write_result({"status": "error", "stop_reason": "harness_error", "error": f"{type(error).__name__}: {error}", "trace_path": profile.get("trace_path")})
        _emit_progress("entry_failed", error=f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
