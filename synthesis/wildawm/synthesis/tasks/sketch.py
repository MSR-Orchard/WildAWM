#!/usr/bin/env python3
"""Sketch generation for task synthesis."""
import json
import math
import re
import time
import uuid
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import task_skill_pool as xp
from wildawm.synthesis.tasks import vision_shim as vs
from wildawm.synthesis.worlds import anchors
from wildawm.webharbor import lib as wh_lib

def _webharbor_site_registry_text() -> str:
    rows = [
        {"id": row["id"], "dbfile": row["dbfile"],
         "domains": row.get("domains") or []}
        for row in wh_lib.site_registry()
    ]
    return json.dumps(rows, ensure_ascii=False, indent=1)


def _image_policy_text() -> str:
    if cfg.XAI_REQUIRE_IMAGE_DECISIVE:
        return (
            "Every sketch must make the decisive Web choice depend on visual "
            "evidence obtained with the image tool. Include a complete "
            "image_grounding object. Text labels alone are insufficient."
        )
    return (
        "Image grounding is optional. Use the image tool and include "
        "image_grounding only when the proposed task genuinely depends on a "
        "visual fact; otherwise set image_grounding to null."
    )


_PROMPT = """# Propose {n} diverse cross-interface task sketches

You are in the live world for profile {profile}, day {day}. Inspect the clean
snapshot at `/tmp/_xai_snap`, the user-visible workspace, active MCP schemas,
and live WebHarbor sites. Propose grounded sketches only. Do not author task
files or verifiers in this stage.

Use live capability and schema evidence. Do not rely on a fixed domain, site
query vocabulary, product family, table-name allowlist, request-model naming
convention, or previous benchmark entity. A website read or write must be
possible through its public UI. When a sketch uses a Web write, discover the
actual persisted AFTER-state surface and natural key from the live site/schema;
do not guess a table or column. Before emitting `browser_write_flow`, query the
selected site's live SQLite schema and copy an existing table name exactly into
`web_write_surface.state_table`; a button/action name is not a table. Then run
a precise read-only SELECT, joining through user/account and target natural keys
as needed, that returns a row iff the exact intended account-target state already
exists. Put that SELECT in `baseline_absence_query`, and emit the sketch only if
it returned zero rows. A pre-existing save, enrollment, like, booking, review,
or equivalent state cannot count as a new browser write; choose another natural
target/action instead of confirming, deleting/recreating, or mutating it. Web
read tasks do not need a write surface or absence query.

Exploration is yours to structure: no order, action count, or stop point is
prescribed; the exploration wall clock is the only hard limit. Do not read
synthesis-skill files; the relevant experience is already in this prompt.
Regardless of how you explore:
- Obtain image evidence by fetching the target and distractor image URLs
  through the canonical public site host (for example `curl -k`; the local
  canonical-site TLS chain may not validate), saving the bytes under `/tmp`,
  and calling the image tool. Do not search the container filesystem for
  WebHarbor source files or static-asset directories, and do not search outside
  `/tmp/_xai_snap` for databases. In image-decisive mode, return `[]` if usable
  image evidence cannot be established.
One valid candidate is better than filling the requested count.

Each sketch must:
- require real CLI, MCP, and Web work, with all three indispensable;
- require the solver to write and run a script or command-line program over a
  real user-visible source or a solver-created export from an active MCP app;
- name only the public workspace path or solver-created `<app> MCP export` in
  `cli_code_step.input_sources`; never emit an internal snapshot/database path;
- make a later MCP or Web decision depend on the script result; a separate
  output artifact is optional;
- use an MCP app active in this world: {active_mcp_apps};
- describe the interface order in `flow`, naming `cli`, `mcp`, and `web`; no
  fixed hop count or interface re-cross is required;
- target {difficulty_target};
- remain self-contained, solvable, and machine-verifiable;
- use only entities and capabilities established by live inspection;
- avoid exposing private answer keys, internal database paths, verifier routes,
  or raw fixture IDs in the public title and summary.

{difficulty_rule}

Image policy: {image_policy}

Use canonical WebHarbor site IDs from this live platform registry. JSON fields
`sites`, `image_grounding.site`, and `web_write_surface.site` must use `id`,
never the database filename or host domain:
{webharbor_sites}

The current clean workspace files are:
{workspace_file_index}

Short workspace previews:
{workspace_source_previews}

Accepted and concurrently locked tasks for semantic diversity comparison:
{prior_records}

{experience_notes}

Return only a JSON array with at most {n} objects. Return fewer candidates or
`[]` when evidence is insufficient. Use this schema:
[{{
  "title": "concise public title",
  "area": "coarse life area",
  "archetype": "short task pattern",
  "difficulty": "medium|hard",
  "summary": "public objective without the private answer",
  "interfaces": ["cli", "mcp", "web"],
  "flow": "cli: ... -> mcp: ... -> web: ...",
  "sites": ["live WebHarbor site id"],
  "mcp_domains": ["active MCP app id"],
  "image_grounding": {{"site":"...","attribute":"...","truth":"...","distractor":"..."}} | null,
  "cli_code_step": {{
    "requires_code": true,
    "purpose": "what the script or program computes",
    "input_sources": ["real workspace path or real MCP export source"],
    "output_artifact": "optional relative path or null",
    "downstream_use": "how the script result drives later MCP/Web work"
  }},
  "web_action_type": "read_only|browser_write_flow",
  "web_write_surface": {{"site":"...","action":"...","state_table":"live discovered table","natural_key":"user-facing non-ID column such as slug/title/URL","target_natural_key":"exact nonnumeric target value","baseline_absence_query":"single read-only SELECT against the exact account-target state; returns a row iff that state already exists, and was confirmed to return zero rows"}} | null,
  "natural_keys": ["explicit nonnumeric target URL, slug, title, name, or other live natural-key value; never a row/FK id"],
  "anchors": "grounding evidence",
  "why_hard": "brief reason"
}}]
"""

_CONTINUATION_PROMPT = """Continue the same sketch proposal task using evidence already gathered. Do not try to finish the original exploration plan and do not continue broad exploration. Output only a JSON array with at most {n} sketches. Every sketch must require indispensable cli, mcp, and web work, and must require the solver to write and run a script or program whose result drives later work. Do not impose a fixed flow-hop count. {image_policy} Return `[]` if the evidence does not support a grounded task."""

_FINALIZE_PROMPT = """Finish the current sketch proposal using evidence already gathered. Do not use tools. Output only a JSON array with at most {n} sketches. Every sketch must require indispensable cli, mcp, and web work, and must require the solver to write and run a script or program whose result drives later work. Do not impose a fixed flow-hop count. {image_policy} Return `[]` if the evidence does not support a grounded task."""

_FRESH_FINALIZE_PROMPT = """Convert the supplied exploration evidence into at most {n} grounded task sketches. Output only one JSON array. Do not use tools and do not invent missing capabilities, entities, files, schema, visual facts, or baseline-absence evidence.

Every sketch must require indispensable cli, mcp, and web work. The solver must write and run a script or program over a real workspace source or a solver-created export from an active MCP app, and later MCP/Web work must depend on its result. In emitted `cli_code_step.input_sources`, name the public workspace path or the solver-created `<app> MCP export`; never emit `/tmp/_xai_snap`, a SQLite path, a home path, or any other internal exploration source. A separate output artifact is optional. Do not require a fixed number of interface hops. Web writes must name an exact existing state table established by a live SQLite schema query in the evidence; never turn an UI button/action name into a guessed table. A browser write also requires a read-only `baseline_absence_query` from the supplied tool evidence that joins through the exact intended account and target natural keys, returns a row iff that state already exists, and was observed to return zero rows. Omit any write candidate whose target state already exists or whose absence was not actually queried; never propose confirmation or delete/recreate as a new write. {image_policy}

Active MCP apps: {active_mcp_apps}. `mcp_domains` may contain only these app
IDs. WebHarbor site databases and website/catalog queries are Web evidence,
not MCP work.

Canonical WebHarbor site registry. Use each `id` in task JSON, never its
`dbfile` or domain:
{webharbor_sites}

Clean workspace files:
{workspace_file_index}

Workspace previews:
{workspace_source_previews}

Exploration notes:
{exploration_notes}

Tool evidence:
{transcript_evidence}

Return only a JSON array with at most {n} objects in this exact schema. Do not
return `id`, `task`, `required_work`, or `dependency_chain` as substitutes for
the required fields:
[{{
  "title": "concise public title",
  "area": "coarse life area",
  "archetype": "short task pattern",
  "difficulty": "medium|hard",
  "summary": "public objective without the private answer",
  "interfaces": ["cli", "mcp", "web"],
  "flow": "cli: ... -> mcp: ... -> web: ...",
  "sites": ["canonical live WebHarbor site id"],
  "mcp_domains": ["active MCP app id"],
  "image_grounding": {{"site":"canonical site id","attribute":"...","truth":"...","distractor":"..."}} | null,
  "cli_code_step": {{
    "requires_code": true,
    "purpose": "what the script or program computes",
    "input_sources": ["real workspace path or real MCP export source"],
    "output_artifact": "optional relative path or null",
    "downstream_use": "how the script result drives later MCP/Web work"
  }},
  "web_action_type": "read_only|browser_write_flow",
  "web_write_surface": {{"site":"canonical site id","action":"...","state_table":"live discovered table","natural_key":"user-facing non-ID column such as slug/title/URL","target_natural_key":"exact nonnumeric target value","baseline_absence_query":"single read-only SELECT against the exact account-target state; returns a row iff that state already exists, and was confirmed to return zero rows"}} | null,
  "natural_keys": ["explicit nonnumeric target URL, slug, title, name, or other live natural-key value; never a row/FK id"],
  "anchors": "grounding evidence",
  "why_hard": "brief reason"
}}]
Return `[]` only when the supplied evidence cannot support even one object in
this schema.
"""

_CONTINUATION_STOP_STATUSES = {"interrupted", "timeout", "max_turns"}
_OPENCODE_FINALIZE_STATUSES = {
    "interrupted", "timeout", "max_turns", "max_output", "empty", "parse_miss"
}
_OPENCODE_PREFILL_ERROR_RE = re.compile(
    r"assistant message prefill|conversation must end with a user message",
    re.I,
)


def _opencode_should_finalize_failure(rec: cr.CallRecord,
                                      call_dict: dict) -> bool:
    if rec.status in _OPENCODE_FINALIZE_STATUSES:
        return True
    haystack = "\n".join(
        str(part or "")
        for part in (
            rec.result_text,
            rec.stdout_tail,
            rec.stderr_tail,
            call_dict.get("result_text"),
            call_dict.get("stdout_tail"),
            call_dict.get("stderr_tail"),
        )
    )
    return bool(_OPENCODE_PREFILL_ERROR_RE.search(haystack))


def _difficulty_prompt_parts(difficulty: str) -> tuple[str, str]:
    """Return human target text and a JSON-field rule for sketch prompts."""
    raw = str(difficulty or "").strip()
    low = raw.lower()
    if low in {"medium", "hard"}:
        return low, (
            f'- sets the JSON field `"difficulty"` to exactly `"{low}"`. '
            "Do not write batch labels, mixed labels, prose, or any other value "
            "in that field;"
        )
    if "mixed day batch" in low:
        has_medium = "medium" in low
        has_hard = "hard" in low
        if has_hard and not has_medium:
            return "hard", (
                '- sets the JSON field `"difficulty"` to exactly `"hard"`. '
                "This request is a day-batch wrapper for hard slot(s); never "
                "write the wrapper label in the JSON field;"
            )
        if has_medium and not has_hard:
            return "medium", (
                '- sets the JSON field `"difficulty"` to exactly `"medium"`. '
                "This request is a day-batch wrapper for medium slot(s); never "
                "write the wrapper label in the JSON field;"
            )
        return "the requested medium/hard day mix", (
            '- sets the JSON field `"difficulty"` to either `"medium"` or '
            '`"hard"` for each individual sketch. Never write a batch label, '
            "mixed label, prose, or any other value in that field;"
        )
    return "medium or hard", (
        '- sets the JSON field `"difficulty"` to either `"medium"` or `"hard"`. '
        "Never write batch labels, prose, or any other value in that field;"
    )


def setup_context(container: str, profile: str, day: int) -> None:
    """Copy the clean day snapshot into the sketch container."""
    # A filled scratch copy; the master world is never modified here.
    snap = anchors.inspection_world(profile, day)
    cr.run_cmd(
        ["docker", "exec", container, "rm", "-rf", "/tmp/_xai_snap"],
        role="sketch_setup", check=False)
    cr.run_cmd(["docker", "cp", str(snap), f"{container}:/tmp/_xai_snap"],
               role="sketch_setup", timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    cr.run_cmd(["docker", "cp", str(vs.DESCRIBE_IMAGE_MCP),
                f"{container}:{vs.CONTAINER_DESCRIBE_IMAGE_MCP}"],
               role="sketch_setup")
    xp.stage_readonly(container)


def _tool_watchdog(container: str, started_epoch: float):
    def _check() -> tuple[bool, str]:
        if cfg.SKETCH_TOOL_LIMIT <= 0:
            return False, ""
        probe = cr.run_cmd(
            [
                "docker", "exec", container, "python3", "-c",
                (
                    "import datetime,json,pathlib,sys\n"
                    "started=float(sys.argv[1]); n=0\n"
                    "root=pathlib.Path('/home/agent/.claude/projects')\n"
                    "for p in root.glob('**/*.jsonl'):\n"
                    "    try: lines=p.open(errors='replace')\n"
                    "    except OSError: continue\n"
                    "    with lines:\n"
                    "        for line in lines:\n"
                    "            try: e=json.loads(line)\n"
                    "            except Exception: continue\n"
                    "            ts=e.get('timestamp')\n"
                    "            if ts:\n"
                    "                try:\n"
                    "                    epoch=datetime.datetime.fromisoformat(\n"
                    "                        ts.replace('Z','+00:00')).timestamp()\n"
                    "                except ValueError:\n"
                    "                    epoch=started\n"
                    "                if epoch < started: continue\n"
                    "            msg=e.get('message') or {}\n"
                    "            if e.get('type')!='assistant' or msg.get('role')!='assistant':\n"
                    "                continue\n"
                    "            for c in msg.get('content') or []:\n"
                    "                if isinstance(c,dict) and c.get('type')=='tool_use':\n"
                    "                    n+=1\n"
                    "print(n)"
                ),
                str(started_epoch),
            ],
            role="sketch_watchdog", timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
            check=False)
        if not probe.ok:
            return False, ""
        try:
            tool_count = int(probe.result_text.strip())
        except ValueError:
            return False, ""
        if tool_count <= cfg.SKETCH_TOOL_LIMIT:
            return False, ""
        return (
            True,
            "sketch tool watchdog fired: "
            f"{tool_count} tool calls before JSON sketches",
        )

    return _check


def _llm_turn_watchdog(container: str, session_id: str, max_turns: int,
                       *, baseline_turns: int = 0,
                       label: str = "sketch LLM turn budget"):
    last_count = 0

    def _check() -> tuple[bool, str]:
        nonlocal last_count
        if max_turns <= 0 or not session_id:
            return False, ""
        transcript = cr.capture_claude_session_transcript(
            container=container, session_id=session_id)
        turn_count = max(
            0, cr.count_claude_llm_assistant_turns(transcript) - baseline_turns)
        last_count = max(last_count, turn_count)
        if last_count < max_turns:
            return False, ""
        return (
            True,
            f"{label} reached: "
            f"{last_count}/{max_turns}; interrupting live Claude session",
        )

    return _check


def _continuation_extra(*, baseline_turns: int, total_turns: int,
                        phase_turns: int, finalize_turns: int,
                        timeout_s: int) -> dict:
    return {
        "resumed_after_interrupt": True,
        "continuation_baseline_turns": baseline_turns,
        "continuation_max_llm_turns": total_turns,
        "continuation_phase_max_llm_turns": phase_turns,
        "finalize_max_llm_turns": finalize_turns,
        "continuation_timeout_s": timeout_s,
    }


def _workspace_file_index(container: str, *, limit: int = 120) -> list[str]:
    """Return user-visible clean workspace files for sketch prompting."""
    try:
        rec = cr.run_cmd(
            [
                "docker", "exec", container, "sh", "-lc",
                (
                    "find /workspace -type f "
                    "! -path '*/.git/*' "
                    "! -path '*/__pycache__/*' "
                    "! -path '*/node_modules/*' "
                    "! -path '*/.venv/*' "
                    "-printf '%P\\n' | sort"
                ),
            ],
            role="sketch_workspace_file_index",
            timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
            check=False,
        )
    except Exception:
        return []
    if not rec.ok:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in rec.result_text.splitlines():
        path = line.strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if not path:
            continue
        if path in {"USER.md", "WORLD.md"}:
            continue
        parts = Path(path).parts
        if any(part.startswith(".") for part in parts):
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _format_workspace_file_index(paths: list[str]) -> str:
    if not paths:
        return (
            "- (no user-visible files found; do not claim a pre-existing "
            "workspace input file in `cli_code_step.input_sources`)"
        )
    return "\n".join(f"- {p}" for p in paths)


def _workspace_source_previews(
        container: str, paths: list[str], *, limit: int = 36,
        per_file_chars: int = 700, total_chars: int = 12000) -> dict[str, str]:
    """Return compact text previews for user-visible clean workspace files."""
    if not paths:
        return {}
    script = r"""
import json
import os
import sys

paths = json.loads(sys.argv[1])
limit = int(sys.argv[2])
per_file_chars = int(sys.argv[3])
root = os.path.realpath("/workspace")
out = {}

for rel in paths:
    if len(out) >= limit:
        break
    rel = str(rel).replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        continue
    if not os.path.isfile(full):
        continue
    try:
        with open(full, "rb") as f:
            raw = f.read(max(2048, per_file_chars * 3))
    except OSError:
        continue
    if not raw or b"\x00" in raw:
        continue
    text = raw.decode("utf-8", "ignore")
    if not text.strip():
        continue
    printable = sum((ch.isprintable() or ch.isspace()) for ch in text)
    if printable / max(1, len(text)) < 0.85:
        continue
    text = " ".join(text.split())
    if text:
        out[rel] = text[:per_file_chars]

print(json.dumps(out, ensure_ascii=False))
"""
    try:
        rec = cr.run_cmd(
            [
                "docker", "exec", container, "python3", "-c", script,
                json.dumps(paths, ensure_ascii=False),
                str(limit),
                str(per_file_chars),
            ],
            role="sketch_workspace_source_previews",
            timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
            check=False,
        )
    except Exception:
        return {}
    if not rec.ok:
        return {}
    try:
        previews = json.loads(rec.result_text)
    except Exception:
        return {}
    if not isinstance(previews, dict):
        return {}
    out: dict[str, str] = {}
    used = 0
    for path in paths:
        text = str(previews.get(path) or "").strip()
        if not text:
            continue
        budget = max(0, total_chars - used)
        if budget <= 0:
            break
        clipped = text[:budget]
        out[path] = clipped
        used += len(path) + len(clipped) + 8
    return out


def _format_workspace_source_previews(previews: dict[str, str]) -> str:
    if not previews:
        return (
            "- (no text previews available; use real MCP app evidence from "
            "the snapshot as source support, or output [] if neither MCP nor "
            "workspace evidence supports the same domain as the web write)"
        )
    lines: list[str] = []
    for path, text in previews.items():
        clean = " ".join(str(text or "").split())
        lines.append(f"- `{path}`: {clean}")
    return "\n".join(lines)


def _format_prior_records(priors: list[dict], *, limit: int = 180) -> str:
    if not priors:
        return "- (none yet)"
    lines = []
    for p in priors[-limit:]:
        ig = p.get("image_grounding") if isinstance(p.get("image_grounding"), dict) else {}
        sites = p.get("sites") or ([ig.get("site")] if ig.get("site") else [])
        site = ",".join(str(s) for s in sites if s) or "?"
        attr = (
            ig.get("attribute") or ig.get("visual_attribute")
            or ig.get("image_type") or ig.get("type") or "?"
        )
        lines.append(
            f"- [{p.get('status', 'prior')}|{p.get('difficulty','?')}|"
            f"{p.get('area','?')}] site={site} archetype={p.get('archetype','?')} "
            f"image={str(attr)[:120]} title={p.get('title','?')}"
        )
    return "\n".join(lines)


def _finalize_stopped_continuation(
        container: str, active_session_id: str, n: int, *,
        continuation_phase: dict, continuation_timeout: int,
        finalize_max_turns: int, continuation_extra: dict) -> cr.CallRecord:
    rec = cr.run_claude_code(
        _FINALIZE_PROMPT.format(n=n, image_policy=_image_policy_text()),
        cr.LLM(model=cfg.TASK_SKETCH_LLM_MODEL,
               effort=cfg.TASK_SKETCH_LLM_EFFORT,
               route=cfg.TASK_SKETCH_LLM_ROUTE),
        role="sketch", timeout=continuation_timeout,
        container=container, proxy_url=cfg.TASK_SKETCH_LLM_PROXY,
        retries=0,
        allowed_tools="",
        mcp_config=None, mcp_strict=True,
        disallowed_tools=None,
        resume_session_id=continuation_phase.get("session_id")
        or active_session_id,
        max_turns=finalize_max_turns,
        stall_check=None, interrupt_check=None,
        stall_poll_s=cfg.SKETCH_LLM_TURN_POLL_S)
    rec.extra.update(continuation_extra)
    rec.extra["finalized_after_continuation_stop"] = True
    rec.extra["continuation_phase"] = continuation_phase
    return rec


def propose(container: str, profile: str, day: int, *, difficulty: str,
            priors: list[dict], require_web: bool, n: int = 4,
            timeout: int | None = None, session_id: str | None = None,
            resume_session_id: str | None = None) -> dict:
    """Run the sketch call inside the provisioned generator container."""
    setup_context(container, profile, day)
    active_session_id = resume_session_id or session_id or str(uuid.uuid4())
    use_opencode = harness.resolve_harness("sketch") == "opencode"
    snapshot_root = anchors.master_snapshot_dir(profile, day)
    snapshot_data = snapshot_root / "data"
    snapshot_apps = snapshot_root / "overlay" / "apps"
    generated_apps = {path.name for path in snapshot_apps.iterdir() if path.is_dir()} if snapshot_apps.is_dir() else set()
    active_mcp_apps = sorted(path.stem for path in snapshot_data.glob("*.db") if path.stem in generated_apps) if snapshot_data.is_dir() else []
    active_mcp_text = ", ".join(active_mcp_apps) or "(none; no valid XAI task can require MCP until a service is synthesized)"
    workspace_files: list[str] = []
    workspace_source_previews: dict[str, str] = {}
    if not resume_session_id:
        workspace_files = _workspace_file_index(container)
        workspace_source_previews = _workspace_source_previews(container, workspace_files)
    # Visual evidence comes only from the sketch agent's live image-tool calls.
    if resume_session_id:
        prompt = _CONTINUATION_PROMPT.format(
            n=n, image_policy=_image_policy_text())
    else:
        difficulty_target, difficulty_rule = _difficulty_prompt_parts(difficulty)
        prompt = _PROMPT.format(
            n=n, profile=profile, day=day, difficulty=difficulty,
            active_mcp_apps=active_mcp_text,
            difficulty_target=difficulty_target,
            difficulty_rule=difficulty_rule,
            workspace_file_index=_format_workspace_file_index(workspace_files),
            workspace_source_previews=_format_workspace_source_previews(
                workspace_source_previews),
            prior_records=_format_prior_records(priors),
            image_policy=_image_policy_text(),
            webharbor_sites=_webharbor_site_registry_text(),
            experience_notes=xp.prompt_section())
    # opencode: one bounded run (maxSteps); vision and app/browser MCP come from run_agent.
    if use_opencode:
        return _propose_opencode(container, prompt, n, difficulty=difficulty,
                                 timeout=timeout,
                                 resume_session_id=resume_session_id,
                                 active_mcp_apps=active_mcp_text,
                                 workspace_files=workspace_files,
                                 workspace_source_previews=workspace_source_previews)
    mcp_config = disallowed_tools = None
    mcp_strict = True
    if cfg.model_needs_vision_shim(cfg.TASK_SKETCH_LLM_MODEL):
        prompt += vs.SKETCH_NOTE
        mcp_config = vs.generator_mcp_config()
        disallowed_tools = vs.generator_disallowed_tools()
        mcp_strict = False
    tool_watchdog = (
        _tool_watchdog(container, time.time())
        if cfg.SKETCH_TOOL_LIMIT > 0 else None
    )
    interrupt_watchdog = (
        _llm_turn_watchdog(container, active_session_id, cfg.SKETCH_MAX_LLM_TURNS)
        if not resume_session_id and cfg.SKETCH_MAX_LLM_TURNS > 0 else None
    )
    continuation_timeout = min(
        timeout or cfg.LLM_MEDIUM_TIMEOUT_S,
        cfg.SKETCH_CONTINUATION_TIMEOUT_S,
    )
    finalize_max_turns = max(1, cfg.SKETCH_FINALIZE_MAX_LLM_TURNS)
    continuation_total_turns = max(1, cfg.SKETCH_CONTINUATION_MAX_LLM_TURNS)
    continuation_phase_turns = max(
        1, continuation_total_turns - finalize_max_turns)
    baseline_turns = 0
    resume_interrupt_watchdog = None
    run_timeout = timeout or cfg.LLM_MEDIUM_TIMEOUT_S
    run_max_turns = 0
    run_interrupt_check = interrupt_watchdog
    run_stall_poll_s = cfg.SKETCH_LLM_TURN_POLL_S if interrupt_watchdog else 15
    if resume_session_id:
        transcript_before_resume = cr.capture_claude_session_transcript(
            container=container, session_id=active_session_id)
        baseline_turns = cr.count_claude_llm_assistant_turns(
            transcript_before_resume)
        resume_interrupt_watchdog = _llm_turn_watchdog(
            container, active_session_id, continuation_phase_turns,
            baseline_turns=baseline_turns,
            label="sketch continuation LLM turn budget")
        run_timeout = continuation_timeout
        run_max_turns = continuation_phase_turns
        run_interrupt_check = resume_interrupt_watchdog
        run_stall_poll_s = cfg.SKETCH_LLM_TURN_POLL_S
    rec = cr.run_claude_code(
        prompt,
        cr.LLM(model=cfg.TASK_SKETCH_LLM_MODEL,
               effort=cfg.TASK_SKETCH_LLM_EFFORT,
               route=cfg.TASK_SKETCH_LLM_ROUTE),
        role="sketch", timeout=run_timeout,
        container=container, proxy_url=cfg.TASK_SKETCH_LLM_PROXY, retries=0,
        mcp_config=mcp_config, mcp_strict=mcp_strict,
        disallowed_tools=disallowed_tools,
        session_id=active_session_id if not resume_session_id else None,
        resume_session_id=resume_session_id,
        max_turns=run_max_turns,
        stall_check=tool_watchdog if not resume_session_id else None,
        interrupt_check=run_interrupt_check,
        stall_poll_s=run_stall_poll_s)
    interrupted_phase = None
    if resume_session_id:
        continuation_extra = _continuation_extra(
            baseline_turns=baseline_turns,
            total_turns=continuation_total_turns,
            phase_turns=continuation_phase_turns,
            finalize_turns=finalize_max_turns,
            timeout_s=continuation_timeout)
        rec.extra.update(continuation_extra)
        if rec.status in _CONTINUATION_STOP_STATUSES:
            continuation_phase = rec.to_dict()
            rec = _finalize_stopped_continuation(
                container, active_session_id, n,
                continuation_phase=continuation_phase,
                continuation_timeout=continuation_timeout,
                finalize_max_turns=finalize_max_turns,
                continuation_extra=continuation_extra)
    if rec.status == "interrupted" and not resume_session_id:
        interrupted_phase = rec.to_dict()
        continue_prompt = _CONTINUATION_PROMPT.format(
            n=n, image_policy=_image_policy_text())
        if cfg.model_needs_vision_shim(cfg.TASK_SKETCH_LLM_MODEL):
            continue_prompt += vs.SKETCH_NOTE
        transcript_before_resume = cr.capture_claude_session_transcript(
            container=container, session_id=rec.session_id or active_session_id)
        baseline_turns = cr.count_claude_llm_assistant_turns(transcript_before_resume)
        rec = cr.run_claude_code(
            continue_prompt,
            cr.LLM(model=cfg.TASK_SKETCH_LLM_MODEL,
                   effort=cfg.TASK_SKETCH_LLM_EFFORT,
                   route=cfg.TASK_SKETCH_LLM_ROUTE),
            role="sketch", timeout=continuation_timeout,
            container=container, proxy_url=cfg.TASK_SKETCH_LLM_PROXY, retries=0,
            allowed_tools=None,
            mcp_config=mcp_config, mcp_strict=mcp_strict,
            disallowed_tools=disallowed_tools,
            resume_session_id=rec.session_id or active_session_id,
            max_turns=continuation_phase_turns,
            stall_check=None,
            interrupt_check=_llm_turn_watchdog(
                container, rec.session_id or active_session_id,
                continuation_phase_turns,
                baseline_turns=baseline_turns,
                label="sketch continuation LLM turn budget"),
            stall_poll_s=cfg.SKETCH_LLM_TURN_POLL_S)
        continuation_extra = _continuation_extra(
            baseline_turns=baseline_turns,
            total_turns=continuation_total_turns,
            phase_turns=continuation_phase_turns,
            finalize_turns=finalize_max_turns,
            timeout_s=continuation_timeout)
        rec.extra.update(continuation_extra)
        if rec.status in _CONTINUATION_STOP_STATUSES:
            continuation_phase = rec.to_dict()
            rec = _finalize_stopped_continuation(
                container, active_session_id, n,
                continuation_phase=continuation_phase,
                continuation_timeout=continuation_timeout,
                finalize_max_turns=finalize_max_turns,
                continuation_extra=continuation_extra)
    call_dict = rec.to_dict()
    if interrupted_phase:
        call_dict.setdefault("extra", {})["interrupted_phase"] = interrupted_phase
    transcript = cr.capture_claude_session_transcript(container=container,
                                                      session_id=rec.session_id)
    if transcript:
        call_dict["session_transcript"] = transcript
        call_dict["session_transcript_events"] = len(transcript)
    if not rec.ok:
        return {"ok": False, "sketches": [], "call": call_dict}
    sketches = _parse_sketches(rec.result_text)
    return {"ok": bool(sketches), "sketches": sketches, "call": call_dict}


def _propose_opencode(container: str, prompt: str, n: int, *,
                      difficulty: str = "hard",
                      timeout: int | None,
                      resume_session_id: str | None,
                      active_mcp_apps: str,
                      workspace_files: list[str] | None = None,
                      workspace_source_previews: dict[str, str] | None = None) -> dict:
    """Run bounded live sketch exploration and an optional JSON finalizer."""
    total_timeout = timeout or cfg.LLM_MEDIUM_TIMEOUT_S
    reserved_finalize_timeout = _opencode_reserved_finalize_timeout(total_timeout)
    explore_timeout = max(1, total_timeout - reserved_finalize_timeout)
    rec = harness.run_agent(
        prompt, role="sketch", timeout=explore_timeout,
        container=container, workdir="/workspace", container_mcp=False,
        retries=0, resume_session_id=resume_session_id,
        max_turns=_opencode_explore_max_turns(), vision=True)
    call_dict = _call_dict_with_transcript(rec)
    if not rec.ok:
        if (_opencode_should_finalize_failure(rec, call_dict)
                and _opencode_has_vision_evidence(call_dict)):
            return _finalize_opencode_sketch(
                container, n, rec, call_dict,
                difficulty=difficulty,
                finalize_timeout=_opencode_finalize_timeout(
                    total_timeout, elapsed_s=rec.seconds),
                reason="explore_stop",
                active_mcp_apps=active_mcp_apps,
                workspace_files=workspace_files,
                workspace_source_previews=workspace_source_previews)
        return {"ok": False, "sketches": [], "call": call_dict}
    sketches = _parse_sketches(rec.result_text)
    if not sketches:
        if not _opencode_has_vision_evidence(call_dict):
            return {"ok": False, "sketches": [], "call": call_dict}
        return _finalize_opencode_sketch(
            container, n, rec, call_dict,
            difficulty=difficulty,
            finalize_timeout=_opencode_finalize_timeout(
                total_timeout, elapsed_s=rec.seconds),
            reason="parse_miss",
            active_mcp_apps=active_mcp_apps,
            workspace_files=workspace_files,
            workspace_source_previews=workspace_source_previews)
    if not _opencode_has_vision_evidence(call_dict):
        return {"ok": False, "sketches": [], "call": call_dict}
    return {"ok": True, "sketches": sketches, "call": call_dict}


def _opencode_has_vision_evidence(call_dict: dict) -> bool:
    if not cfg.XAI_REQUIRE_IMAGE_DECISIVE:
        return True

    def walk(obj) -> bool:
        if isinstance(obj, dict):
            if isinstance(obj.get("n_vision_calls"), int) and obj.get("n_vision_calls") > 0:
                return True
            return any(walk(v) for v in obj.values())
        if isinstance(obj, list):
            return any(walk(v) for v in obj)
        return False

    return walk(call_dict)


def _opencode_reserved_finalize_timeout(total_timeout: int) -> int:
    reserve = max(0, min(
        cfg.SKETCH_OPENCODE_FINALIZE_RESERVE_S,
        max(0, total_timeout - 1),
    ))
    if reserve <= 0:
        return min(total_timeout, cfg.SKETCH_OPENCODE_FRESH_FINALIZE_TIMEOUT_S)
    return max(1, reserve)


def _opencode_finalize_timeout(total_timeout: int, *, elapsed_s: float) -> int:
    reserved = _opencode_reserved_finalize_timeout(total_timeout)
    remaining = max(1, math.ceil(total_timeout - max(0.0, float(elapsed_s or 0))))
    return max(1, min(max(reserved, remaining), total_timeout))


def _opencode_explore_max_turns() -> int:
    base = int(cfg.SKETCH_MAX_LLM_TURNS)
    cap = int(cfg.SKETCH_OPENCODE_EXPLORE_MAX_LLM_TURNS)
    # Zero: no maxSteps entry; wall time is the only exploration limit.
    if base <= 0 or cap <= 0:
        return 0
    return max(1, min(base, cap))


def _call_dict_with_transcript(rec: cr.CallRecord) -> dict:
    transcript = rec.extra.pop("trajectory", None) or []
    call_dict = rec.to_dict()
    if transcript:
        call_dict["session_transcript"] = transcript
        call_dict["session_transcript_events"] = len(transcript)
    return call_dict


def _finalize_opencode_sketch(
        container: str, n: int, rec: cr.CallRecord, phase_call: dict, *,
        difficulty: str,
        finalize_timeout: int, reason: str,
        active_mcp_apps: str,
        workspace_files: list[str] | None = None,
        workspace_source_previews: dict[str, str] | None = None) -> dict:
    fresh_finalizer = reason in {"parse_miss", "explore_stop"}
    direct_failure: dict | None = None
    if fresh_finalizer:
        finalize_timeout = max(
            1,
            min(finalize_timeout, cfg.SKETCH_OPENCODE_FRESH_FINALIZE_TIMEOUT_S),
        )
        direct = _direct_fresh_opencode_finalizer(
            n=n, difficulty=difficulty, rec=rec, phase_call=phase_call,
            finalize_timeout=finalize_timeout, reason=reason,
            active_mcp_apps=active_mcp_apps,
            workspace_files=workspace_files,
            workspace_source_previews=workspace_source_previews)
        if direct is not None:
            if direct.get("ok"):
                return direct
            direct_failure = direct.get("call") or {}
            spent = int(math.ceil(float(direct_failure.get("seconds") or 0)))
            finalize_timeout = max(1, finalize_timeout - max(0, spent))
    prompt = (
        _fresh_opencode_finalize_prompt(
            n=n, difficulty=difficulty, phase_call=phase_call,
            active_mcp_apps=active_mcp_apps,
            workspace_files=workspace_files,
            workspace_source_previews=workspace_source_previews)
        if fresh_finalizer else _FINALIZE_PROMPT.format(
            n=n, image_policy=_image_policy_text())
    )
    final_rec = harness.run_agent(
        prompt,
        role="sketch",
        timeout=finalize_timeout,
        container=container,
        workdir="/workspace",
        container_mcp=False,
        retries=0,
        resume_session_id=None if fresh_finalizer else rec.session_id,
        max_turns=max(1, cfg.SKETCH_FINALIZE_MAX_LLM_TURNS),
        allowed_tools="",
        vision=False,
        opencode_container_scratch=rec.extra.get("opencode_container_scratch"),
    )
    final_call = _call_dict_with_transcript(final_rec)
    extra = final_call.setdefault("extra", {})
    extra[f"finalized_after_{reason}"] = True
    extra[f"{reason}_phase"] = phase_call
    extra["explore_elapsed_s"] = rec.seconds
    extra["finalize_budget_s"] = finalize_timeout
    extra["finalize_elapsed_s"] = final_rec.seconds
    extra["fresh_finalizer"] = fresh_finalizer
    extra["unspent_budget_s"] = max(0, finalize_timeout - final_rec.seconds)
    if direct_failure is not None:
        extra["direct_fresh_finalizer_fallback"] = True
        extra["direct_fresh_finalizer_failure"] = {
            "status": direct_failure.get("status"),
            "seconds": direct_failure.get("seconds"),
            "chars": len(str(direct_failure.get("result_text") or "")),
            "parsed": (
                (direct_failure.get("extra") or {})
                .get("fresh_finalizer_parsed_sketches")
            ),
            "failures": (
                (direct_failure.get("extra") or {})
                .get("fresh_finalizer_failures")
            ),
        }
    if not final_rec.ok:
        return {"ok": False, "sketches": [], "call": final_call}
    sketches = _parse_sketches(final_rec.result_text)
    if sketches and not _opencode_has_vision_evidence(final_call):
        return {"ok": False, "sketches": [], "call": final_call}
    return {"ok": bool(sketches), "sketches": sketches, "call": final_call}


def _annotate_fresh_finalizer_call(
        call: dict, *, rec: cr.CallRecord, phase_call: dict, reason: str,
        finalize_timeout: int, fresh_finalizer: bool) -> dict:
    extra = call.setdefault("extra", {})
    extra[f"finalized_after_{reason}"] = True
    extra[f"{reason}_phase"] = phase_call
    extra["explore_elapsed_s"] = rec.seconds
    extra["finalize_budget_s"] = finalize_timeout
    extra["finalize_elapsed_s"] = call.get("seconds")
    extra["fresh_finalizer"] = fresh_finalizer
    extra["unspent_budget_s"] = max(
        0, finalize_timeout - float(call.get("seconds") or 0))
    return call


def _direct_fresh_opencode_finalizer(
        *, n: int, difficulty: str, rec: cr.CallRecord, phase_call: dict,
        finalize_timeout: int, reason: str, active_mcp_apps: str,
        workspace_files: list[str] | None = None,
        workspace_source_previews: dict[str, str] | None = None) -> dict | None:
    refs = list(cfg.SKETCH_OPENCODE_FRESH_FINALIZER_REFS)
    if not refs:
        return None
    prompt = _fresh_opencode_finalize_prompt(
        n=n, difficulty=difficulty, phase_call=phase_call,
        active_mcp_apps=active_mcp_apps,
        workspace_files=workspace_files,
        workspace_source_previews=workspace_source_previews,
        transcript_chars=cfg.SKETCH_OPENCODE_FRESH_FINALIZER_TRANSCRIPT_CHARS)
    failures: list[dict] = []
    system = (
        "You are a strict JSON emitter. Output only the requested JSON array. "
        "The first character must be '[' and the last character must be ']'. "
        "No analysis, no markdown, no prose before or after the JSON."
    )
    last_call: dict | None = None
    for ref in refs:
        try:
            spec = cfg.model_spec(ref)
        except Exception as e:
            failures.append({
                "ref": ref,
                "status": "config_error",
                "error": f"{type(e).__name__}: {e}",
            })
            continue
        reasoning_effort = _direct_finalizer_reasoning_effort(spec)
        final_rec = cr.http_chat(
            spec,
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            role="sketch",
            max_tokens=cfg.SKETCH_OPENCODE_FRESH_FINALIZER_MAX_TOKENS,
            timeout=finalize_timeout,
            retries=0,
            reasoning_effort=reasoning_effort,
        )
        final_call = final_rec.to_dict()
        final_call = _annotate_fresh_finalizer_call(
            final_call, rec=rec, phase_call=phase_call, reason=reason,
            finalize_timeout=finalize_timeout, fresh_finalizer=True)
        extra = final_call.setdefault("extra", {})
        extra["harness"] = "direct-chat"
        extra["fresh_finalizer_direct_chat"] = True
        extra["fresh_finalizer_model_ref"] = ref
        extra["fresh_finalizer_reasoning_effort"] = reasoning_effort
        extra["fresh_finalizer_prompt_chars"] = len(prompt)
        extra["fresh_finalizer_transcript_chars"] = (
            cfg.SKETCH_OPENCODE_FRESH_FINALIZER_TRANSCRIPT_CHARS)
        sketches = _parse_sketches(final_rec.result_text)
        extra["fresh_finalizer_parsed_sketches"] = len(sketches)
        if failures:
            extra["fresh_finalizer_prior_failures"] = failures[-3:]
        last_call = final_call
        if final_rec.ok and sketches:
            return {"ok": True, "sketches": sketches, "call": final_call}
        failures.append({
            "ref": ref,
            "status": final_rec.status,
            "seconds": final_rec.seconds,
            "chars": len(final_rec.result_text or ""),
            "parsed": len(sketches),
            "stderr_tail": (final_rec.stderr_tail or "")[-500:],
        })
    if last_call is None:
        rec0 = cr.CallRecord(role="sketch", status="server", rc=2,
                             stderr_tail="no configured fresh finalizer model")
        last_call = rec0.to_dict()
        last_call = _annotate_fresh_finalizer_call(
            last_call, rec=rec, phase_call=phase_call, reason=reason,
            finalize_timeout=finalize_timeout, fresh_finalizer=True)
    last_call.setdefault("extra", {})["fresh_finalizer_failures"] = failures[-6:]
    return {"ok": False, "sketches": [], "call": last_call}


def _direct_finalizer_reasoning_effort(spec: dict) -> str | None:
    """Disable reasoning for JSON-only formatting; hidden reasoning can eat the whole budget."""
    return "none"


def _fresh_opencode_finalize_prompt(
        *, n: int, difficulty: str, phase_call: dict,
        active_mcp_apps: str,
        workspace_files: list[str] | None = None,
        workspace_source_previews: dict[str, str] | None = None,
        transcript_chars: int | None = None) -> str:
    notes = str(phase_call.get("result_text") or "").strip()
    if not notes:
        notes = "(none)"
    _ = difficulty
    return _FRESH_FINALIZE_PROMPT.format(
        n=n,
        image_policy=_image_policy_text(),
        active_mcp_apps=active_mcp_apps,
        webharbor_sites=_webharbor_site_registry_text(),
        workspace_file_index=_format_workspace_file_index(workspace_files or []),
        workspace_source_previews=_format_workspace_source_previews(
            workspace_source_previews or {}),
        exploration_notes=notes,
        transcript_evidence=_compact_opencode_transcript_evidence(
            phase_call, max_chars=transcript_chars
            or cfg.SKETCH_OPENCODE_FRESH_FINALIZER_TRANSCRIPT_CHARS),
    )


def _compact_opencode_transcript_evidence(
        phase_call: dict, *, max_chars: int = 14000) -> str:
    """Keep visual context, initial world evidence, and recent exploration."""
    transcript = phase_call.get("session_transcript") or []
    if not isinstance(transcript, list):
        return "(none)"
    entries: list[dict] = []

    def compact(text: str, *, limit: int) -> str:
        value = " ".join(str(text or "").split())
        return value if len(value) <= limit else value[:limit].rstrip() + " ..."

    for event_index, event in enumerate(transcript):
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "text":
            text = compact(f"assistant: {part.get('text')}", limit=900)
            if text:
                entries.append({"event": event_index, "text": text, "image": False})
            continue
        if event.get("type") != "tool_use":
            continue
        tool = str(part.get("tool") or "")
        if tool not in {"read", "bash", "vision_describe_image", "describe_image"}:
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        output = state.get("output") or ""
        if not output and isinstance(state.get("metadata"), dict):
            output = state["metadata"].get("output") or ""
        label = (
            inp.get("question") or inp.get("description") or inp.get("command")
            or inp.get("filePath") or inp.get("path") or ""
        )
        is_image = "describe_image" in tool
        text = compact(f"{tool}: {label}\n{output}", limit=2200 if is_image else 1600)
        if text:
            entries.append({"event": event_index, "text": text, "image": is_image})
    if not entries:
        return "(none)"

    image_positions = [index for index, entry in enumerate(entries) if entry["image"]]
    visual_context: set[int] = set()
    for index in image_positions:
        visual_context.update(range(max(0, index - 3), min(len(entries), index + 3)))
    early = set(range(min(6, len(entries))))
    recent = set(range(max(0, len(entries) - 12), len(entries)))

    sections = (
        ("Visual evidence and nearby discovery", sorted(visual_context), 0.50),
        ("Initial world and source evidence", sorted(early - visual_context), 0.25),
        ("Most recent exploration evidence", sorted(recent - visual_context - early), 0.25),
    )
    chunks: list[str] = []
    total = 0
    seen: set[int] = set()
    for heading, indexes, share in sections:
        if not indexes or total >= max_chars:
            continue
        section_limit = max(256, int(max_chars * share))
        section_total = 0
        heading_line = f"## {heading}"
        chunks.append(heading_line)
        total += len(heading_line) + 1
        section_total += len(heading_line) + 1
        for index in indexes:
            if index in seen:
                continue
            if total >= max_chars or section_total >= section_limit:
                break
            seen.add(index)
            item = entries[index]["text"]
            room = min(max_chars - total, section_limit - section_total)
            if len(item) + 3 > room:
                if room <= 8:
                    break
                item = item[:room - 6].rstrip() + " ..."
            chunks.append(f"- {item}")
            total += len(item) + 3
            section_total += len(item) + 3
    return "\n".join(chunks) if chunks else "(none)"


def _parse_sketches(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    arrays: list[list] = []
    for match in re.finditer(r"\[", text or ""):
        try:
            value, _end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            arrays.append(value)
    if not arrays:
        return []
    for arr in reversed(arrays):
        out = []
        for s in arr:
            if isinstance(s, dict) and s.get("title") and s.get("summary"):
                s.setdefault("area", "?")
                out.append(s)
        if out:
            return out
    return []
