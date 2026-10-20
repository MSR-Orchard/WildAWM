"""OpenCode `run --format json` events as the OpenClaw-style transcript that QwenClawBench graders read."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def part(event: dict) -> dict:
    value = event.get("part")
    return value if isinstance(value, dict) else {}


def as_int(value) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def timestamp(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def session_id(events: list[dict]) -> str | None:
    for event in events:
        value = event.get("sessionID") or part(event).get("sessionID")
        if isinstance(value, str) and value:
            return value
    return None


def finish_reason(events: list[dict]) -> str | None:
    for event in reversed(events):
        if part(event).get("type") == "step-finish" or event.get("type") == "step_finish":
            reason = part(event).get("reason")
            return reason if isinstance(reason, str) else None
    return None


def to_transcript(*, prompt: str, events: list[dict], model: str, provider: str, api: str, reasoning_effort: str | None, cwd: str) -> list[dict]:
    start = timestamp(events[0].get("timestamp") if events else None)
    transcript = [
        {"type": "session", "version": 3, "id": session_id(events) or "opencode", "timestamp": start, "cwd": cwd, "harness": "opencode"},
        {"type": "model_change", "id": "opencode-model", "parentId": None, "timestamp": start, "provider": provider, "modelId": model},
        {"type": "thinking_level_change", "id": "opencode-reasoning", "parentId": "opencode-model", "timestamp": start, "thinkingLevel": reasoning_effort or "none"},
        {"type": "message", "id": "opencode-user", "parentId": "opencode-reasoning", "timestamp": start, "message": {"role": "user", "content": prompt}},
    ]
    current: list[dict] = []
    parent_id = "opencode-user"
    assistant_events: dict[str, dict] = {}
    step_index = 0
    for event in events:
        item = part(event)
        kind = item.get("type")
        if kind == "user" and isinstance(item.get("text"), str):
            user_id = f"opencode-user-{step_index}-{len(transcript)}"
            transcript.append({"type": "message", "id": user_id, "parentId": parent_id, "timestamp": timestamp(event.get("timestamp")), "message": {"role": "user", "content": item["text"]}})
            parent_id = user_id
        elif kind in {"reasoning", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                block = {"type": "thinking", "thinking": text} if kind == "reasoning" else {"type": "text", "text": text}
                message_id = str(item.get("messageID") or "")
                if message_id in assistant_events:
                    assistant_events[message_id]["message"]["content"].append(block)
                else:
                    current.append(block)
        elif kind == "tool":
            state = item.get("state") if isinstance(item.get("state"), dict) else {}
            if state.get("status") not in {"completed", "error"}:
                continue
            call_id = str(item.get("callID") or item.get("id") or f"opencode-tool-{step_index}")
            tool_name = str(item.get("tool") or "unknown")
            arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
            tool_call = {"type": "toolCall", "id": call_id, "name": tool_name, "arguments": arguments, "partialArgs": json.dumps(arguments, ensure_ascii=False)}
            message_id = str(item.get("messageID") or f"opencode-assistant-{step_index}")
            if message_id in assistant_events:
                if current:
                    assistant_events[message_id]["message"]["content"].extend(current)
                    current = []
                assistant_events[message_id]["message"]["content"].append(tool_call)
            else:
                current.append(tool_call)
                usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0, "cost": {"total": 0.0}}
                assistant = {"type": "message", "id": message_id, "parentId": parent_id, "timestamp": timestamp(event.get("timestamp")),
                             "message": {"role": "assistant", "content": current, "api": api, "provider": provider, "model": model, "usage": usage, "stopReason": "toolUse"}}
                transcript.append(assistant)
                assistant_events[message_id] = assistant
                parent_id = message_id
                current = []
            output = state.get("output") if state.get("status") == "completed" else state.get("error")
            result_id = f"{call_id}-result"
            transcript.append({"type": "message", "id": result_id, "parentId": parent_id, "timestamp": timestamp(event.get("timestamp")),
                               "message": {"role": "toolResult", "toolCallId": call_id, "toolName": tool_name,
                                           "content": [{"type": "text", "text": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)}],
                                           "isError": state.get("status") == "error"}})
            parent_id = result_id
        elif kind == "step-finish" or event.get("type") == "step_finish":
            tokens = item.get("tokens") if isinstance(item.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            assistant_id = str(item.get("messageID") or f"opencode-assistant-{step_index}")
            tokens_in, tokens_out = as_int(tokens.get("input")), as_int(tokens.get("output")) + as_int(tokens.get("reasoning"))
            usage = {"input": tokens_in, "output": tokens_out, "cacheRead": as_int(cache.get("read")), "cacheWrite": as_int(cache.get("write")),
                     "totalTokens": tokens_in + tokens_out, "cost": {"total": float(item.get("cost") or 0.0)}}
            if current:
                assistant = {"type": "message", "id": assistant_id, "parentId": parent_id, "timestamp": timestamp(event.get("timestamp")),
                             "message": {"role": "assistant", "content": current, "api": api, "provider": provider, "model": model, "usage": usage, "stopReason": str(item.get("reason") or "stop")}}
                transcript.append(assistant)
                assistant_events[assistant_id] = assistant
                parent_id = assistant_id
                current = []
            elif assistant_id in assistant_events:
                assistant_events[assistant_id]["message"]["usage"] = usage
            step_index += 1
    return transcript


def usage_totals(transcript: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "request_count": 0}
    for entry in transcript:
        message = entry.get("message", {}) if entry.get("type") == "message" else {}
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage", {})
        totals["request_count"] += 1
        totals["input_tokens"] += usage.get("input", 0)
        totals["output_tokens"] += usage.get("output", 0)
        totals["cache_read_tokens"] += usage.get("cacheRead", 0)
        totals["cache_write_tokens"] += usage.get("cacheWrite", 0)
        totals["total_tokens"] += usage.get("totalTokens", 0)
        totals["cost_usd"] += usage.get("cost", {}).get("total", 0.0)
    return totals
