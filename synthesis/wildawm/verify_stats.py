"""Shared run/verify statistics helpers; unknown token counts stay ``None``."""

from __future__ import annotations

from typing import Any


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _avg(total: int | None, calls: int | None) -> float | None:
    if total is None or not calls:
        return None
    return round(total / calls, 1)


def _avg_per_item(total: int | float | None, item_count: int | None) -> float | None:
    if total is None or not item_count:
        return None
    return round(total / item_count, 1)


def _sum_known(values: list[int | float | None]) -> int | float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _norm_section(section: dict | None) -> dict:
    section = dict(section or {})
    out = {
        "seconds": _num(section.get("seconds")),
        "llm_calls": _int(section.get("llm_calls")),
        "tool_calls": _int(section.get("tool_calls")),
        "input_tokens": _int(section.get("input_tokens")),
        "output_tokens": _int(section.get("output_tokens")),
    }
    out["avg_input_tokens"] = _avg(out["input_tokens"], out["llm_calls"])
    out["avg_output_tokens"] = _avg(out["output_tokens"], out["llm_calls"])
    return out


def _proxy_steps(proxy_trajectory: Any) -> list[dict]:
    if isinstance(proxy_trajectory, list):
        return [x for x in proxy_trajectory if isinstance(x, dict)]
    if not isinstance(proxy_trajectory, dict):
        return []
    if isinstance(proxy_trajectory.get("steps"), list):
        return [x for x in proxy_trajectory["steps"] if isinstance(x, dict)]
    sessions = proxy_trajectory.get("sessions")
    if isinstance(sessions, dict):
        out: list[dict] = []
        for recs in sessions.values():
            if isinstance(recs, list):
                out.extend(x for x in recs if isinstance(x, dict))
        return out
    return []


def _step_usage(step: dict) -> tuple[int | None, int | None]:
    output = step.get("output")
    if not isinstance(output, dict):
        return None, None
    usage = output.get("usage")
    if not isinstance(usage, dict):
        return None, None
    inp = _int(usage.get("input_tokens", usage.get("prompt_tokens")))
    out = _int(usage.get("output_tokens", usage.get("completion_tokens")))
    return inp, out


def _proxy_tool_calls(step: dict) -> int:
    output = step.get("output")
    if not isinstance(output, dict):
        return 0
    tcs = output.get("tool_calls")
    if isinstance(tcs, list):
        return len(tcs)
    # Some response-shaped adapters record function calls under output[].
    items = output.get("output")
    if isinstance(items, list):
        return sum(1 for x in items
                   if isinstance(x, dict)
                   and str(x.get("type") or "").lower() in (
                       "function_call", "tool_call"))
    return 0


def agent_stats_from_history_proxy(
    history: list[dict] | None,
    proxy_trajectory: Any = None,
    *,
    seconds: float | int | None = None,
) -> dict:
    """Stats for a solver agent turn from structured history + proxy trajectory."""
    history = history or []
    steps = _proxy_steps(proxy_trajectory)
    assistant_msgs = sum(
        1 for h in history
        if h.get("type") == "message" and h.get("role") == "assistant")
    llm_calls = len(steps) or assistant_msgs or None
    hist_tools = sum(1 for h in history if h.get("type") == "tool_call")
    proxy_tools = sum(_proxy_tool_calls(s) for s in steps)
    tool_calls = hist_tools or proxy_tools or None
    in_parts, out_parts = [], []
    for step in steps:
        inp, out = _step_usage(step)
        in_parts.append(inp)
        out_parts.append(out)
    return _norm_section({
        "seconds": seconds,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "input_tokens": _sum_known(in_parts),
        "output_tokens": _sum_known(out_parts),
    })


def judge_stats_from_meta(meta: dict | None) -> dict:
    """Stats for a verifier/judge call from dashboard/synthesis judge metadata."""
    meta = meta or {}
    mode = str(meta.get("mode") or "")
    llm_calls = _int(meta.get("turns"))
    if llm_calls is None and mode == "lightweight":
        llm_calls = 1
    tool_calls = _int(meta.get("n_tool_uses"))
    if tool_calls is None:
        tool_calls = _int(meta.get("tool_calls"))
    return _norm_section({
        "seconds": meta.get("elapsed_s"),
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
    })


def finalize_stats(agent: dict | None = None, judge: dict | None = None) -> dict:
    """Return {agent, judge, total} with additive known values."""
    a = _norm_section(agent)
    j = _norm_section(judge)
    total_llm = _sum_known([a["llm_calls"], j["llm_calls"]])
    total_tools = _sum_known([a["tool_calls"], j["tool_calls"]])
    total_in = _sum_known([a["input_tokens"], j["input_tokens"]])
    total_out = _sum_known([a["output_tokens"], j["output_tokens"]])
    total_seconds = _sum_known([a["seconds"], j["seconds"]])
    total = _norm_section({
        "seconds": round(total_seconds, 2) if total_seconds is not None else None,
        "llm_calls": total_llm,
        "tool_calls": total_tools,
        "input_tokens": total_in,
        "output_tokens": total_out,
    })
    return {"agent": a, "judge": j, "total": total}


def stats_from_verify_response(verify_resp: dict | None,
                               manifest: dict | None = None) -> dict:
    """Build stats for a verify response, optionally preserving archived agent stats."""
    verify_resp = verify_resp or {}
    if isinstance(verify_resp.get("stats"), dict):
        return finalize_stats(
            verify_resp["stats"].get("agent"),
            verify_resp["stats"].get("judge"),
        )
    base = stats_from_manifest(manifest)
    return finalize_stats(base.get("agent"), judge_stats_from_meta(
        verify_resp.get("meta") if isinstance(verify_resp.get("meta"), dict) else {}))


def stats_from_manifest(manifest: dict | None) -> dict:
    manifest = manifest or {}
    if isinstance(manifest.get("stats"), dict):
        return finalize_stats(
            (manifest["stats"] or {}).get("agent"),
            (manifest["stats"] or {}).get("judge"),
        )
    timing = manifest.get("timing") if isinstance(manifest.get("timing"), dict) else {}
    tokens = manifest.get("tokens") if isinstance(manifest.get("tokens"), dict) else {}
    judge_tok = tokens.get("judge") if isinstance(tokens.get("judge"), dict) else {}
    return finalize_stats(
        {"seconds": timing.get("agent_seconds")},
        {"seconds": timing.get("verify"),
         "input_tokens": judge_tok.get("input"),
         "output_tokens": judge_tok.get("output")},
    )


def aggregate_stats(items: list[dict]) -> dict:
    """Aggregate a list of finalized stats dicts."""
    def section(name: str) -> dict:
        vals = [(s or {}).get(name) or {} for s in items]
        return _norm_section({
            "seconds": _sum_known([v.get("seconds") for v in vals]),
            "llm_calls": _sum_known([v.get("llm_calls") for v in vals]),
            "tool_calls": _sum_known([v.get("tool_calls") for v in vals]),
            "input_tokens": _sum_known([v.get("input_tokens") for v in vals]),
            "output_tokens": _sum_known([v.get("output_tokens") for v in vals]),
        })

    return finalize_stats(section("agent"), section("judge"))


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if v.is_integer():
            return f"{int(v)}{suffix}"
        return f"{v:.1f}{suffix}"
    return f"{v}{suffix}"


def compact_stats_line(stats: dict | None, *, item_count: int | None = None) -> str:
    """One-line human-readable stats summary."""
    stats = finalize_stats((stats or {}).get("agent"), (stats or {}).get("judge"))
    a, j, t = stats["agent"], stats["judge"], stats["total"]
    line = (
        f"llm={_fmt(t['llm_calls'])} "
        f"tools={_fmt(t['tool_calls'])} "
        f"tokens={_fmt(t['input_tokens'])}/{_fmt(t['output_tokens'])} "
        f"avg={_fmt(t['avg_input_tokens'])}/{_fmt(t['avg_output_tokens'])} "
        f"agent={_fmt(a['seconds'], 's')} "
        f"judge={_fmt(j['seconds'], 's')}"
    )
    if item_count is not None:
        line += (
            f" llm/task={_fmt(_avg_per_item(t['llm_calls'], item_count))} "
            f"tools/task={_fmt(_avg_per_item(t['tool_calls'], item_count))}"
        )
    return line


def table_stats_cells(stats: dict | None, *, item_count: int | None = None) -> list[str]:
    """Compact CLI table cells; with item_count, per-task averages are appended."""
    stats = finalize_stats((stats or {}).get("agent"), (stats or {}).get("judge"))
    a, j, t = stats["agent"], stats["judge"], stats["total"]
    cells = [
        _fmt(t["llm_calls"]),
        _fmt(t["tool_calls"]),
        f"{_fmt(t['input_tokens'])}/{_fmt(t['output_tokens'])}",
        f"{_fmt(t['avg_input_tokens'])}/{_fmt(t['avg_output_tokens'])}",
        _fmt(a["seconds"], "s"),
        _fmt(j["seconds"], "s"),
    ]
    if item_count is not None:
        cells.extend([
            _fmt(_avg_per_item(t["llm_calls"], item_count)),
            _fmt(_avg_per_item(t["tool_calls"], item_count)),
        ])
    return cells
