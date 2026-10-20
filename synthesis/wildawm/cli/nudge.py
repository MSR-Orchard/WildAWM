"""Completion-probe prompts and loop for `awm agent run`."""

import subprocess
import uuid
import time
import re
from collections.abc import Callable
import inspect

from wildawm import const_config as cfg

TEST_COMPLETION_NUDGE = """You stopped at the end of a turn. Decide from the current session state whether the original task is fully complete.

Important:
- Do not restart, re-plan from scratch, or ask the user anything.
- Do not narrate future steps. If work remains, your next assistant response must call a real tool now.
- Do not switch to another task, day, bootstrap flow, memo, or onboarding workflow.
- This probe is not permission to repeat, replace, or improve work that is already complete. Inspect the existing artifacts and persisted state before any mutating tool call; if every requested outcome already exists, write the marker immediately without changing task state.
- Never replace the chosen entity, save an alternative, place an optional order or booking, or perform any action the original task did not request. If one concrete required action is genuinely missing, do only that missing action and preserve every already-correct result.
- If you need visual evidence and `/agent-config/describe_image_mcp.py` exists, save a screenshot first, e.g. `browser-use screenshot /tmp/page.png`, then run `python3 /agent-config/describe_image_mcp.py /tmp/page.png "focused question"`. Do not use a nonexistent `view_image` shell command.

When-and only when-the original task is fully complete, write a concise completion summary to this exact file, then stop:
{marker_path}

If any required action remains, do not create or modify that marker file. Continue the original task immediately with tool calls."""

INITIAL_COMPLETION_CONTRACT = """Completion protocol:
- Continue working until the original task is fully complete.
- Only after every required action is complete, use a real tool call to write a concise completion summary to this exact file:
{marker_path}
- Do not create or modify that file before completion.
- After writing the completion summary, give your final response and stop."""


_TOOL_PARSER_LEAK_RE = re.compile(r"</?router\b[^>]*>", re.IGNORECASE)


def _looks_like_tool_parser_leak(text: str) -> bool:
    """Detect leaked tool-routing markup: the tool parser failed and no tool call will follow."""
    if not text:
        return False
    n = len(_TOOL_PARSER_LEAK_RE.findall(text))
    return n >= 20 or (n >= 5 and len(text) > 5000)


def completion_probe_prompt(marker_path: str, task_text: str = "") -> str:
    rendered = TEST_COMPLETION_NUDGE.replace("{marker_path}", marker_path)
    if marker_path not in rendered:
        rendered += (
            "\n\nWhen the original task is fully complete, write the completion "
            f"summary to this exact file and then stop:\n{marker_path}\n"
            "Do not create or touch that file before completion."
        )
    return rendered


def new_completion_marker_path() -> str:
    return f"/tmp/wildawm_completion_{uuid.uuid4().hex}.md"


def initial_completion_contract(marker_path: str) -> str:
    return INITIAL_COMPLETION_CONTRACT.replace("{marker_path}", marker_path)


def agent_container_name(world: str, harness: str) -> str:
    return f"wildawm-agent-{harness}-{world}"


def marker_probe(container: str, marker_path: str) -> tuple[bool, str]:
    """Return (exists, content) for a marker kept outside /workspace so it never pollutes snapshots."""
    try:
        exists = subprocess.run(
            ["docker", "exec", container, "test", "-f", marker_path],
            capture_output=True, timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return False, ""
    if exists.returncode != 0:
        return False, ""
    try:
        read = subprocess.run(
            ["docker", "exec", container, "cat", marker_path],
            capture_output=True, text=True,
            timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return True, ""
    return True, read.stdout if read.returncode == 0 else ""


def remove_marker(container: str, marker_path: str) -> None:
    try:
        subprocess.run(
            ["docker", "exec", container, "rm", "-f", marker_path],
            capture_output=True, timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        pass


def _chat_accepts_timeout(chat_func: Callable) -> bool:
    try:
        sig = inspect.signature(chat_func)
    except (TypeError, ValueError):
        return False
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "timeout_s"
        for p in sig.parameters.values()
    )


def completion_probe_loop(*, chat_func: Callable[[str], dict],
                          container: str, task_text: str = "",
                          max_nudges: int | None = None,
                          deadline: float | None = None,
                          min_remaining_s: float | None = None,
                          log: Callable[[str], None] = print,
                          echo_reply: bool = False,
                          marker_probe_func: Callable[
                              [str, str], tuple[bool, str]] | None = None,
                          remove_marker_func: Callable[
                              [str, str], None] | None = None) -> dict:
    """Run the completion-marker probe; `chat_func` sends a prompt to the same agent session."""
    max_nudges = cfg.TEST_NUDGE_MAX if max_nudges is None else max_nudges
    max_nudges = max(0, int(max_nudges or 0))
    out = {
        "enabled": max_nudges > 0,
        "max_nudges": max_nudges,
        "nudges_used": 0,
        "done": False,
        "marker_path": None,
        "summary": "",
        "errors": [],
        "replies": [],
        "deadline": deadline,
        "budget_exhausted": False,
        "stop_reason": "",
    }
    if max_nudges <= 0:
        out["stop_reason"] = "disabled"
        return out

    marker_probe_func = marker_probe_func or marker_probe
    remove_marker_func = remove_marker_func or remove_marker
    accepts_timeout = _chat_accepts_timeout(chat_func)
    min_remaining = (
        cfg.HTTP_SHORT_TIMEOUT_S if min_remaining_s is None
        else max(0.0, float(min_remaining_s))
    )
    prev_marker = ""
    for idx in range(1, max_nudges + 1):
        remaining = None
        if deadline is not None:
            remaining = float(deadline) - time.time()
            out["remaining_s"] = round(max(0.0, remaining), 3)
            if remaining <= min_remaining:
                out["budget_exhausted"] = True
                out["stop_reason"] = "time_budget"
                log("completion probe skipped: insufficient remaining time budget")
                break
        if prev_marker:
            try:
                remove_marker_func(container, prev_marker)
            except Exception as e:  # noqa: BLE001
                err = f"marker_remove: {e}"
                out["errors"].append(err)
                log(f"completion marker cleanup errored: {e}")
        marker = new_completion_marker_path()
        prev_marker = marker
        prompt = completion_probe_prompt(marker, task_text=task_text)
        out["nudges_used"] = idx
        out["marker_path"] = marker
        log(f"completion probe {idx}/{max_nudges}: marker={marker}")
        call_timeout_s = None
        if accepts_timeout:
            if remaining is not None:
                call_timeout_s = remaining
            turn_cap = float(getattr(cfg, "TEST_NUDGE_TURN_TIMEOUT_S", 0) or 0)
            if turn_cap > 0:
                call_timeout_s = (
                    min(call_timeout_s, turn_cap)
                    if call_timeout_s is not None else turn_cap
                )
            if call_timeout_s is not None:
                call_timeout_s = max(1, int(call_timeout_s))
        try:
            chat = (
                chat_func(prompt, timeout_s=call_timeout_s)
                if call_timeout_s is not None else chat_func(prompt)
            )
            nreply = chat.get("content") or ""
            out["replies"].append(nreply)
            log(f"completion probe {idx}/{max_nudges} returned "
                f"{len(nreply)} chars")
            if echo_reply:
                print("\n" + (nreply or "(empty)") + "\n")
            if _looks_like_tool_parser_leak(nreply):
                err = "tool_parser_leak: repeated <router> markup in harness reply"
                out["errors"].append(err)
                out["stop_reason"] = "infra:tool_parser_leak"
                log("completion probe detected tool-parser leak; aborting probe loop")
                break
        except Exception as e:  # noqa: BLE001
            err = str(e)
            out["errors"].append(err)
            log(f"completion probe {idx}/{max_nudges} errored: {err}")
            if "stalled without tool progress" in err:
                out["stop_reason"] = "infra:agent_stall"
                log("completion probe stopped after harness made no tool progress")
                break
            continue

        try:
            exists, summary = marker_probe_func(container, marker)
        except Exception as e:  # noqa: BLE001
            err = f"marker_probe: {e}"
            out["errors"].append(err)
            out["stop_reason"] = "infra:marker_probe"
            log(f"completion marker probe errored: {e}")
            break
        if exists:
            out["done"] = True
            out["summary"] = summary
            out["stop_reason"] = "done"
            log(f"completion marker found ({len(summary)} chars)")
            break
        log("completion marker not found; continuing probe loop")
    if not out["stop_reason"]:
        out["stop_reason"] = "max_nudges"
    return out
