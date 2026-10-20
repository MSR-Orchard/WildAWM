#!/usr/bin/env python3
"""Export accepted tasks into a self-contained named set under `tasks_xai/sets/<name>/`."""
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from simpleArgParser import parse_args

from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import tasks_index as ti

# Public task contract (validated by artifact_lint) plus the golden solution.
TASK_FILES = ("task.md", "meta.json", "reason.md", "reason_helper.md", "golden.py")
TASK_DIRS = ("verifier",)
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


@dataclass
class Args:
    name: str = "task-set"
    since: str = ""  # optional lower bound on accept time; empty exports every accepted task
    until: str = ""  # optional upper bound, so a named set stays reproducible while the run keeps accepting
    profile: str = ""  # optional single-profile filter
    sets_root: str = ""  # defaults to <TASKS_OUT>/sets
    overwrite: bool = False


def flow_legs(meta: dict) -> list[str]:
    """The interface legs of a task's flow, e.g. ['cli', 'mcp', 'web', 'cli']."""
    legs = [re.split(r"[:\s]", leg.strip())[0].lower()
            for leg in str(meta.get("interface_flow", "")).split("->")]
    return [leg for leg in legs if leg in ("cli", "mcp", "web")]


def select_tasks(since_ts: float, profile: str | None, until_ts: float | None = None) -> list[Path]:
    """Accepted task dirs whose verdict landed in [`since_ts`, `until_ts`], newest last."""
    chosen = []
    for task_dir in ti.accepted_dirs(profile or None):
        mtime = (task_dir / "_synthesis_report.json").stat().st_mtime
        if mtime >= since_ts and (until_ts is None or mtime <= until_ts):
            chosen.append((mtime, task_dir))
    return [d for _, d in sorted(chosen)]


def copy_task(task_dir: Path, dest: Path) -> list[str]:
    """Copy one task's artifacts into `dest`, returning the names actually copied."""
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in TASK_FILES:
        src = task_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
            copied.append(name)
    for name in TASK_DIRS:
        src = task_dir / name
        if src.is_dir():
            shutil.copytree(src, dest / name, ignore=COPY_IGNORE, dirs_exist_ok=True)
            copied.append(name + "/")
    return copied


def task_record(task_dir: Path, copied: list[str]) -> dict:
    meta = load_jsonc(task_dir / "meta.json")
    report = task_dir / "_synthesis_report.json"
    verdict = ti.verdict_of(report)
    legs = flow_legs(meta)
    day_match = re.match(r"^day(\d+)$", task_dir.parent.name)
    return {"slug": task_dir.name,
            "profile": task_dir.parent.parent.name,
            "day": int(day_match.group(1)) if day_match else None,
            "difficulty": meta.get("difficulty"),
            "verdict": verdict,
            "title": meta.get("title"),
            "area": meta.get("area"),
            "archetype": meta.get("archetype"),
            "interfaces": meta.get("interfaces", []),
            "interface_flow": meta.get("interface_flow"),
            "flow_shape": "->".join(legs),
            "hops": len(legs),
            "sites": meta.get("sites", []),
            "mcp_domains": meta.get("mcp_domains", []),
            "accepted_at": cfg.report_stamp(report.stat().st_mtime),
            "source": str(task_dir.relative_to(cfg.PROJECT_ROOT)),
            "artifacts": copied}


def summarize(records: list[dict]) -> dict:
    return {"count": len(records),
            "by_difficulty": dict(Counter(r["difficulty"] for r in records).most_common()),
            "by_verdict": dict(Counter(r["verdict"] for r in records).most_common()),
            "by_profile": dict(Counter(r["profile"] for r in records).most_common()),
            "by_start_interface": dict(Counter(r["flow_shape"].split("->")[0] for r in records
                                               if r["flow_shape"]).most_common()),
            "hop_histogram": dict(sorted(Counter(r["hops"] for r in records).items())),
            "distinct_flow_shapes": len({r["flow_shape"] for r in records if r["flow_shape"]}),
            "flow_shapes": dict(Counter(r["flow_shape"] for r in records if r["flow_shape"]).most_common())}


def write_readme(set_dir: Path, args: Args, records: list[dict], stats: dict) -> None:
    lines = [f"# {args.name}",
             "",
             f"{stats['count']} accepted cross-agent-interface tasks"
             + (f" accepted at or after {args.since}" if args.since else "")
             + f". Timestamps are in {cfg.REPORT_TZ}.",
             "",
             "## What is here",
             "",
             "One directory per task, named by its slug. Each holds only the task itself:",
             "",
             "- `task.md` — the public task statement given to a solver",
             "- `meta.json` — title, difficulty, interfaces, `interface_flow`, grounded anchors, `image_grounding`, and `world_digest` (per-db content hashes identifying the world state the task expects)",
             "- `reason.md` / `reason_helper.md` — the reasoning trace and its helper notes",
             "- `golden.py` — the golden solution",
             "- `verifier/` — `rubrics.md`, `verify.skill.md` and the verifier scripts",
             "",
             "`manifest.json` indexes every task with its provenance (source path, verdict, accept time) and the set-level diversity stats.",
             "",
             "## What is deliberately not here",
             "",
             "No worlds. `_synthesis_log/` (world snapshots and solver trajectories, gigabytes per slot), `_synthesis_report.json` (the synthesis audit record — its verdict and reason are folded into `manifest.json`) and `judge_trajectories/` were all left in the source tree. Use each task's `source` field in the manifest to get back to them.",
             "",
             "## Composition",
             "",
             f"- Difficulty: {', '.join(f'{k} {v}' for k, v in stats['by_difficulty'].items())}",
             f"- Verdict: {', '.join(f'{k} {v}' for k, v in stats['by_verdict'].items())}",
             f"- Profiles: {', '.join(f'{k} {v}' for k, v in stats['by_profile'].items())}",
             f"- Starting interface: {', '.join(f'{k} {v}' for k, v in stats['by_start_interface'].items())}",
             f"- Hop histogram: {', '.join(f'{k} hops x{v}' for k, v in stats['hop_histogram'].items())}",
             f"- Distinct flow shapes: {stats['distinct_flow_shapes']}",
             "",
             "## Tasks",
             "",
             "| slug | difficulty | hops | flow | title |",
             "| --- | --- | --- | --- | --- |"]
    for r in records:
        title = (r["title"] or "").replace("|", "\\|")
        lines.append(f"| `{r['slug']}` | {r['difficulty']} | {r['hops']} | {r['flow_shape']} | {title} |")
    (set_dir / "README.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    args = parse_args(Args)
    since_ts = cfg.parse_report_time(args.since).timestamp() if args.since else 0.0
    until_ts = cfg.parse_report_time(args.until).timestamp() if args.until else None
    sets_root = Path(args.sets_root) if args.sets_root else cfg.TASKS_OUT / "sets"
    set_dir = sets_root / args.name

    task_dirs = select_tasks(since_ts, args.profile, until_ts)
    if not task_dirs:
        raise SystemExit("no accepted tasks" + (f" at or after {args.since}" if args.since else "")
                         + (f" for profile {args.profile}" if args.profile else ""))

    pool_before = len(ti.accepted_dirs(None))
    set_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for task_dir in task_dirs:
        dest = set_dir / task_dir.name
        if dest.exists():
            if not args.overwrite:
                raise SystemExit(f"{dest} already exists; pass --overwrite true to replace it")
            shutil.rmtree(dest)
        records.append(task_record(task_dir, copy_task(task_dir, dest)))
        print(f"  {task_dir.name:22s} <- {records[-1]['source']}")

    stats = summarize(records)
    manifest = {"name": args.name,
                "created_at": cfg.report_stamp(),
                "timezone": str(cfg.REPORT_TZ),
                "source_run_start": cfg.report_stamp(since_ts) if args.since else None,
                "accepted_until": cfg.report_stamp(until_ts) if until_ts else None,
                "contents": "task artifacts only — no worlds; see README.md",
                "task_files": list(TASK_FILES), "task_dirs": list(TASK_DIRS),
                "stats": stats, "tasks": records}
    (set_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    write_readme(set_dir, args, records, stats)

    pool_after = len(ti.accepted_dirs(None))
    print(f"\nexported {len(records)} task(s) -> {set_dir}")
    print(f"accepted pool unchanged: {pool_before} -> {pool_after}"
          if pool_before == pool_after else
          f"WARNING: accepted pool moved {pool_before} -> {pool_after}")
    print("stats:", stats["by_difficulty"], "| shapes:", stats["distinct_flow_shapes"],
          "| hops:", stats["hop_histogram"], "| starts:", stats["by_start_interface"])


if __name__ == "__main__":
    main()
