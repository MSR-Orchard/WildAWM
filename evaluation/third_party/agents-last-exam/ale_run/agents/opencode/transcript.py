from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ale_run.base_interface import (
    ContentPart,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)


def _part(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("part")
    return value if isinstance(value, dict) else {}


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def parse_events_into(work_dir: Path, builder: TrajectoryBuilder) -> None:
    events_path = work_dir / "events.jsonl"
    if not events_path.is_file():
        builder.add_step(source="system", message=f"opencode: no events at {events_path}", extra={"reason": "no_events"})
        return

    events: list[dict[str, Any]] = []
    parse_errors = 0
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(value, dict):
            events.append(value)

    message_steps: dict[str, Any] = {}
    pending_text: dict[str, list[str]] = {}
    pending_reasoning: dict[str, list[str]] = {}

    def agent_step(message_id: str):
        step = message_steps.get(message_id)
        if step is None:
            step = builder.add_step(source="agent")
            message_steps[message_id] = step
        return step

    for event in events:
        part = _part(event)
        part_type = part.get("type")
        message_id = str(part.get("messageID") or part.get("messageId") or f"event-{len(message_steps)}")
        if part_type == "reasoning":
            text = part.get("text")
            if isinstance(text, str) and text:
                pending_reasoning.setdefault(message_id, []).append(text)
            continue
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                pending_text.setdefault(message_id, []).append(text)
            continue
        if part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if state.get("status") not in {"completed", "error"}:
                continue
            call_id = str(part.get("callID") or part.get("id") or f"call-{len(builder.trajectory.steps)}")
            arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
            step = agent_step(message_id)
            if pending_text.get(message_id):
                step.message = "".join(pending_text.pop(message_id)).strip() or None
            if pending_reasoning.get(message_id):
                step.reasoning = "".join(pending_reasoning.pop(message_id)).strip() or None
            step.tool_calls.append(ToolCall(id=call_id, name=str(part.get("tool") or "unknown"), arguments=arguments))
            output = state.get("output") if state.get("status") == "completed" else state.get("error")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            is_error = state.get("status") == "error" or text.lstrip().startswith("### Error")
            builder.add_step(
                source="environment",
                observation=Observation(results=[ToolResult(tool_call_id=call_id, content=[ContentPart(type="text", text=text)], is_error=is_error)]),
                extra={"tool": str(part.get("tool") or "unknown")},
            )
            continue
        if part_type == "step-finish" or event.get("type") == "step_finish":
            step = agent_step(message_id)
            if pending_text.get(message_id):
                step.message = "".join(pending_text.pop(message_id)).strip() or None
            if pending_reasoning.get(message_id):
                step.reasoning = "".join(pending_reasoning.pop(message_id)).strip() or None
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            step.metrics = StepMetrics(
                input_tokens=_integer(tokens.get("input")),
                output_tokens=(_integer(tokens.get("output")) or 0) + (_integer(tokens.get("reasoning")) or 0),
                cache_read_tokens=_integer(cache.get("read")),
                cache_creation_tokens=_integer(cache.get("write")),
                cost_usd=float(part["cost"]) if isinstance(part.get("cost"), (int, float)) and not isinstance(part.get("cost"), bool) else None,
            )
            step.extra["stop_reason"] = str(part.get("reason") or "stop")

    for message_id in set(pending_text) | set(pending_reasoning):
        step = agent_step(message_id)
        if pending_text.get(message_id):
            step.message = "".join(pending_text[message_id]).strip() or None
        if pending_reasoning.get(message_id):
            step.reasoning = "".join(pending_reasoning[message_id]).strip() or None

    result_path = work_dir / "result.json"
    result: dict[str, Any] = {}
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            result = value
    except (OSError, ValueError):
        pass
    if isinstance(result.get("input_tokens"), int) or isinstance(result.get("output_tokens"), int):
        builder.override_final_metrics(
            total_input_tokens=_integer(result.get("input_tokens")),
            total_output_tokens=_integer(result.get("output_tokens")),
        )
    builder.trajectory.extra["opencode"] = {
        "version": "1.17.7",
        "events_path": str(events_path),
        "result_path": str(result_path),
        "turns": result.get("turns"),
        "stop_reason": result.get("stop_reason"),
        "completion_marker": result.get("completion_summary") is not None,
        "provider_stream_retries_used": result.get("stream_retries"),
        "parse_errors": parse_errors,
    }
