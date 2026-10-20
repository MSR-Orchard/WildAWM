#!/usr/bin/env python3
"""Verifier fixer: repair only verifier/ and golden.py for a defect; task.md stays frozen."""
import hashlib
import shutil
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import capability_contract as cap
from wildawm.synthesis.tasks import claude_runner as cr

TASKS_DIR = Path(__file__).resolve().parent


_PROMPT = """# Fix the VERIFIER for this task — the TASK is frozen, only the verifier may change

A faithful solve of this task was judged COMPLETED by independent judges, but
the deterministic verifier disagreed (or a pressure-probe showed the verifier
is fragile, or a check crashed). The TASK ITSELF is fine and MUST NOT change —
fix ONLY the verifier so it (a) FAILS on an unchanged world, (b) PASSES on a
faithful completed solve, and (c) never gates on a tool-stamped field.

## Defect evidence
{defects}

## Files you MAY edit (in this directory)
- `verifier/scripts/*.py` (compose SDK primitives — see contract below)
- `verifier/rubrics.md`, `verifier/verify.skill.md`
- `golden.py` (only if it writes a state a real tool could not produce)

## Files you must NOT change
- `task.md` (frozen — it is the contract with the solver)

## task.md (read-only)
{task_md}

## Capability contract (NEVER gate on an error-class stamped field)
{contract}

## Verifier runtime and current-world schema
Scripts import `from _verify_sdk import World, Result, parse_common_args,
mentions, row_summary, DBError`. The runtime diffs AFTER vs `--baseline-dir`
and provides schema-independent primitives: `new_rows`, `web_new_rows`,
`w.after.q`, `w.after.web`, and file diff methods. Generate every app-specific
query from the exact active database tables and columns in the capability
contract above. Do not call removed fixed-schema helpers such as `email_sent`,
`calendar_events`, `slack_posted`, `dm_sent`, `tickets_created`,
`slack_channel_id`, or `dm_thread_id`. NO hardcoded baseline constants and NO
hardcoded entity ids.

## Snapshots to reason against (read-only)
- clean day-N baseline: {baseline}
- judged-completed solves (AFTER): {afters}

Diff each completed AFTER snapshot against the baseline and derive checks from
the task's actual live schema and requested result. Resolve identities and
targets by natural keys. Do not introduce a fixed application schema, domain
vocabulary, or benchmark entity rule. Do not use traces, screenshots, session
captures, or workspace proxies as evidence for persisted MCP/Web state. For a
browser write, require the user-reachable AFTER-state declared by the task. If
the public UI did not persist that state, report an invalid task/write surface
instead of weakening the verifier.

Make the smallest change that repairs the concrete defect. Preserve the acceptance semantics of every unrelated task requirement and every check that already agreed across deterministic, lightweight, and heavy judges. Do not loosen a calendar, artifact, interface, visual, or content requirement merely because another deliverable's check was mis-scoped. If the defect identifies a requirement as belonging to one deliverable, change only that deliverable's check; do not make the same requirement optional elsewhere. Rubric and skill edits may clarify the frozen public contract but may not reinterpret or weaken it.

Edit the files now. When done, summarize exactly what changed and why each changed predicate was necessary for the supplied defect.
"""


def fix(task_dir: Path, *, defects: list[str], baseline_dir: Path,
        completed_after_dirs: list[str], timeout: int | None = None) -> dict:
    """Run one in-place fixer pass on verifier/ and golden.py; returns {ok, changed_files, call}."""
    timeout = timeout or cfg.LLM_HEAVY_TIMEOUT_S
    task_dir = Path(task_dir)
    contract_text = cap.generate_md(Path(baseline_dir))

    # Hashes of verifier/ and golden.py, to report what changed.
    def _digest():
        out = {}
        for p in list((task_dir / "verifier").rglob("*")) + [task_dir / "golden.py"]:
            if p.is_file() and p.name != "_verify_sdk.py":
                out[str(p.relative_to(task_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    before = _digest()
    prompt = _PROMPT.format(
        defects="\n".join(f"- {d}" for d in defects) or "(none given)",
        task_md=(task_dir / "task.md").read_text()[:6000],
        contract=contract_text[:24000],
        baseline=str(baseline_dir),
        afters=", ".join(completed_after_dirs) or "(none available)")

    # cwd=task_dir so the session edits in place; verifier execution is sandboxed anyway.
    llm = cr.LLM(model=cfg.model_spec(cfg.HEAVY_JUDGE_MODEL)["api_model"],
                 route="proxy", model_ref=cfg.HEAVY_JUDGE_MODEL)
    rec = harness.run_agent(prompt, role="fixer", claude_llm=llm, timeout=timeout,
                            cwd=str(task_dir), retries=0,
                            extra_paths=[str(baseline_dir), *completed_after_dirs])
    rec.extra.pop("trajectory", None)  # full event stream; unused by the fixer
    # Restore the pristine SDK and drop any pycache the fixer produced.
    sdk = task_dir / "verifier" / "scripts" / "_verify_sdk.py"
    shutil.copy2(TASKS_DIR / "_verify_sdk.py", sdk)
    shutil.rmtree(task_dir / "verifier" / "scripts" / "__pycache__", ignore_errors=True)
    after = _digest()
    changed = sorted([k for k in after if before.get(k) != after.get(k)]
                     + [k for k in before if k not in after])
    return {"ok": rec.ok and bool(changed), "changed_files": changed,
            "call": rec.to_dict()}
