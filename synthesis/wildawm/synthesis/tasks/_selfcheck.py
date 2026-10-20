#!/usr/bin/env python3
"""In-container self-check: clean copy must fail, golden copy must pass, style must be clean."""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from simpleArgParser import parse_args

# The lightweight judge must not inherit the solver's reasoning-effort setting.
os.environ["AWM_REASONING_EFFORT"] = os.environ.get(
    "AWM_LIGHTWEIGHT_JUDGE_REASONING_EFFORT", "")

from wildawm import const_config as cfg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_judge as lj                                                 # noqa: E402  (copied beside)
import capability_contract as cap                                      # noqa: E402  (copied beside)
import artifact_lint as alint                                          # noqa: E402  (copied beside)


def norm3(v: str) -> str:
    v = (v or "").strip().lower()
    if "world" in v or "imperfect" in v:
        return "world_imperfect"
    if "complet" in v:
        return "completed"
    if "partial" in v or "fail" in v:
        return "failed"
    return "infra"


def _copy_snap(snap: Path, dest: Path) -> dict:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    out = {}
    for d in ("workspace", "data", "webharbor"):
        s = snap / d
        if s.exists():
            shutil.copytree(s, dest / d, symlinks=True)
            out[d] = dest / d
    return out


def _run_scripts(vdir: Path, ws, data, wh, baseline: Path) -> list[dict]:
    per = []
    for s in sorted((vdir / "scripts").glob("*.py")):
        if s.name == "_verify_sdk.py":
            continue
        cmd = ["python3", str(s), "--workspace", str(ws), "--data-dir", str(data),
               "--baseline-dir", str(baseline)]
        if wh:
            cmd += ["--webharbor-dir", str(wh)]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=cfg.LOCAL_SCRIPT_TIMEOUT_S)
            out, err, rc = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "timeout", 124
        rj = None
        for line in reversed(out.splitlines()):
            if line.startswith("RESULT_JSON="):
                try:
                    rj = json.loads(line[len("RESULT_JSON="):])
                except json.JSONDecodeError:
                    rj = None
                break
        per.append({"name": s.name,
                    "verdict": norm3(rj.get("verdict")) if rj else "script_error",
                    "checks": (rj or {}).get("checks", {}),
                    "details": (rj or {}).get("details", {}),
                    "errors": (rj or {}).get("errors", {}),
                    "stdout": out, "stderr": err, "rc": rc})
    return per


def _evidence(per: list[dict]) -> list[dict]:
    marker = re.compile(r"^(RESULT_JSON=|VERDICT:)")
    return [{"script": s["name"], "extracted": s["details"],
             "world_errors": s["errors"],
             "output": "\n".join(l for l in s["stdout"].splitlines()
                                 if l.strip() and not marker.match(l))}
            for s in per]


_BASELINE_CONST = re.compile(r"BASELINE[_A-Z0-9]*\s*=\s*\d+")
_ID_GT_CONST = re.compile(r"\bid\b\s*>\s*\d{1,7}\b", re.I)
_GOLDEN_HARDCODED_PATH = re.compile(
    r"(?<![\w.-])(?:/workspace(?:/|\b)|/tmp/_xai_snap(?:/|\b))")


@dataclass
class SelfcheckArgs:
    out_dir: str
    snapshot: str
    scratch: str = "/tmp/_xai_selfcheck"
    world_patch: str = ""


def _task_dir_for_lint(out_dir: Path, scratch: Path) -> Path:
    """Run style checks through a slug-named path, since .xai_out cannot match meta.slug."""
    if out_dir.name != ".xai_out":
        return out_dir
    meta_path = out_dir / "meta.json"
    try:
        meta = alint.loads_jsonc(meta_path.read_text(errors="replace"))
    except Exception:
        return out_dir
    raw_slug = meta.get("slug") if isinstance(meta, dict) else None
    slug = raw_slug.strip() if isinstance(raw_slug, str) else ""
    if not slug or slug.startswith(".") or "/" in slug or "\\" in slug:
        return out_dir
    alias_root = scratch / "_taskdir_alias"
    alias = alias_root / slug
    try:
        alias_root.mkdir(parents=True, exist_ok=True)
        if alias.is_symlink() or alias.is_file():
            alias.unlink()
        elif alias.exists():
            shutil.rmtree(alias)
        alias.symlink_to(out_dir, target_is_directory=True)
        return alias
    except OSError:
        return out_dir


def _patched_snap(snap: Path, patch_dir: Path, scratch: Path) -> Path:
    patched = scratch / "patched_baseline"
    copied = _copy_snap(snap, patched)
    script = patch_dir / "patch_data.py"
    if not script.exists():
        raise RuntimeError(f"world_patch missing patch_data.py: {script}")
    cmd = [sys.executable, str(script), "--instance-dir", str(patched),
           "--workspace", str(copied["workspace"]),
           "--data-dir", str(copied["data"])]
    if copied.get("webharbor"):
        cmd += ["--webharbor-dir", str(copied["webharbor"])]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=cfg.LOCAL_SCRIPT_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"world_patch/patch_data.py failed: {r.stderr}")
    return patched


def style_problems(out_dir: Path, workspace_dir: Path | None = None) -> list[str]:
    probs = []
    probs.extend(alint.xai_meta_contract_problems(out_dir))
    probs.extend(alint.public_task_length_problems(out_dir))
    probs.extend(alint.public_task_leakage_problems(out_dir))
    probs.extend(alint.public_answer_leak_problems(out_dir))
    probs.extend(alint.public_visual_decision_problems(out_dir))
    probs.extend(alint.public_cli_code_requirement_problems(out_dir))
    probs.extend(alint.verifier_sdk_api_problems(out_dir))
    probs.extend(alint.workspace_input_contract_problems(out_dir))
    probs.extend(alint.workspace_existing_claim_problems(out_dir, workspace_dir))
    probs.extend(alint.world_patch_layout_problems(out_dir))
    probs.extend(alint.verifier_state_surface_problems(out_dir))
    golden = out_dir / "golden.py"
    if golden.exists():
        for lineno, line in enumerate(golden.read_text(errors="replace").splitlines(), 1):
            if _GOLDEN_HARDCODED_PATH.search(line):
                probs.append(
                    f"golden.py:{lineno}: hardcoded container path in golden.py: "
                    "use --workspace/--data-dir/--webharbor-dir/--baseline-dir"
                )
    for s in sorted((out_dir / "verifier" / "scripts").glob("*.py")):
        if s.name == "_verify_sdk.py":
            continue
        text = s.read_text()
        if "_verify_sdk" not in text:
            probs.append(f"{s.name}: does not import the SDK")
        for pat, why in ((_BASELINE_CONST, "hardcoded BASELINE constant"),
                         (_ID_GT_CONST, "hardcoded id>N comparison")):
            for m in pat.finditer(text):
                probs.append(f"{s.name}: {why}: `{m.group(0)}` — use the "
                             f"--baseline-dir diff primitives instead")
    for v in cap.lint_task_dir(out_dir):
        if v.severity == "error":
            probs.append(f"CONTRACT {v.where}: {v.why} :: {v.line}")
    return probs


def main() -> int:
    a = parse_args(SelfcheckArgs)
    out, snap, scratch = Path(a.out_dir), Path(a.snapshot), Path(a.scratch)
    out = _task_dir_for_lint(out, scratch)
    if a.world_patch:
        snap = _patched_snap(snap, Path(a.world_patch), scratch)
    vdir, golden = out / "verifier", out / "golden.py"
    task_md = (out / "task.md").read_text() if (out / "task.md").exists() else ""
    rubric = (vdir / "rubrics.md").read_text() if (vdir / "rubrics.md").exists() else ""

    bad: list[str] = []
    print("=" * 74)
    print("XAI SELF-CHECK  (clean must FAIL · golden must PASS · style clean)")
    print("=" * 74)

    # Style first: cheap, and a style problem usually explains later failures.
    style = style_problems(out, snap / "workspace")
    for p in style:
        print(f"  [BAD style] {p}")
    bad += style

    clean = _copy_snap(snap, scratch / "clean")
    per_c = _run_scripts(vdir, clean["workspace"], clean["data"],
                         clean.get("webharbor"), snap)
    print("\n[STATE 1] CLEAN untouched day-N (want: every script `failed`)")
    for s in per_c:
        good = s["verdict"] == "failed"
        print(f"  [{'ok ' if good else 'BAD'}] {s['name']}: {s['verdict']}")
        if not good:
            why = {"completed": "WRONGLY PASSES on clean — the artifact it looks "
                                "for must be NEW vs --baseline-dir",
                   "world_imperfect": "reports WORLD-ERROR on a pristine snapshot "
                                      "— wrong db/table name or bad query",
                   "script_error": f"crashed: {s['stderr'][:200]}"}.get(
                s["verdict"], s["verdict"])
            print(f"        ^ {why}")
            bad.append(f"clean:{s['name']}:{s['verdict']}")
    if any(s["verdict"] == "script_error" for s in per_c):
        print("  [skip] lightweight judge on clean: script_error must be fixed first")
    else:
        l2c = lj.judge(task_md, rubric, _evidence(per_c))
        okc = l2c["verdict"] == "failed"
        print(f"  [{'ok ' if okc else 'BAD'}] lightweight judge on clean: {l2c['verdict']}")
        if not okc:
            print(f"        ^ {l2c['justification'][-260:]}")
            bad.append(f"clean:lw:{l2c['verdict']}")

    gold = _copy_snap(snap, scratch / "golden")
    gcmd = ["python3", str(golden), "--workspace", str(gold["workspace"]),
            "--data-dir", str(gold["data"]), "--baseline-dir", str(snap)]
    if gold.get("webharbor"):
        gcmd += ["--webharbor-dir", str(gold["webharbor"])]
    try:
        g = subprocess.run(
            gcmd, capture_output=True, text=True,
            timeout=cfg.LOCAL_SCRIPT_TIMEOUT_S)
        gok, gerr = g.returncode == 0, g.stderr
    except subprocess.TimeoutExpired:
        gok, gerr = False, "golden timeout"
    print(f"\n[golden.py apply] ok={gok}")
    if not gok:
        print(f"        ^ golden CRASHED: {gerr}")
        bad.append("golden:crash")
    per_g = _run_scripts(vdir, gold["workspace"], gold["data"],
                         gold.get("webharbor"), snap)
    print("[STATE 2] GOLDEN-applied (want: every script `completed`)")
    for s in per_g:
        good = s["verdict"] == "completed"
        print(f"  [{'ok ' if good else 'BAD'}] {s['name']}: {s['verdict']}")
        if not good:
            det = json.dumps(s["details"], ensure_ascii=False)[:300]
            print(f"        ^ golden and check disagree — golden must produce "
                  f"EXACTLY the tool-reachable state the check reads "
                  f"(capability contract!). details: {det}")
            bad.append(f"golden:{s['name']}:{s['verdict']}")
    if any(s["verdict"] == "script_error" for s in per_g):
        print("  [skip] lightweight judge on golden: script_error must be fixed first")
    else:
        l2g = lj.judge(task_md, rubric, _evidence(per_g))
        okg = l2g["verdict"] == "completed"
        print(f"  [{'ok ' if okg else 'BAD'}] lightweight judge on golden: {l2g['verdict']}")
        if not okg:
            print(f"        ^ {l2g['justification'][-260:]}")
            bad.append(f"golden:lw:{l2g['verdict']}")

    green = not bad
    print("\n" + "=" * 74)
    print("RESULT: ALL GREEN ✅ — you may finish." if green else
          f"RESULT: NOT GREEN ❌ — {len(bad)} problem(s) above. Fix and RE-RUN.")
    print("=" * 74)
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
