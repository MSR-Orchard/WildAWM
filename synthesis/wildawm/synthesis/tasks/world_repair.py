#!/usr/bin/env python3
"""Handle world_imperfect verdicts: reproduce on a clean snapshot, then classify the fault."""
import json
from pathlib import Path

from wildawm.synthesis import harness
from wildawm import const_config as cfg
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import verifier as vf

DISPOSITIONS = ("confirmed_world_fault", "not_world_fault", "ambiguous")


def reproduce_on_clean(verifier_dir: Path, clean_snapshot: Path, *,
                       use_sandbox: bool = True) -> dict:
    """Verify a clean snapshot as both after and baseline; world errors here mean a broken world."""
    wh = clean_snapshot / "webharbor"
    rd = vf.run_verifier(verifier_dir, clean_snapshot / "workspace",
                         clean_snapshot / "data", clean_snapshot,
                         wh if wh.is_dir() else None,
                         use_sandbox=use_sandbox)
    world_broken = (rd["verdict"] == "world_imperfect") or bool(rd["script_errors"])
    return {"world_broken": world_broken, "verdict": rd["verdict"],
            "n_scripts": rd.get("n_scripts", 0),
            "script_errors": rd["script_errors"],
            "world_errors": {s["name"]: s["errors"] for s in rd["scripts"]
                             if s.get("errors")}}


def classify_world_fault(profile: str, day: int, slug: str, evidence: dict,
                         clean_snapshot: Path, verifier_dir: Path, *,
                         use_sandbox: bool = True) -> dict:
    """Classify a suspected world defect as confirmed_world_fault, not_world_fault or ambiguous."""
    try:
        repro = reproduce_on_clean(verifier_dir, clean_snapshot, use_sandbox=use_sandbox)
    except Exception as e:  # noqa: BLE001
        return {"disposition": "ambiguous", "profile": profile, "day": day,
                "slug": slug, "evidence": evidence,
                "reproduce": {"error": f"{type(e).__name__}: {e}"}}
    if not repro.get("n_scripts"):
        return {"disposition": "ambiguous", "profile": profile, "day": day,
                "slug": slug, "evidence": evidence, "reproduce": repro,
                "note": "no deterministic verifier scripts were available for the reproduce gate"}
    if repro["world_broken"]:
        return {"disposition": "confirmed_world_fault", "profile": profile,
                "day": day, "slug": slug, "evidence": evidence,
                "reproduce": repro}
    return {"disposition": "not_world_fault", "profile": profile, "day": day,
            "slug": slug, "evidence": evidence, "reproduce": repro,
            "note": "checks are clean on a fresh patched baseline; treat this as agent failure, judge mistake, or verifier defect"}


_DIAGNOSTIC_PROMPT = """# Diagnose suspected XAI world defect

You are a diagnostic role. Do not modify files. Inspect the task assets, verifier output, evidence, and clean snapshot paths. Decide whether this task is impossible because of a world defect.

Return exactly one line:
WORLD_DIAGNOSIS: confirmed_world_fault | not_world_fault | ambiguous

Then include the smallest concrete evidence. Do not propose or apply a patch.

Evidence JSON:
{evidence}

Clean snapshot: {clean_snapshot}
"""


def diagnose_ambiguous(task_dir: Path, evidence: dict, clean_snapshot: Path,
                       *, timeout: int | None = None) -> dict:
    """Small read-only diagnostic agent for ambiguous reproduce results."""
    timeout = timeout or cfg.LLM_MEDIUM_TIMEOUT_S
    llm = cr.LLM(model=cfg.model_spec(cfg.HEAVY_JUDGE_MODEL)["api_model"],
                 route="proxy", model_ref=cfg.HEAVY_JUDGE_MODEL)
    rec = harness.run_agent(
        _DIAGNOSTIC_PROMPT.format(
            evidence=json.dumps(evidence, indent=2, ensure_ascii=False),
            clean_snapshot=str(clean_snapshot)),
        role="world_diagnostic",
        claude_llm=llm,
        timeout=timeout,
        cwd=str(task_dir),
        retries=0,
        extra_paths=[str(clean_snapshot)],
        allowed_tools="Read,Glob,Grep,Bash",
    )
    text = rec.result_text or ""
    low = text.lower()
    if "confirmed_world_fault" in low:
        disp = "confirmed_world_fault"
    elif "not_world_fault" in low:
        disp = "not_world_fault"
    else:
        disp = "ambiguous"
    return {"disposition": disp, "diagnostic": rec.to_dict(),
            "diagnostic_text": text}
