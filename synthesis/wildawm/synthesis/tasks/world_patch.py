#!/usr/bin/env python3
"""Task-local world patches, applied only to copied world instances of one task."""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.core.paths import PROJECT_ROOT
from wildawm.json_utils import load_jsonc
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import llm_judge as lj
from wildawm.synthesis.tasks import verifier as vf
from wildawm.synthesis.worlds import anchors

PATCH_DIRNAME = "world_patch"
MANIFEST = "manifest.json"
PATCH_DATA = "patch_data.py"
PATCH_RUNTIME = "patch_runtime.py"
REPAIR_REQUEST = "repair_request.json"
README = "README.md"

ALLOWED_TARGETS = ("workspace", "data", "webharbor", "runtime_code")
MUST_NOT = [
    "pre-complete the task",
    "write expected solution artifacts",
    "change task.md semantics",
    "change verifier to hide the world defect",
    "modify worlds/_master or the host source tree",
]

_DATA_FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "urllib", "requests", "httpx", "docker", "git",
}
_RUNTIME_FORBIDDEN_IMPORTS = {"git"}
_FORBIDDEN_STRING_FRAGMENTS = (
    "worlds/_master",
    "/worlds/_master",
    "tasks_xai/",
    "/home/",
    "git commit",
    "git push",
    "docker build",
    "docker commit",
    "docker volume",
)
_PATCHED_BASELINE_CACHE = cfg.SCRATCH / "patched_baselines"


@dataclass
class PatchApplyResult:
    ok: bool
    skipped: bool = False
    patch_dir: str = ""
    phase: str = "data"
    stdout: str = ""
    stderr: str = ""
    rc: int | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "patch_dir": self.patch_dir,
            "phase": self.phase,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "rc": self.rc,
            "error": self.error,
        }


def task_patch_dir(task_dir: Path | str) -> Path:
    return Path(task_dir) / PATCH_DIRNAME


def has_patch(task_dir: Path | str) -> bool:
    p = task_patch_dir(task_dir)
    return (p / MANIFEST).is_file() and (p / PATCH_DATA).is_file()


def resolve_patch_dir(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"world_patch dir not found: {p}")
    return p


def manifest(patch_dir: Path | str) -> dict:
    p = Path(patch_dir) / MANIFEST
    if not p.exists():
        return {}
    try:
        return load_jsonc(p)
    except (ValueError, OSError):
        return {}


def _safe_world_id(world_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in world_id.lower())


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _ensure_not_master(path: Path) -> None:
    resolved = path.resolve()
    for master in (cfg.WS_MASTERS_DIR, cfg.MASTER_DIR):
        if _is_under(resolved, master):
            raise RuntimeError(f"refusing to apply task-local patch under a master dir: {resolved}")


def _script_safety_errors(script: Path, *, runtime: bool) -> list[str]:
    if not script.exists():
        return []
    try:
        tree = ast.parse(script.read_text(errors="replace"))
    except SyntaxError as e:
        return [f"{script.name}: syntax error: {e}"]
    forbidden_imports = _RUNTIME_FORBIDDEN_IMPORTS if runtime else _DATA_FORBIDDEN_IMPORTS
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in forbidden_imports:
                    errors.append(f"{script.name}: forbidden import `{alias.name}`")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in forbidden_imports:
                errors.append(f"{script.name}: forbidden import `{node.module}`")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            for frag in _FORBIDDEN_STRING_FRAGMENTS:
                if frag in text:
                    errors.append(f"{script.name}: forbidden string fragment `{frag}`")
    return errors


def _digest_tree(root: Path) -> str:
    h = hashlib.sha256()
    root = Path(root)
    if not root.exists():
        h.update(b"<missing>")
        return h.hexdigest()
    if root.is_file():
        h.update(f"F {root.name} {root.stat().st_size}\n".encode())
        with root.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            h.update(f"D {rel}\n".encode())
            continue
        if p.is_symlink():
            h.update(f"L {rel} {os.readlink(p)}\n".encode())
            continue
        h.update(f"F {rel} {p.stat().st_size}\n".encode())
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\n")
    return h.hexdigest()


def world_state_digest(root: Path | str) -> str:
    root = Path(root)
    h = hashlib.sha256()
    for sub in ("workspace", "data", "webharbor", "overlay"):
        h.update(sub.encode() + b"\0")
        h.update(_digest_tree(root / sub).encode() + b"\0")
    return h.hexdigest()


def _copy_snapshot(src: Path, dest: Path) -> dict:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    out = {}
    for d in ("workspace", "data", "webharbor", "overlay"):
        s = src / d
        if s.exists():
            shutil.copytree(s, dest / d, symlinks=True)
            out[d] = dest / d
    return out


def master_snapshot(profile: str, day: int) -> Path:
    # A filled scratch copy; the master world is never modified here.
    return anchors.inspection_world(profile, day)


def apply_data_patch(patch_dir: Path | str | None, instance_dir: Path | str) -> PatchApplyResult:
    patch_dir = resolve_patch_dir(patch_dir)
    if not patch_dir:
        return PatchApplyResult(ok=True, skipped=True)
    instance_dir = Path(instance_dir).resolve()
    _ensure_not_master(instance_dir)
    script = patch_dir / PATCH_DATA
    if not script.exists():
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="data", error="world_patch/patch_data.py is required")
    errors = _script_safety_errors(script, runtime=False)
    if errors:
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="data", error="; ".join(errors))
    before_protected = {
        "meta.json": _digest_tree(instance_dir / "meta.json"),
        "scripts": _digest_tree(instance_dir / "scripts"),
    }
    cmd = [
        sys.executable,
        str(script),
        "--instance-dir",
        str(instance_dir),
        "--workspace",
        str(instance_dir / "workspace"),
        "--data-dir",
        str(instance_dir / "data"),
        "--webharbor-dir",
        str(instance_dir / "webharbor"),
    ]
    env = dict(os.environ)
    env.update({
        "AWM_WORLD_PATCH_PHASE": "data",
        "AWM_WORLD_PATCH_DIR": str(patch_dir),
        "AWM_WORLD_PATCH_INSTANCE_DIR": str(instance_dir),
    })
    try:
        r = subprocess.run(
            cmd,
            cwd=str(patch_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=cfg.LOCAL_SCRIPT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="data", rc=124, error="patch_data.py timed out")
    after_protected = {
        "meta.json": _digest_tree(instance_dir / "meta.json"),
        "scripts": _digest_tree(instance_dir / "scripts"),
    }
    changed_protected = [k for k, v in before_protected.items() if after_protected.get(k) != v]
    if changed_protected:
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="data", stdout=r.stdout, stderr=r.stderr, rc=r.returncode, error=f"patch_data.py modified protected instance paths: {changed_protected}")
    return PatchApplyResult(ok=r.returncode == 0, patch_dir=str(patch_dir), phase="data", stdout=r.stdout, stderr=r.stderr, rc=r.returncode, error="" if r.returncode == 0 else "patch_data.py failed")


def runtime_patch_args(patch_dir: Path | str | None, world_id: str, project: str, instance_dir: Path | str) -> list[str]:
    patch_dir = resolve_patch_dir(patch_dir)
    if not patch_dir:
        return []
    safe = _safe_world_id(world_id)
    return [
        sys.executable,
        str(patch_dir / PATCH_RUNTIME),
        "--world-id",
        world_id,
        "--project",
        project,
        "--instance-dir",
        str(Path(instance_dir).resolve()),
        "--mcp-container",
        f"wildawm-mcp-{safe}",
        "--webharbor-container",
        f"wildawm-webharbor-{safe}",
        "--worldctl-container",
        f"wildawm-worldctl-{safe}",
    ]


def apply_runtime_patch(patch_dir: Path | str | None, *, world_id: str, project: str, instance_dir: Path | str) -> PatchApplyResult:
    patch_dir = resolve_patch_dir(patch_dir)
    if not patch_dir:
        return PatchApplyResult(ok=True, skipped=True, phase="runtime")
    script = patch_dir / PATCH_RUNTIME
    if not script.exists():
        return PatchApplyResult(ok=True, skipped=True, patch_dir=str(patch_dir), phase="runtime")
    errors = _script_safety_errors(script, runtime=True)
    if errors:
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="runtime", error="; ".join(errors))
    cmd = runtime_patch_args(patch_dir, world_id, project, instance_dir)
    env = dict(os.environ)
    env.update({
        "AWM_WORLD_PATCH_PHASE": "runtime",
        "AWM_WORLD_PATCH_DIR": str(patch_dir),
        "AWM_WORLD_PATCH_INSTANCE_DIR": str(Path(instance_dir).resolve()),
        "AWM_WORLD_ID": world_id,
    })
    try:
        r = subprocess.run(
            cmd,
            cwd=str(patch_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=cfg.DOCKER_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return PatchApplyResult(ok=False, patch_dir=str(patch_dir), phase="runtime", rc=124, error="patch_runtime.py timed out")
    return PatchApplyResult(ok=r.returncode == 0, patch_dir=str(patch_dir), phase="runtime", stdout=r.stdout, stderr=r.stderr, rc=r.returncode, error="" if r.returncode == 0 else "patch_runtime.py failed")


def materialize_clean_snapshot(profile: str, day: int, dest: Path | str, patch_dir: Path | str | None = None) -> dict:
    src = master_snapshot(profile, day)
    dest = Path(dest)
    out = _copy_snapshot(src, dest)
    if patch_dir:
        applied = apply_data_patch(patch_dir, dest)
        if not applied.ok:
            raise RuntimeError(f"world patch failed on clean snapshot: {applied.to_dict()}")
        for d in ("workspace", "data", "webharbor", "overlay"):
            s = dest / d
            if s.exists():
                out[d] = s
    return out


def patched_baseline_dir(profile: str, day: int, task_dir: Path | str, *, cache_root: Path | None = None) -> Path:
    task_dir = Path(task_dir)
    patch_dir = task_patch_dir(task_dir)
    if not has_patch(task_dir):
        return master_snapshot(profile, day)
    cache_root = cache_root or _PATCHED_BASELINE_CACHE
    key_src = f"{profile}\0{day}\0{task_dir.resolve()}\0{_digest_tree(patch_dir)}"
    key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
    dest = cache_root / profile / f"day{day}" / task_dir.name / key
    marker = dest / ".world_patch_applied"
    if marker.exists():
        return dest
    materialize_clean_snapshot(profile, day, dest, patch_dir)
    marker.write_text(json.dumps({"patch_dir": str(patch_dir.resolve()), "created_at": time.time()}))
    return dest


def write_repair_request(*, profile: str, day: int, slug: str, task_dir: Path | str, evidence: dict, base_snapshot: Path | str, existing_patch: Path | str | None = None, allowed_targets: list[str] | None = None) -> Path:
    task_dir = Path(task_dir)
    patch_dir = task_patch_dir(task_dir)
    patch_dir.mkdir(parents=True, exist_ok=True)
    req = {
        "profile": profile,
        "day": day,
        "slug": slug,
        "task_dir": str(task_dir),
        "base_snapshot": str(base_snapshot),
        "existing_patch": str(existing_patch or patch_dir),
        "evidence": evidence,
        "allowed_targets": allowed_targets or list(ALLOWED_TARGETS),
        "must_not": list(MUST_NOT),
    }
    path = patch_dir / REPAIR_REQUEST
    path.write_text(json.dumps(req, indent=2, ensure_ascii=False))
    return path


_REPAIR_PROMPT = """# Generate a deterministic task-local world_patch

You are the `world_patch_generator` repair role, separate from the task author. Fix only the world defect described in `world_patch/repair_request.json` by writing a deterministic task-local patch under `world_patch/`.

Required output layout:
- `world_patch/manifest.json`
- `world_patch/patch_data.py`
- optional `world_patch/patch_runtime.py` only if a container-local service overlay is necessary
- `world_patch/README.md`

Constraints:
- Do not edit `task.md`, `reason.md`, `golden.py`, `verifier/`, `meta.json`, `worlds/_master`, host repository source, shared Docker images, or shared Docker volumes.
- `patch_data.py` is invoked as `python patch_data.py --instance-dir <root> --workspace <ws> --data-dir <data> --webharbor-dir <wh>`. It MUST define all four argparse arguments (`--instance-dir`, `--workspace`, `--data-dir`, `--webharbor-dir`; `parse_known_args` is fine) so the call does not crash on `--instance-dir`. It must be deterministic, offline, idempotent, and may modify only the `workspace`, `data`, and `webharbor` dirs (not other contents of the instance root). Do not import or use `urllib`, `requests`, `httpx`, `socket`, `subprocess`, `docker`, or `git`; do not download seed files during patch application. Seed files by writing fixed content/bytes, copying from the passed clean instance dirs, or embedding verified exact bytes.
- `patch_runtime.py`, if needed, is invoked as `python patch_runtime.py --world-id <id> --project <proj> --instance-dir <root> --mcp-container <c> --webharbor-container <c> --worldctl-container <c>`. It MUST define all six argparse arguments (or use `parse_known_args`) so the call does not crash. It must operate only on the passed `--world-id` containers and must not rebuild images, commit containers, or write shared volumes.
- The patch must fix the world precondition without completing the user's task or writing expected solution artifacts.

Inspect the task assets, verifier/golden behavior, the clean snapshot, and the repair request. Then write the patch files now. Finish only after the layout exists and the scripts are syntactically valid.
"""


def run_repair_agent(task_dir: Path | str, request_path: Path | str, *, base_snapshot: Path | str, timeout: int | None = None) -> dict:
    timeout = timeout or cfg.LLM_HEAVY_TIMEOUT_S
    task_dir = Path(task_dir)
    request_path = Path(request_path)
    before = _digest_tree(task_dir / PATCH_DIRNAME)
    llm = cr.LLM(
        model=cfg.model_spec(cfg.HEAVY_JUDGE_MODEL)["api_model"],
        route="proxy",
        model_ref=cfg.HEAVY_JUDGE_MODEL,
    )
    rec = harness.run_agent(
        _REPAIR_PROMPT,
        role="world_patch_generator",
        claude_llm=llm,
        timeout=timeout,
        cwd=str(task_dir),
        retries=0,
        extra_paths=[str(base_snapshot), str(request_path)],
    )
    rec.extra.pop("trajectory", None)
    after = _digest_tree(task_dir / PATCH_DIRNAME)
    patch_dir = task_patch_dir(task_dir)
    changed = before != after
    present = {
        MANIFEST: (patch_dir / MANIFEST).is_file(),
        PATCH_DATA: (patch_dir / PATCH_DATA).is_file(),
        README: (patch_dir / README).is_file(),
    }
    return {
        "ok": rec.ok and changed and all(present.values()),
        "changed": changed,
        "present": present,
        "patch_dir": str(patch_dir),
        "call": rec.to_dict(),
    }


def _default_lightweight(task_md: str, rubric: str, rule_result: dict) -> dict:
    return lj.judge(task_md, rubric, vf.build_lightweight_evidence(rule_result), spec=cfg.model_spec(cfg.LIGHTWEIGHT_JUDGE_MODEL))


def validate_patch(task_dir: Path | str, profile: str, day: int, scratch: Path | str, *, lightweight_judge=None) -> dict:
    task_dir = Path(task_dir)
    scratch = Path(scratch)
    patch_dir = task_patch_dir(task_dir)
    defects: list[str] = []
    if not has_patch(task_dir):
        return {"ok": False, "defects": ["world_patch layout incomplete"], "detail": {}}
    for script, runtime in ((patch_dir / PATCH_DATA, False), (patch_dir / PATCH_RUNTIME, True)):
        defects.extend(_script_safety_errors(script, runtime=runtime))
    manifest_json = manifest(patch_dir)
    if not manifest_json:
        defects.append("world_patch/manifest.json is missing or invalid JSON")
    if defects:
        return {"ok": False, "defects": defects, "detail": {"patch_dir": str(patch_dir)}}

    once = scratch / "patch_once"
    materialize_clean_snapshot(profile, day, once, patch_dir)
    digest_once = world_state_digest(once)
    reapplied = apply_data_patch(patch_dir, once)
    digest_twice = world_state_digest(once)
    if not reapplied.ok:
        defects.append(f"patch_data.py failed on idempotence reapply: {reapplied.to_dict()}")
    if digest_once != digest_twice:
        defects.append("patch_data.py is not idempotent on workspace/data/webharbor")

    baseline = scratch / "patched_baseline"
    clean = materialize_clean_snapshot(profile, day, baseline, patch_dir)
    vdir = task_dir / "verifier"
    golden = task_dir / "golden.py"
    task_md = (task_dir / "task.md").read_text() if (task_dir / "task.md").exists() else ""
    rubric = (vdir / "rubrics.md").read_text() if (vdir / "rubrics.md").exists() else ""
    rc = vf.run_verifier(vdir, clean["workspace"], clean["data"], baseline, clean.get("webharbor"))
    judge = lightweight_judge or _default_lightweight
    l2c = judge(task_md, rubric, rc)
    if rc["script_errors"]:
        defects.append(f"scripts crashed on patched clean: {rc['script_errors']}")
    if rc["verdict"] != "failed":
        defects.append(f"patched clean world did not fail task (got {rc['verdict']}); patch may pre-complete the task or leave a world defect")
    if l2c.get("verdict") not in ("failed", "world_imperfect"):
        defects.append(f"lightweight judge saw patched clean as {l2c.get('verdict')}")

    gold_dir = scratch / "patched_golden"
    gold = materialize_clean_snapshot(profile, day, gold_dir, patch_dir)
    ag = vf.apply_golden(golden, gold["workspace"], gold["data"], gold.get("webharbor"), baseline)
    rg = vf.run_verifier(vdir, gold["workspace"], gold["data"], baseline, gold.get("webharbor"))
    l2g = judge(task_md, rubric, rg)
    if not ag["ok"]:
        defects.append(f"golden.py crashed on patched clean: {ag.get('stderr', '')}")
    if rg["verdict"] != "completed":
        defects.append(f"patched golden did not pass (got {rg['verdict']})")
    if l2g.get("verdict") != "completed":
        defects.append(f"lightweight judge saw patched golden as {l2g.get('verdict')}")

    return {
        "ok": not defects,
        "defects": defects,
        "detail": {
            "manifest": manifest_json,
            "patch_dir": str(patch_dir),
            "idempotent": digest_once == digest_twice and reapplied.ok,
            "clean_l1": rc["verdict"],
            "clean_l2": l2c.get("verdict"),
            "golden_apply": ag["ok"],
            "golden_l1": rg["verdict"],
            "golden_l2": l2g.get("verdict"),
            "runtime_patch": (patch_dir / PATCH_RUNTIME).exists(),
        },
    }
