#!/usr/bin/env python3
"""WildAWM verifier for Harbor: run the task's check scripts and write the reward.

completed -> reward 1, failed -> reward 0, world_imperfect or script errors -> no reward file;
the full result is always written to /logs/verifier/l1_verdict.json.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

VERDICTS = ("completed", "failed", "world_imperfect")
SCRIPT_TIMEOUT_S = 180          # cfg.LOCAL_SCRIPT_TIMEOUT_S
_EXCLUDE = {"_verify_sdk.py", "verify_wrapper.py", "__init__.py"}


def norm3(v: str) -> str:
    """Same mapping as norm3 in wildawm/synthesis/tasks/verifier.py."""
    v = (v or "").strip().lower()
    if not v:
        return "infra"
    if "world" in v or "imperfect" in v:
        return "world_imperfect"
    if "partial" in v or "fail" in v or v in ("incomplete", "none", "agent_fail"):
        return "failed"
    if "complet" in v or v in ("pass", "passed", "success", "ok"):
        return "completed"
    return "infra"


def aggregate(verdicts: list[str]) -> str:
    """Same rule as aggregate in wildawm/synthesis/tasks/verifier.py."""
    real = [v for v in verdicts if v in VERDICTS]
    if not real:
        return "failed"
    if any(v == "world_imperfect" for v in real):
        return "world_imperfect"
    if all(v == "completed" for v in real):
        return "completed"
    return "failed"


def _parse_result_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT_JSON="):
            try:
                return json.loads(line[len("RESULT_JSON="):])
            except json.JSONDecodeError:
                return None
    return None


def run_verifier(tests_dir: Path, workspace: Path, data_dir: Path,
                 baseline_dir: Path, webharbor_dir: Path,
                 baseline_webharbor_dir: Path) -> dict:
    scripts = sorted(p for p in tests_dir.glob("*.py")
                     if p.name not in _EXCLUDE and not p.name.startswith("__"))
    per: list[dict] = []
    script_errors: list[str] = []
    for s in scripts:
        cmd = [
            sys.executable, str(s),
            "--workspace", str(workspace),
            "--data-dir", str(data_dir),
            "--baseline-dir", str(baseline_dir),
            "--webharbor-dir", str(webharbor_dir),
            "--baseline-webharbor-dir", str(baseline_webharbor_dir),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SCRIPT_TIMEOUT_S)
            out, err, rc = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "script timeout", 124
        rj = _parse_result_json(out)
        if rj is None:
            script_errors.append(s.name)
            per.append({"name": s.name, "verdict": "script_error",
                        "checks": {}, "details": {}, "errors": {},
                        "stdout": out, "stderr": err, "rc": rc})
            continue
        per.append({"name": s.name, "verdict": norm3(rj.get("verdict")),
                    "checks": rj.get("checks", {}),
                    "details": rj.get("details", {}),
                    "errors": rj.get("errors", {}),
                    "stdout": out, "stderr": err, "rc": rc})
    verdict = aggregate([p["verdict"] for p in per])
    return {"verdict": verdict, "scripts": per, "n_scripts": len(per),
            "script_errors": script_errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", default="/tests")
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--data-dir", default="/world/data")
    parser.add_argument("--baseline-dir", default="/baseline")
    parser.add_argument("--webharbor-dir", default="/tmp/verify-webharbor")
    parser.add_argument("--baseline-webharbor-dir", default="/baseline/webharbor")
    parser.add_argument("--out-dir", default="/logs/verifier")
    parser.add_argument("--collect-script", default=None,
                        help="run this script first to snapshot the served "
                             "WebHarbor DBs into --webharbor-dir")
    args = parser.parse_args()

    tests_dir = Path(args.tests_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.collect_script:
        subprocess.run(["bash", args.collect_script, args.webharbor_dir],
                       check=True)

    result = run_verifier(
        tests_dir=tests_dir,
        workspace=Path(args.workspace),
        data_dir=Path(args.data_dir),
        baseline_dir=Path(args.baseline_dir),
        webharbor_dir=Path(args.webharbor_dir),
        baseline_webharbor_dir=Path(args.baseline_webharbor_dir),
    )

    for s in result["scripts"]:
        (out_dir / f"{s['name']}.stdout").write_text(s.get("stdout", ""))
    slim = {
        "verdict": result["verdict"],
        "n_scripts": result["n_scripts"],
        "script_errors": result["script_errors"],
        "scripts": [
            {"name": s["name"], "verdict": s["verdict"], "checks": s["checks"],
             "details": s["details"], "errors": s["errors"], "rc": s["rc"]}
            for s in result["scripts"]
        ],
    }
    (out_dir / "l1_verdict.json").write_text(
        json.dumps(slim, indent=2, ensure_ascii=False) + "\n")

    # Any verifier defect means no reward file, so Harbor records a trial error.
    per_script_defects = [s["name"] for s in result["scripts"]
                          if s["verdict"] not in VERDICTS]
    if (result["verdict"] in ("completed", "failed")
            and not result["script_errors"] and not per_script_defects):
        reward = {"reward": 1 if result["verdict"] == "completed" else 0}
        for s in result["scripts"]:
            for check_id, ok in sorted(s["checks"].items()):
                reward[f"{s['name']}:{check_id}"] = 1 if ok else 0
        (out_dir / "reward.json").write_text(
            json.dumps(reward, indent=2) + "\n")
        print(f"L1 verdict: {result['verdict']} -> reward "
              f"{reward['reward']}")
        return 0

    print(f"L1 verdict: {result['verdict']} "
          f"(script_errors={result['script_errors']}, "
          f"defects={per_script_defects}) -> NO reward file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
