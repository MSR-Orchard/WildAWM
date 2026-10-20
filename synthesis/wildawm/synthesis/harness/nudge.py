#!/usr/bin/env python3
"""Per-role completion reminders that resume opencode turns which stop before finishing."""

_GENERATOR = """You stopped, but the task may be UNFINISHED. This is a tool-action turn, not a discussion turn.

First check the actual files:
`ls -la /workspace/.xai_out /workspace/.xai_out/verifier /workspace/.xai_out/verifier/scripts 2>/dev/null`

Required files:
- `/workspace/.xai_out/task.md`
- `/workspace/.xai_out/reason.md`
- `/workspace/.xai_out/meta.json`
- `/workspace/.xai_out/golden.py`
- `/workspace/.xai_out/verifier/rubrics.md`
- `/workspace/.xai_out/verifier/verify.skill.md`
- at least one `/workspace/.xai_out/verifier/scripts/<check>.py`

If ANY required file is missing, your NEXT assistant response MUST create the missing deliverables immediately. Prefer one `bash` tool call with `mkdir -p` and heredocs (`cat > /workspace/.xai_out/... <<'EOF'`) that writes the complete draft files. Use the anchors and evidence already in this session. Do not keep exploring, do not run `grep`/`sqlite3`/`curl`/browser/vision checks, do not read `_verify_sdk.py`, and do not explain what you plan to do.

If all files exist, run:
`python3 /tmp/_selfcheck.py --out_dir /workspace/.xai_out --snapshot /tmp/_xai_snap`

If self-check is not `ALL GREEN`, your NEXT assistant response MUST include an edit/write/bash tool call that fixes exactly the reported issue, then rerun self-check. Continue using tools until self-check is `ALL GREEN`.

ONLY if every deliverable exists AND self-check prints `ALL GREEN`, reply with the single word DONE and call NO tools."""

_HEAVY_JUDGE = """You stopped, but you may not have RECORDED your verdict. Verify: have you used the Write/write tool to write the complete verdict JSON file to the exact path stated in your instructions? If not, finish examining the snapshots and write that file now (it is how your verdict is recorded). ONLY if the verdict file is fully written, reply with the single word DONE and call NO tools."""

_FIXER = """You stopped, but the fix may be INCOMPLETE. Verify you have actually EDITED the verifier scripts / golden to resolve the reported defect (the task.md is frozen and must NOT change). If the defect is not yet addressed, continue editing now. ONLY once the verifier/golden edits are complete, reply with the single word DONE and call NO tools."""

_WORLD_PATCH_GENERATOR = """You stopped, but the task-local world patch may be INCOMPLETE. Verify that `world_patch/manifest.json`, `world_patch/patch_data.py`, and `world_patch/README.md` exist, and that `patch_runtime.py` exists if runtime code overlay is required. If any required patch file is missing or the defect is not yet addressed, continue editing only `world_patch/` now. ONLY once the patch layout is complete and idempotent, reply with the single word DONE and call NO tools."""

# World-synthesis roles; text-deliverable roles are not nudged.
_PROFILE_CREATOR = """You stopped, but the persona may be UNFINISHED. Verify you have WRITTEN both deliverables to the exact paths stated in your instructions: the persona bible (`persona.md`) AND the accounts file (`accounts.json`), each well-formed and self-consistent (every web account has a username + password + password_bcrypt + email). If either is missing or malformed, CONTINUE using tools to write/fix them now. ONLY once both files exist and are well-formed, reply with the single word DONE and call NO tools."""

_DAY_PLANNER = """You stopped, but the day-planning stage may be UNFINISHED. Verify you have WRITTEN both files to the exact paths in your instructions: the grounded `day_N.md` plan and the valid `day_N.skills.json` mapping. If either file is missing, empty, malformed, or persona-specific values appear in a reusable activity description, CONTINUE using tools to write/fix it now. ONLY once both files exist and are complete, reply with the single word DONE and call NO tools."""

_SKILL_CREATOR = """You stopped, but the reusable event skill may be UNFINISHED. Verify that `skill.md` exists at the exact staged path in your instructions and contains the required H1 plus Description, Parameters, Scenarios involved, Consistency checks, Preconditions, and Possible follow-ups sections. Remove persona-specific names, ids, dates, account values, and synthesized-world paths. If the file is missing or incomplete, CONTINUE using tools to write/fix it now. ONLY once the reusable skill is complete, reply with the single word DONE and call NO tools."""

_APP_CREATOR = """You stopped, but the reusable MCP service implementation may be UNFINISHED. Verify that `models.py`, `schemas.py`, `handlers.py`, `run.py`, and `capability_rules.json` exist at the exact staged path. Run `python3 -m py_compile *.py` and parse `capability_rules.json`. The package must define `run.create_app(db_path)`, use only its supplied database, expose useful deterministic FastAPI operations with unique operation IDs, and contain no persona-specific values. If any file or check is incomplete, CONTINUE using tools to fix it now. ONLY once the reusable service package is complete, reply with the single word DONE and call NO tools."""

_DAY_IMPLEMENTER = """You stopped, but the day implementation may be UNFINISHED. Verify you have WRITTEN the deterministic replay entry `delta.sh` (plus any `*.py` sub-scripts it orchestrates), the machine-readable `produces.json`, the `state_delta.md` summary, and — if this day introduces or patches code — the `overlay/` files (apps/ and/or webharbor/ + capability_rules.json) to the exact paths stated in your instructions.

Before replying DONE, run these local self-checks against the exact day_N deliverable directory:
- `bash -n delta.sh`
- `python3 -m py_compile *.py`
- `python3 -m json.tool produces.json >/dev/null`

If any required file is missing or any self-check errors, CONTINUE using tools to write/fix them now. ONLY once the full day_N deliverable set exists AND those self-checks pass, reply with the single word DONE and call NO tools."""

# Roles absent here are not nudged: their deliverable is the final text.
REMINDERS = {
    "generator": _GENERATOR,
    "heavy_judge": _HEAVY_JUDGE,
    "fixer": _FIXER,
    "world_patch_generator": _WORLD_PATCH_GENERATOR,
    "profile_creator": _PROFILE_CREATOR,
    "day_planner": _DAY_PLANNER,
    "skill_creator": _SKILL_CREATOR,
    "app_creator": _APP_CREATOR,
    "day_implementer": _DAY_IMPLEMENTER,
}


def reminder_for(role: str) -> str | None:
    """The completion reminder for a role, or None if the role is not nudged."""
    return REMINDERS.get(role)
