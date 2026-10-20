#!/usr/bin/env python3
"""Run a task's generated verifier scripts in the sandbox and build blind judge evidence."""
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from simpleArgParser import parse_args
from wildawm import const_config as cfg
from wildawm.synthesis.tasks import sandbox

VERDICTS = ("completed", "failed", "world_imperfect")


@dataclass
class VerifierArgs:
    verifier_dir: str
    workspace: str
    data_dir: str
    baseline_dir: str
    webharbor_dir: str | None = None
    no_sandbox: bool = False


def norm3(v: str) -> str:
    """Map judge/script vocabulary to completed/failed/world_imperfect, or infra if unknown."""
    v = (v or "").strip().lower()
    if not v:
        return "infra"
    if "world" in v or "imperfect" in v:
        return "world_imperfect"
    # "partially_completed" contains "complet", so check partial/fail first.
    if "partial" in v or "fail" in v or v in ("incomplete", "none", "agent_fail"):
        return "failed"
    if "complet" in v or v in ("pass", "passed", "success", "ok"):
        return "completed"
    return "infra"


def _parse_result_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT_JSON="):
            try:
                return json.loads(line[len("RESULT_JSON="):])
            except json.JSONDecodeError:
                return None
    return None


def run_verifier(verifier_dir: Path, workspace: Path, data_dir: Path,
                 baseline_dir: Path, webharbor_dir: Path | None = None,
                 *, baseline_webharbor_dir: Path | None = None,
                 use_sandbox: bool = True,
                 timeout: int = cfg.LOCAL_SCRIPT_TIMEOUT_S) -> dict:
    """Run every scripts/*.py; return the aggregate verdict, per-script results and script_errors."""
    verifier_dir = Path(verifier_dir)
    scripts = sorted((verifier_dir / "scripts").glob("*.py"))
    scripts = [s for s in scripts if s.name != "_verify_sdk.py"]
    per: list[dict] = []
    script_errors: list[str] = []
    for s in scripts:
        try:
            if use_sandbox:
                argv = sandbox.check_argv(
                    webharbor=bool(webharbor_dir),
                    baseline_webharbor=bool(baseline_webharbor_dir))
                r = sandbox.run_py(s, argv, workspace=Path(workspace),
                                   data_dir=Path(data_dir),
                                   baseline_dir=Path(baseline_dir),
                                   webharbor_dir=Path(webharbor_dir) if webharbor_dir else None,
                                   baseline_webharbor_dir=Path(baseline_webharbor_dir)
                                   if baseline_webharbor_dir else None,
                                   timeout=timeout)
            else:
                cmd = [sys.executable, str(s), "--workspace", str(workspace),
                       "--data-dir", str(data_dir),
                       "--baseline-dir", str(baseline_dir)]
                if webharbor_dir:
                    cmd += ["--webharbor-dir", str(webharbor_dir)]
                if baseline_webharbor_dir:
                    cmd += ["--baseline-webharbor-dir", str(baseline_webharbor_dir)]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout)
            out, err, rc = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "script timeout", 124

        rj = _parse_result_json(out)
        if rj is None:
            # No contract output: a verifier defect, not a verdict.
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


def aggregate(verdicts: list[str]) -> str:
    """Aggregate real verdicts: any world_imperfect wins, all completed is completed, else failed."""
    real = [v for v in verdicts if v in VERDICTS]
    if not real:
        return "failed"
    if any(v == "world_imperfect" for v in real):
        return "world_imperfect"
    if all(v == "completed" for v in real):
        return "completed"
    return "failed"


_MARKER = re.compile(r"^(RESULT_JSON=|EVIDENCE_JSON=|VERDICT:)")


def build_lightweight_evidence(rule_result: dict) -> list[dict]:
    """Blind evidence for the lightweight judge: facts only, no per-check booleans or verdicts."""
    ev = []
    for s in rule_result.get("scripts", []):
        lines = [l for l in (s.get("stdout") or "").splitlines()
                 if l.strip() and not _MARKER.match(l)]
        ev.append({"script": s.get("name"),
                   "extracted": s.get("details", {}),
                   "world_errors": s.get("errors", {}),
                   "output": "\n".join(lines)})
    return ev


def apply_golden(golden_py: Path, workspace: Path, data_dir: Path,
                 webharbor_dir: Path | None, baseline_dir: Path | None = None,
                 *, use_sandbox: bool = True,
                 timeout: int = cfg.LOCAL_SCRIPT_TIMEOUT_S) -> dict:
    """Run golden.py in the sandbox to write the passing state into scratch dirs."""
    golden_py = Path(golden_py)
    try:
        if use_sandbox:
            argv = sandbox.golden_argv(webharbor=bool(webharbor_dir),
                                       baseline=bool(baseline_dir))
            r = sandbox.run_py(golden_py, argv, workspace=Path(workspace),
                               data_dir=Path(data_dir),
                               baseline_dir=Path(baseline_dir) if baseline_dir else None,
                               webharbor_dir=Path(webharbor_dir) if webharbor_dir else None,
                               timeout=timeout)
        else:
            cmd = [sys.executable, str(golden_py), "--workspace", str(workspace),
                   "--data-dir", str(data_dir)]
            if webharbor_dir:
                cmd += ["--webharbor-dir", str(webharbor_dir)]
            if baseline_dir:
                cmd += ["--baseline-dir", str(baseline_dir)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "rc": r.returncode,
                "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 124, "stdout": "", "stderr": "golden timeout"}


if __name__ == "__main__":
    a = parse_args(VerifierArgs)
    res = run_verifier(Path(a.verifier_dir), Path(a.workspace), Path(a.data_dir),
                       Path(a.baseline_dir),
                       Path(a.webharbor_dir) if a.webharbor_dir else None,
                       use_sandbox=not a.no_sandbox)
    print(json.dumps(res, indent=2)[:5000])
