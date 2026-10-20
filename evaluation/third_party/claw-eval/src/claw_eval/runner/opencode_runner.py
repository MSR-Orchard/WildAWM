"""MCP tool servers and OpenCode event-to-trace conversion for the pod-side agent."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any
from uuid import uuid4

import uvicorn
from mcp.server.fastmcp import FastMCP

from ..models.content import TextBlock, ToolResultBlock, ToolUseBlock
from ..models.message import Message
from ..models.trace import TokenUsage, TraceMessage, ToolDispatch
from ..models.tool import ToolSpec
from ..trace.writer import TraceWriter


class _MCPServer:
    def __init__(self, name: str, tools: list[tuple[ToolSpec, str]], invoke, *, timeout_ms: int) -> None:
        if timeout_ms <= 0:
            raise ValueError("MCP timeout must be positive")
        self.name = name
        self.port = _available_port()
        self.timeout_ms = timeout_ms
        self.mcp = FastMCP(name, host="127.0.0.1", port=self.port, stateless_http=True, json_response=True)
        for spec, canonical_name in tools:
            function = _dynamic_function(spec, canonical_name, invoke)
            self.mcp.add_tool(function, name=spec.name, description=spec.description)
            self.mcp._tool_manager._tools[spec.name].parameters = spec.input_schema
        self.server = uvicorn.Server(uvicorn.Config(self.mcp.streamable_http_app(), host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, name=f"claweval-opencode-mcp-{name}", daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.05)
        raise RuntimeError(f"OpenCode MCP server {self.name} did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def config(self) -> dict[str, Any]:
        return {"type": "remote", "url": f"http://127.0.0.1:{self.port}/mcp", "enabled": True, "timeout": self.timeout_ms}


def _available_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dynamic_function(spec: ToolSpec, canonical_name: str, invoke):
    properties = spec.input_schema.get("properties") if isinstance(spec.input_schema, dict) else {}
    properties = properties if isinstance(properties, dict) else {}
    required = set(spec.input_schema.get("required") or []) if isinstance(spec.input_schema, dict) else set()
    names = list(properties)
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
        raise ValueError(f"MCP tool {spec.name} has a non-Python parameter name")
    ordered = [name for name in names if name in required] + [name for name in names if name not in required]
    signature = ", ".join([*ordered[:len(required)], *(f"{name}=None" for name in ordered[len(required):])])
    arguments = ", ".join(f"{name!r}: {name}" for name in ordered)
    source = f"async def generated({signature}):\n    return await _invoke({canonical_name!r}, {{{arguments}}})"
    namespace = {"_invoke": invoke}
    exec(source, namespace)
    function = namespace["generated"]
    function.__name__ = spec.name
    function.__qualname__ = spec.name
    function.__doc__ = spec.description
    return function


def _result_text(result: ToolResultBlock) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextBlock))


def _event_part(event: dict[str, Any]) -> dict[str, Any]:
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _completed_tool_event(event: dict[str, Any]) -> bool:
    part = _event_part(event)
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    return part.get("type") == "tool" and state.get("status") in {"completed", "error"}


def _canonical_wire_name(value: str) -> str:
    if value == "vision_describe_image":
        return "DescribeImage"
    for prefix in ("sandbox_", "task_"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    sandbox_names = {name.casefold(): name for name in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "BrowserScreenshot", "ReadMedia", "DescribeImage", "Download")}
    return sandbox_names.get(value.casefold(), value)


def _write_opencode_messages(writer: TraceWriter, trace_id: str, events: list[dict[str, Any]], dispatch_records: list[tuple[str, dict[str, Any], ToolResultBlock, ToolDispatch]]) -> tuple[int, int, int]:
    pending: list[TextBlock | ToolUseBlock] = []
    turn_count = 0
    input_tokens = 0
    output_tokens = 0
    dispatch_index = 0
    for event in events:
        part = _event_part(event)
        part_type = part.get("type")
        if part_type == "user" and isinstance(part.get("text"), str):
            if pending:
                writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="assistant", content=pending)))
                pending = []
            writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="user", content=[TextBlock(text=part["text"])])))
            continue
        if part_type in {"text", "reasoning"} and isinstance(part.get("text"), str) and part["text"]:
            pending.append(TextBlock(text=part["text"]))
            continue
        if _completed_tool_event(event):
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            wire_name = str(part.get("tool") or "unknown")
            canonical = _canonical_wire_name(wire_name)
            call_id = str(part.get("callID") or part.get("id") or uuid4())
            arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
            pending.append(ToolUseBlock(id=call_id, name=canonical, input=arguments))
            writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="assistant", content=pending)))
            pending = []
            record = None
            while dispatch_index < len(dispatch_records):
                candidate = dispatch_records[dispatch_index]
                dispatch_index += 1
                if candidate[0] == canonical:
                    record = candidate
                    break
            if record is not None:
                _, _, tool_result, dispatch = record
                normalized_result = tool_result.model_copy(update={"tool_use_id": call_id})
                normalized_dispatch = dispatch.model_copy(update={"tool_use_id": call_id})
            else:
                output = state.get("output") if state.get("status") == "completed" else state.get("error")
                normalized_result = ToolResultBlock(tool_use_id=call_id, content=[TextBlock(text=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))], is_error=state.get("status") == "error")
                normalized_dispatch = ToolDispatch(trace_id=trace_id, tool_use_id=call_id, tool_name=canonical, endpoint_url="opencode://unmatched-tool", request_body=arguments, response_status=500 if normalized_result.is_error else 200, response_body=output)
            writer.write_event(normalized_dispatch)
            writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="user", content=[normalized_result])))
            continue
        if part_type == "step-finish" or event.get("type") == "step_finish":
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            step_input = int(tokens.get("input") or 0)
            step_output = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
            input_tokens += step_input
            output_tokens += step_output
            turn_count += 1
            if pending:
                writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="assistant", content=pending), usage=TokenUsage(input_tokens=step_input, output_tokens=step_output)))
                pending = []
    if pending:
        writer.write_event(TraceMessage(trace_id=trace_id, message=Message(role="assistant", content=pending)))
    return turn_count, input_tokens, output_tokens
