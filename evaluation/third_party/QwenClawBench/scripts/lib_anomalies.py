"""
Rule-based anomaly detection for QwenClawBench task runs.

Each anomaly represents an infrastructure or execution problem that makes a
run's score unreliable (e.g. OOM kill, empty transcript, grading script error).
Anomaly data is written into grading.json alongside the score so that downstream
score computation can filter or flag affected runs.
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Anomaly severity
# ---------------------------------------------------------------------------
ERROR = "error"       # score-impacting: run result is unreliable / incomplete
WARNING = "warning"   # transient: anomaly detected but score is still valid

# ---------------------------------------------------------------------------
# Transcript text extraction
# ---------------------------------------------------------------------------

def _get_transcript_text(transcript: List[Dict[str, Any]]) -> str:
    """Flatten a transcript list to a single string for keyword searches.

    We only look at text-like fields to avoid false positives from binary
    content or argument objects.  This is intentionally broad — false
    positives are acceptable for a heuristic.
    """
    parts: List[str] = []
    for entry in transcript:
        entry_type = entry.get("type", "")
        if entry_type == "message":
            msg = entry.get("message", {})
            for item in msg.get("content", []):
                if isinstance(item, dict):
                    text = item.get("text", "") or item.get("content", "")
                    if text:
                        parts.append(str(text))
        elif entry_type in ("toolResult", "toolCall"):
            content = entry.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "") or item.get("content", "")
                        if text:
                            parts.append(str(text))
            elif isinstance(content, str):
                parts.append(content)
    return "\n".join(parts)


def _scan_text_for_keywords(text: str, keywords: List[str]) -> bool:
    """Return True if any keyword appears in text (case-insensitive)."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _extract_llm_api_errors(transcript: List[Dict[str, Any]]) -> str:
    """Extract only provider-level error fields from assistant and prompt-error events, excluding task content that merely discusses errors."""
    parts: List[str] = []
    for entry in transcript:
        if entry.get("type") == "message":
            msg = entry.get("message", {})
            if msg.get("role") != "assistant" or msg.get("stopReason") not in {"error", "aborted"}:
                continue
            err = msg.get("errorMessage", "")
            if err:
                parts.append(str(err))
        elif entry.get("type") == "custom" and entry.get("customType") == "openclaw:prompt-error":
            err = entry.get("data", {}).get("error", "")
            if err:
                parts.append(str(err))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Individual rule checks
# The check functions receive (execution_result, grade_notes, transcript_text,
# stderr_text) and return either None (no anomaly) or a description string.
# ---------------------------------------------------------------------------

def _check_execution_exception(exec_r, notes, transcript_text, stderr) -> str | None:
    timed_out = bool(exec_r.get("timed_out"))
    if exec_r.get("infrastructure_failed") or (exec_r.get("exit_code") == -1 and not timed_out):
        # Try to extract a short cause from stderr
        cause = ""
        if stderr:
            # Take the last non-empty line (usually the final error message)
            lines = [l.strip() for l in stderr.splitlines() if l.strip()]
            if lines:
                cause = f": {lines[-1][:200]}"
        return f"Container/subprocess infrastructure execution failed (exit_code={exec_r.get('exit_code')}){cause}"
    return None


def _check_oom_kill(exec_r, notes, transcript_text, stderr) -> str | None:
    if exec_r.get("exit_code") == 137:
        return "Process killed by SIGKILL (exit_code=137), likely OOM or manual kill"
    return None


def _check_exit_code_nonzero(exec_r, notes, transcript_text, stderr) -> str | None:
    code = exec_r.get("exit_code")
    timed_out = exec_r.get("timed_out", False)
    if code not in (0, -1, 137, None) and not timed_out:
        return f"Non-zero exit code (exit_code={code}) without timeout"
    return None


def _check_task_timed_out(exec_r, notes, transcript_text, stderr) -> str | None:
    if exec_r.get("timed_out"):
        return "Task execution timed out — agent may have been mid-work when killed"
    return None


def _check_empty_transcript(exec_r, notes, transcript_text, stderr) -> str | None:
    tlen = exec_r.get("transcript_length", 0)
    status = exec_r.get("status", "")
    if tlen == 0 and status != "timeout":
        return "Empty transcript — agent never ran or transcript collection failed"
    return None


def _check_short_transcript(exec_r, notes, transcript_text, stderr) -> str | None:
    tlen = exec_r.get("transcript_length", 0)
    if 0 < tlen < 5:
        return f"Very short transcript ({tlen} entries) — agent may have crashed immediately after starting"
    return None


def _check_quick_exit(exec_r, notes, transcript_text, stderr) -> str | None:
    elapsed = exec_r.get("execution_time", float("inf"))
    status = exec_r.get("status", "")
    if elapsed < 10.0 and status == "error":
        return f"Container exited very quickly ({elapsed:.1f}s) with error — possible missing assets or Docker startup failure"
    return None


def _check_container_cleanup_failed(exec_r, notes, transcript_text, stderr) -> str | None:
    if exec_r.get("cleanup_failed") or "Container cleanup error:" in (stderr or ""):
        return "Container cleanup failed after execution; score remains usable but sandbox recovery must be verified"
    return None


def _check_workspace_collection_failed(exec_r, notes, transcript_text, stderr) -> str | None:
    if exec_r.get("workspace_collection_failed"):
        return "Workspace artifacts were not collected; rule-based grading input is incomplete"
    return None


def _check_unresumed_session_yield(exec_r, notes, transcript_text, stderr) -> str | None:
    transcript = exec_r.get("transcript", [])
    for entry in reversed(transcript):
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") != "assistant":
            continue
        if message.get("stopReason") != "toolUse":
            return None
        for item in message.get("content", []):
            if (
                isinstance(item, dict)
                and item.get("type") in {"toolCall", "tool_use"}
                and item.get("name") == "sessions_yield"
            ):
                return "Run yielded for a future event, but the single-turn benchmark ended before the session resumed"
        return None
    return None


def _check_unfinished_tool_turn(exec_r, notes, transcript_text, stderr) -> str | None:
    if exec_r.get("opencode_terminal_reason") == "length" or _clean_opencode_stop(exec_r):
        return None
    transcript = exec_r.get("transcript", [])
    messages = [entry.get("message", {}) for entry in transcript if entry.get("type") == "message"]
    if not messages or messages[-1].get("role") != "toolResult":
        return None
    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    if assistant_messages and assistant_messages[-1].get("stopReason") == "toolUse":
        return "Run ended after a tool result without a final assistant response"
    return None


def _check_duplicate_tool_events(exec_r, notes, transcript_text, stderr) -> str | None:
    seen_call_ids: set[str] = set()
    seen_result_ids: set[str] = set()
    duplicate_call_ids: set[str] = set()
    duplicate_result_ids: set[str] = set()
    for entry in exec_r.get("transcript", []):
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") == "assistant":
            for item in message.get("content", []):
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                call_id = item.get("id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                if call_id in seen_call_ids:
                    duplicate_call_ids.add(call_id)
                seen_call_ids.add(call_id)
        elif message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                continue
            if call_id in seen_result_ids:
                duplicate_result_ids.add(call_id)
            seen_result_ids.add(call_id)
    if duplicate_call_ids or duplicate_result_ids:
        return f"Transcript contains replayed tool events ({len(duplicate_call_ids)} duplicate call IDs, {len(duplicate_result_ids)} duplicate result IDs)"
    return None


def _check_missing_parent_event(exec_r, notes, transcript_text, stderr) -> str | None:
    seen_event_ids: set[str] = set()
    missing_parent_ids: set[str] = set()
    for entry in exec_r.get("transcript", []):
        parent_id = entry.get("parentId")
        if isinstance(parent_id, str) and parent_id and parent_id not in seen_event_ids:
            missing_parent_ids.add(parent_id)
        event_id = entry.get("id")
        if isinstance(event_id, str) and event_id:
            seen_event_ids.add(event_id)
    if missing_parent_ids:
        return f"Transcript contains {len(missing_parent_ids)} event parent reference(s) whose parent is missing or out of order"
    return None


def _check_grading_script_error(exec_r, notes, transcript_text, stderr) -> str | None:
    if notes and "Grading failed:" in notes:
        # Include the first 150 chars of the error for context
        short = notes[:150].replace("\n", " ")
        return f"Grading script raised an exception: {short}"
    return None


def _check_grading_missing_function(exec_r, notes, transcript_text, stderr) -> str | None:
    if notes and (
        "grading function missing" in notes.lower()
        or "No automated grading code" in notes
    ):
        return "Grading function not found in task definition — score is forced to 0.0"
    return None


def _check_api_rate_limit(exec_r, notes, transcript_text, stderr) -> str | None:
    # Only inspect structured LLM API error messages. OpenClaw's CLI stderr may contain streamed tool output, so scanning it causes false positives when task fixtures discuss rate limits.
    transcript = exec_r.get("transcript", [])
    combined = _extract_llm_api_errors(transcript).lower()
    if _scan_text_for_keywords(combined, [
        "429", "rate limit", "rate_limit", "too many requests", "ratelimit",
        "resource_exhausted", "resource has been exhausted",
    ]):
        return "API rate limit detected in LLM API error response"
    return None


def _check_api_server_error(exec_r, notes, transcript_text, stderr) -> str | None:
    # Same targeted approach: OpenClaw's CLI stderr also carries streamed tool output and is not a provider-error-only channel.
    transcript = exec_r.get("transcript", [])
    combined = _extract_llm_api_errors(transcript)
    if _scan_text_for_keywords(combined, [
        "502", "503", "500",
        "service unavailable", "bad gateway", "internal server error",
        "internal error has occurred",  # "An internal error has occurred, please try again later."
        "backend buffer overflow",       # backend OOM / buffer overflow
        "requesttimeout",                # covers "RequestTimeOut" (case-insensitive)
        "unavailable",                   # covers "UNAVAILABLE: io exception" and variants
    ]):
        return "API server error (5xx or server-side failure) detected in LLM API error response"
    return None


def _check_zero_token_response(exec_r, notes, transcript_text, stderr) -> str | None:
    """Detect cases where the transcript exists but the model generated 0 tokens.

    This indicates a silent API failure: the agent sent the prompt, got back an
    empty response, and the task ended without any model inference actually
    happening.  exit_code is 0 so none of the exit-code rules fire.
    """
    tlen = exec_r.get("transcript_length", 0)
    if tlen == 0:
        return None  # already covered by EMPTY_TRANSCRIPT

    # Collect assistant messages for content check
    transcript = exec_r.get("transcript", [])
    assistant_msgs = [
        e.get("message", {})
        for e in transcript
        if e.get("type") == "message" and e.get("message", {}).get("role") == "assistant"
    ]

    # If any assistant message has actual content, the model DID respond.
    # Some models simply don't log token counts, so total_tokens=0 alone is
    # not sufficient — we must confirm content is also absent.
    if any(msg.get("content") for msg in assistant_msgs):
        return None

    # All assistant messages have empty content; now confirm via token count.
    # total_tokens == -1 means the field was not populated (unknown), skip.
    usage = exec_r.get("usage", {})
    total_tokens = usage.get("total_tokens", -1)
    if total_tokens == 0:
        return (
            "Transcript exists but all assistant messages are empty with 0 tokens — "
            "model API returned empty responses (silent failure, possibly "
            "mis-configured model endpoint or auth error)"
        )

    # Also catch via per-message usage when execution_result["usage"] is absent
    if assistant_msgs and all(
        msg.get("usage", {}).get("totalTokens", -1) == 0
        for msg in assistant_msgs
    ):
        return (
            "All assistant messages have empty content and 0 tokens — "
            "model API returned empty responses (silent failure)"
        )

    return None


def _check_terminal_api_failure(exec_r, notes, transcript_text, stderr) -> str | None:
    """Detect a terminal assistant error, abort, prompt-error event, or silent empty stop after earlier task progress."""
    if exec_r.get("opencode_terminal_reason") == "length" or _clean_opencode_stop(exec_r):
        return None
    transcript = exec_r.get("transcript", [])
    if not transcript:
        return None  # already covered by EMPTY_TRANSCRIPT

    has_trailing_cli_stop = False
    for entry in reversed(transcript):
        if entry.get("type") == "custom" and entry.get("customType") == "openclaw:prompt-error":
            error_msg = str(entry.get("data", {}).get("error", "unknown prompt error"))
            return f"Run ended with terminal OpenClaw prompt error — task never completed: {error_msg[:150]}"
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") != "assistant":
            continue
        if message.get("api") == "cli":
            has_trailing_cli_stop = has_trailing_cli_stop or message.get("stopReason") == "stop"
            continue
        stop_reason = message.get("stopReason")
        if stop_reason in {"error", "aborted"}:
            error_msg = str(message.get("errorMessage", "unknown API error"))
            return f"Run ended with terminal LLM failure (stopReason={stop_reason}) — task never completed: {error_msg[:150]}"
        if stop_reason == "length":
            return None
        if stop_reason == "toolUse" and has_trailing_cli_stop:
            break
        if stop_reason != "stop":
            return f"Run ended without a complete final assistant response (stopReason={stop_reason})"
        break

    assistant_msgs = [
        e.get("message", {})
        for e in transcript
        if e.get("type") == "message" and e.get("message", {}).get("role") == "assistant"
    ]
    if not assistant_msgs:
        return None

    last_msg = assistant_msgs[-1]
    last_text = "\n".join(
        str(item.get("text", ""))
        for item in last_msg.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()
    last_thinking = "\n".join(
        str(item.get("thinking", ""))
        for item in last_msg.get("content", [])
        if isinstance(item, dict) and item.get("type") == "thinking"
    )
    has_native_tool_call = any(
        isinstance(item, dict) and item.get("type") in {"toolCall", "tool_use"}
        for item in last_msg.get("content", [])
    )
    if (
        last_msg.get("stopReason") == "stop"
        and not last_text
        and not has_native_tool_call
        and "<tool_call>" in last_thinking
        and "<function=" in last_thinking
    ):
        return "Run ended with literal tool-call markup trapped in reasoning instead of a native tool call — task never completed"
    if last_msg.get("stopReason") == "stop" and last_text.startswith("Context overflow: prompt too large for the model."):
        return "Run ended with OpenClaw's synthetic context-overflow response — task never completed"
    # Catch silent empty stops: stopReason=stop but empty content + 0 tokens.
    if (
        not last_msg.get("content")
        and last_msg.get("stopReason") == "stop"
        and last_msg.get("usage", {}).get("totalTokens", -1) == 0
    ):
        return (
            "Run ended silently on final turn (content=[], stopReason=stop, tokens=0) — "
            "model produced no output after last tool results (silent task abandonment)"
        )

    return None


def _terminal_llm_api_error(transcript: list) -> str:
    for entry in reversed(transcript):
        if entry.get("type") == "custom" and entry.get("customType") == "openclaw:prompt-error":
            return _extract_llm_api_errors([entry])
        message = entry.get("message", {})
        if entry.get("type") == "message" and message.get("role") == "assistant" and message.get("api") != "cli":
            return _extract_llm_api_errors([entry])
    return ""


def _clean_opencode_stop(result: dict) -> bool:
    return bool(result.get("opencode_terminal_reason") == "stop" and result.get("status") == "success" and result.get("exit_code") == 0 and not any(result.get(key) for key in ("timed_out", "execution_failed", "infrastructure_failed", "workspace_collection_failed")) and not _terminal_llm_api_error(result.get("transcript", [])))


def execution_error_class(result: dict) -> str | None:
    """Classify recorded execution failures, never task text or rubric scores."""
    transcript = result.get("transcript", [])
    provider_error = _extract_llm_api_errors(transcript) if result.get("timed_out") else _terminal_llm_api_error(transcript)
    if result.get("infrastructure_failed") or result.get("workspace_collection_failed") or result.get("opencode_errors") or provider_error:
        return "infrastructure_error"
    if result.get("timed_out"):
        # An empty startup deadline is not evidence of model work consuming the budget.
        has_output = any(entry.get("type") == "message" and entry.get("message", {}).get("role") == "assistant" and entry["message"].get("content") for entry in result.get("transcript", []))
        return "agent_error" if has_output or result.get("model_output_seen") else "infrastructure_error"
    if result.get("execution_failed") or result.get("exit_code") not in (0, None):
        return "agent_error"
    return None


# ---------------------------------------------------------------------------
# Rule registry
# Each entry: (rule_id, severity, check_function)
# Rules are evaluated in order; all matching rules are reported.
# ---------------------------------------------------------------------------

_RULES: List[tuple] = [
    ("EXECUTION_EXCEPTION",      ERROR,   _check_execution_exception),
    ("EXIT_CODE_OOM",            ERROR,   _check_oom_kill),
    ("EXIT_CODE_NONZERO",        ERROR,   _check_exit_code_nonzero),
    ("TASK_TIMED_OUT",           ERROR,   _check_task_timed_out),
    ("EMPTY_TRANSCRIPT",         ERROR,   _check_empty_transcript),
    ("SHORT_TRANSCRIPT",         ERROR,   _check_short_transcript),
    ("QUICK_EXIT_SUSPICIOUS",    ERROR,   _check_quick_exit),
    ("WORKSPACE_COLLECTION_FAILED", ERROR, _check_workspace_collection_failed),
    ("UNRESUMED_SESSION_YIELD", ERROR, _check_unresumed_session_yield),
    ("UNFINISHED_TOOL_TURN",    ERROR,   _check_unfinished_tool_turn),
    ("DUPLICATE_TOOL_EVENTS",  ERROR,   _check_duplicate_tool_events),
    ("MISSING_PARENT_EVENT",   ERROR,   _check_missing_parent_event),
    ("CONTAINER_CLEANUP_FAILED", WARNING, _check_container_cleanup_failed),
    ("GRADING_SCRIPT_ERROR",     ERROR,   _check_grading_script_error),
    ("GRADING_MISSING_FUNCTION", ERROR,   _check_grading_missing_function),
    ("API_RATE_LIMIT",           WARNING, _check_api_rate_limit),
    ("API_SERVER_ERROR",         WARNING, _check_api_server_error),
    ("ZERO_TOKEN_RESPONSE",      ERROR,   _check_zero_token_response),
    ("TERMINAL_API_FAILURE",     ERROR,   _check_terminal_api_failure),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_anomalies(
    execution_result: Dict[str, Any],
    grade_notes: str,
) -> Dict[str, Any]:
    """Detect anomalies in a single task run.

    Parameters
    ----------
    execution_result:
        The dict returned by ``execute_task_in_docker()`` (or reconstructed
        from grading.json for retroactive analysis).  Expected keys:
        ``exit_code``, ``timed_out``, ``status``, ``execution_time``,
        ``transcript_length``, ``transcript`` (list), ``stderr``.
    grade_notes:
        The ``notes`` string from the GradeResult (or grading.json).

    Returns
    -------
    dict with keys:
        ``is_anomalous``  — True if any rule triggered.
        ``has_error``     — True if any error-severity (score-impacting) rule triggered.
        ``items``         — List of {id, severity, description} dicts.
    """
    transcript = execution_result.get("transcript", [])
    transcript_text = _get_transcript_text(transcript)
    stderr = execution_result.get("stderr", "") or ""

    items: List[Dict[str, str]] = []
    for rule_id, severity, check_fn in _RULES:
        description = check_fn(execution_result, grade_notes, transcript_text, stderr)
        if description is not None:
            items.append({"id": rule_id, "severity": severity, "description": description})

    # Post-process: if API_RATE_LIMIT / API_SERVER_ERROR co-occurs with a fatal
    # error that produced zero useful output, upgrade those warnings to error so
    # callers can tell the API issue was fatal rather than a transient disturbance.
    _fatal_error_ids = {
        "EMPTY_TRANSCRIPT",
        "ZERO_TOKEN_RESPONSE",
        "TERMINAL_API_FAILURE",
        "EXECUTION_EXCEPTION",
        "EXIT_CODE_OOM",
    }
    _api_warning_ids = {"API_RATE_LIMIT", "API_SERVER_ERROR"}
    triggered_ids = {item["id"] for item in items}
    has_fatal_error = bool(triggered_ids & _fatal_error_ids)
    if has_fatal_error:
        for item in items:
            if item["id"] in _api_warning_ids:
                item["severity"] = ERROR
                item["description"] += " [upgraded to error: co-occurs with fatal execution failure]"

    has_error = any(item["severity"] == ERROR for item in items)
    return {
        "is_anomalous": len(items) > 0,
        "has_error": has_error,
        "items": items,
    }
