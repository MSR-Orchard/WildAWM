"""JSONL trace reader with type-based partitioning."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..models.trace import (
    AuditSnapshot,
    CompactEvent,
    GradingResult,
    MediaLoad,
    ToolDispatch,
    TraceEnd,
    TraceMessage,
    TraceStart,
)

_EVENT_MAP = {
    "trace_start": TraceStart,
    "message": TraceMessage,
    "tool_dispatch": ToolDispatch,
    "audit_snapshot": AuditSnapshot,
    "media_load": MediaLoad,
    "compact": CompactEvent,
    "trace_end": TraceEnd,
    "grading_result": GradingResult,
}


def opencode_startup_error(path: str | Path) -> str | None:
    """Reject historical empty OpenCode timeouts even when a grader already assigned a score."""
    path = Path(path)
    if not (path.parent / "opencode").is_dir():
        return None
    try:
        with path.open() as stream:
            start = json.loads(next(stream))
        result_path = path.parent / "opencode" / f"{start['task_id']}-{start.get('trial', 0)}-{start['trace_id'][:8]}" / "result.json"
        result = json.loads(result_path.read_text())
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None
    if isinstance(result, dict) and result.get("stop_reason") == "timeout" and result.get("events") == []:
        return "OpenCode startup timeout: no model events were emitted"
    return None


def read_events(path: str | Path) -> Iterator[TraceStart | TraceMessage | ToolDispatch | AuditSnapshot | MediaLoad | CompactEvent | TraceEnd | GradingResult]:
    """Parse each JSONL line by its ``type`` discriminator field."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            event_type = raw.get("type")
            cls = _EVENT_MAP.get(event_type)
            if cls is None:
                raise ValueError(f"Unknown trace event type: {event_type!r}")
            yield cls.model_validate(raw)


def load_trace(
    path: str | Path,
) -> tuple[TraceStart, list[TraceMessage], list[ToolDispatch], list[MediaLoad], TraceEnd | None, dict[str, dict]]:
    """Load a full trace file and partition by event type.

    Returns (start, messages, dispatches, media_events, end, audit_data).
    audit_data is keyed by service_name from AuditSnapshot events.
    GradingResult events are silently skipped (they are post-hoc additions).
    """
    start: TraceStart | None = None
    messages: list[TraceMessage] = []
    dispatches: list[ToolDispatch] = []
    media_events: list[MediaLoad] = []
    end: TraceEnd | None = None
    audit_data: dict[str, dict] = {}

    for event in read_events(path):
        match event:
            case TraceStart():
                start = event
            case TraceMessage():
                messages.append(event)
            case ToolDispatch():
                dispatches.append(event)
            case AuditSnapshot():
                audit_data[event.service_name] = event.audit_data
            case MediaLoad():
                media_events.append(event)
            case TraceEnd():
                end = event
            case GradingResult():
                pass  # post-hoc grading data; not needed for re-grading

    if start is None:
        raise ValueError(f"No TraceStart event found in {path}")
    return start, messages, dispatches, media_events, end, audit_data
