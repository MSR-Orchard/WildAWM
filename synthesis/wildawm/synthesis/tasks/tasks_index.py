#!/usr/bin/env python3
"""Read-side helpers over the accepted-task store (tasks_xai/<profile>/dayN/<slug>/)."""
import re
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import artifact_lint as alint

ACCEPTED = ("accept", "accept_after_fix")


def report_path(profile: str, day: int, slug: str) -> Path:
    return cfg.TASKS_OUT / profile / f"day{day}" / slug / "_synthesis_report.json"


def verdict_of(report_p: Path) -> str | None:
    try:
        return load_jsonc(report_p).get("verdict")
    except (OSError, ValueError):
        return None


def is_accepted(report_p: Path) -> bool:
    return verdict_of(report_p) in ACCEPTED


def quality_prior_problems(task_dir: Path) -> list[str]:
    """Current quality checks that decide whether an accepted task may be a diversity prior."""
    problems: list[str] = []
    for fn in (
        alint.xai_meta_contract_problems,
        alint.public_task_length_problems,
        alint.public_task_leakage_problems,
        alint.public_answer_leak_problems,
        alint.public_visual_decision_problems,
        alint.public_cli_code_requirement_problems,
        alint.verifier_sdk_api_problems,
        alint.workspace_input_contract_problems,
        alint.verifier_state_surface_problems,
    ):
        problems.extend(fn(task_dir))
    return problems


def is_quality_prior(task_dir: Path) -> bool:
    return not quality_prior_problems(task_dir)


def prior_entry(task_dir: Path) -> dict:
    """Compact diversity-prior record for one accepted task dir."""
    meta = {}
    mp = task_dir / "meta.json"
    if mp.exists():
        try:
            meta = load_jsonc(mp)
        except ValueError:
            pass
    task_md = task_dir / "task.md"
    profile = task_dir.parent.parent.name if len(task_dir.parts) >= 3 else ""
    day = None
    m = re.match(r"^day(\d+)$", task_dir.parent.name)
    if m:
        day = int(m.group(1))
    image_grounding = meta.get("image_grounding")
    sites = meta.get("sites") or []
    if not sites and isinstance(image_grounding, dict) and image_grounding.get("site"):
        sites = [image_grounding.get("site")]
    summary = meta.get("summary")
    if not summary and task_md.exists():
        summary = task_md.read_text()[:400]
    return {"title": meta.get("title", task_dir.name),
            "status": "accepted",
            "lock_id": None,
            "owner": None,
            "profile": profile,
            "day": day,
            "slug": meta.get("slug", task_dir.name),
            "area": meta.get("area", "?"),
            "archetype": meta.get("archetype", "?"),
            "difficulty": meta.get("difficulty", "?"),
            "interfaces": meta.get("interfaces", []),
            "interface_flow": meta.get("interface_flow", "?"),
            "summary": summary or "",
            "sites": sites,
            "mcp_domains": meta.get("mcp_domains", []),
            "image_grounding": image_grounding,
            "anchors": meta.get("grounded_anchors") or meta.get("anchors") or [],
            "source_dir": str(task_dir)}


def gather_global_priors() -> list[dict]:
    """All accepted tasks across every profile/day under tasks_xai."""
    return [prior_entry(p) for p in accepted_dirs(None) if is_quality_prior(p)]


def accepted_dirs(profile: str | None) -> list[Path]:
    base = cfg.TASKS_OUT if profile is None else cfg.TASKS_OUT / profile
    out: list[Path] = []
    if base.exists():
        profile_dirs = sorted(p for p in base.iterdir() if p.is_dir()) if profile is None else [base]
        for prof in profile_dirs:
            for dd in sorted(prof.glob("day*")):
                out += [t for t in sorted(dd.iterdir())
                        if t.is_dir() and is_accepted(t / "_synthesis_report.json")]
    return out
