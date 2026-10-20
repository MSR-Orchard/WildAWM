#!/usr/bin/env python3
"""Export accepted WildAWM tasks as Harbor task packages.

Stages: A enumerate accepted tasks, B build world blobs, C verify world digests, D emit packages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis.harbor_export.build_world import build_world

MODULE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = MODULE_DIR / "runtime"
PROJECT_ROOT = cfg.PROJECT_ROOT

ACCEPTED = ("accept", "accept_after_fix")
SITE_NORMALIZE = {"booking.com": "booking"}
KNOWN_SITES = {"allrecipes", "booking", "amazon", "apple", "google_map",
               "coursera", "huggingface", "google_flights", "espn"}

# Pinned Node runtime for the Playwright MCP; sha256-verified when staged.
NODE_ARCHIVE_NAME = "node-v22.23.2-linux-x64.tar.xz"
NODE_ARCHIVE_SHA256 = (
    "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307")
NODE_ARCHIVE_URL = "https://nodejs.org/dist/v22.23.2/" + NODE_ARCHIVE_NAME

def _mcp_servers_toml(app_ids: list[str]) -> str:
    entries = [
        '[[environment.mcp_servers]]\nname = "wildawm"\ntransport = "streamable-http"\nurl = "http://localhost:18080/mcp"'
    ]
    entries.extend(
        '[[environment.mcp_servers]]\nname = "%s"\ntransport = "streamable-http"\nurl = "http://localhost:18080/%s/mcp"' % (app_id, app_id)
        for app_id in sorted(set(app_ids))
    )
    entries.extend([
        '[[environment.mcp_servers]]\nname = "browser"\ntransport = "streamable-http"\nurl = "http://localhost:8931/mcp"',
        '[[environment.mcp_servers]]\nname = "vision"\ntransport = "stdio"\ncommand = "python3"\nargs = ["/agent-config/describe_image_mcp.py"]',
    ])
    return "\n\n".join(entries)

TASK_TOML_TEMPLATE = """schema_version = "1.4"

[task]
name = "wildawm/{slug}"
version = "1.0.0"
description = {description}
keywords = {keywords}

[metadata]
difficulty = "{difficulty}"
persona = "{persona}"
day = {day}
sites = {sites}
mcp_domains = {mcp_domains}
starting_interface = "{starting_interface}"
hops = {hops}
archetype = {archetype}
area = {area}
interface_flow = {interface_flow}
frozen_clock_date = "{frozen_clock_date}"
frozen_clock_time = "{frozen_clock_time}"
source_task = "{source_task}"
world_blob_sha256 = "{world_blob_sha256}"

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 900.0

[environment]
build_timeout_sec = 1800.0
cpus = 2
memory_mb = 4096
storage_mb = 20480
workdir = "/workspace"

[environment.env]
FAKETIME = "@{frozen_clock_date} {frozen_clock_time}"
LD_PRELOAD = "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1"
AWM_MCP_GATEWAY_URL = "http://127.0.0.1:18080"
AWM_BROWSER_MCP_PORT = "8931"
NODE_TLS_REJECT_UNAUTHORIZED = "0"
# Vision model for describe_image; values come from the harbor environment at run time.
WILDAWM_VISION_LLM_ENDPOINT = "${{WILDAWM_VISION_LLM_ENDPOINT:-http://host.docker.internal:8000/v1}}"
WILDAWM_VISION_LLM_MODEL_NAME = "${{WILDAWM_VISION_LLM_MODEL_NAME:-}}"
WILDAWM_VISION_LLM_KEY = "${{WILDAWM_VISION_LLM_KEY:-}}"
# Optional vision tuning.
WILDAWM_VISION_LLM_REASONING_EFFORT = "${{WILDAWM_VISION_LLM_REASONING_EFFORT:-none}}"
WILDAWM_VISION_LLM_REASONING_CHAT_PARAM = "${{WILDAWM_VISION_LLM_REASONING_CHAT_PARAM:-reasoning_effort}}"
WILDAWM_DESCRIBE_IMAGE_MAX_TOKENS = "${{WILDAWM_DESCRIBE_IMAGE_MAX_TOKENS:-4096}}"
WILDAWM_DESCRIBE_IMAGE_TIMEOUT = "${{WILDAWM_DESCRIBE_IMAGE_TIMEOUT:-180}}"
WILDAWM_DESCRIBE_IMAGE_RETRIES = "${{WILDAWM_DESCRIBE_IMAGE_RETRIES:-3}}"
WILDAWM_DESCRIBE_IMAGE_LOCK_TIMEOUT = "${{WILDAWM_DESCRIBE_IMAGE_LOCK_TIMEOUT:-180}}"
# Packaged Playwright browsers.
PLAYWRIGHT_BROWSERS_PATH = "/opt/pw-browsers"

[environment.healthcheck]
command = "bash /opt/wildawm-harbor/healthcheck.sh"
interval_sec = 5.0
timeout_sec = 30.0
start_period_sec = 900.0
start_interval_sec = 5.0
retries = 60

{mcp_servers}"""

DOCKERFILE = """# WildAWM-Harbor task image (single container).
FROM wildawm/wildawm-webharbor-runtime@sha256:8f70a6d6f9e38d0bda3fa604e25043829454695525467c895096c5ce7e315acc

RUN pip install --no-cache-dir zstandard==0.25.0

# Agent installs run under the frozen clock: let Harbor install opencode, use the packaged node, relax download TLS.
RUN rm -f /usr/local/bin/opencode \\
    && echo "harbor-node-bootstrap-marker: not Alpine; glibc image with a frozen clock - see environment/Dockerfile" > /etc/alpine-release \\
    && printf 'insecure\\n' > /root/.curlrc \\
    && git config --system http.https://github.com/.sslVerify false \\
    && printf 'strict-ssl=false\\n' > /etc/npmrc

# WildAWM package for the MCP gateway.
COPY runtime/wildawm-src/ /opt/wildawm-runner/src/
COPY runtime/entrypoint.sh runtime/healthcheck.sh runtime/restore_webharbor.sh /opt/wildawm-harbor/
# Node 22 archive and readiness probe for the Playwright MCP.
COPY runtime/check_playwright_mcp.py /opt/wildawm-harbor/check_playwright_mcp.py
COPY runtime/playwright-node/ /opt/wildawm-node-archive/
COPY runtime/agent-config/ /agent-config/
# Deterministic (persona, day) world archive.
COPY world.tar.zst /opt/world.tar.zst

{patch_copy}ENV PYTHONPATH=/opt/wildawm-runner/src

RUN chmod 0755 /opt/wildawm-harbor/*.sh \\
    && chmod -R a+rX /agent-config \\
    && chown -R agent:agent /workspace

ENTRYPOINT ["/bin/bash", "/opt/wildawm-harbor/entrypoint.sh"]
"""

PATCH_COPY_BLOCK = """# World patch, applied to /world at boot before the baseline snapshot.
COPY runtime/world-patch/ /opt/world-patch/

"""

TEST_SH = """#!/bin/bash
# WildAWM verifier: collect WebHarbor DBs, run check_*.py, write the Harbor reward (see verify_wrapper.py).
set -uo pipefail
mkdir -p /logs/verifier
python3 /tests/verify_wrapper.py \\
    --tests-dir /tests \\
    --workspace /workspace \\
    --data-dir /world/data \\
    --baseline-dir /baseline \\
    --webharbor-dir /tmp/verify-webharbor \\
    --baseline-webharbor-dir /baseline/webharbor \\
    --out-dir /logs/verifier \\
    --collect-script /tests/collect_webharbor.sh
"""


def log_progress(progress_log: Path, stage: str, message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{stamp} [{stage}] {message}"
    with progress_log.open("a") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def toml_str(value) -> str:
    """TOML basic string (json.dumps escapes exactly the TOML subset)."""
    return json.dumps(str(value), ensure_ascii=False)


def toml_list(values) -> str:
    return "[" + ", ".join(toml_str(v) for v in values) + "]"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def flow_legs(meta: dict) -> list[str]:
    """Split interface_flow on '->' or '→' and keep the leg-leading cli|mcp|web tokens."""
    import re
    flow = str(meta.get("interface_flow", "")).replace("\u2192", "->")
    legs = [re.split(r"[:\s]", leg.strip())[0].lower()
            for leg in flow.split("->")]
    return [leg for leg in legs if leg in ("cli", "mcp", "web")]


def _resolve_data_root(env_name: str, what_for: str) -> Path | None:
    """Resolve an optional READ-ONLY source root from the environment."""
    value = os.environ.get(env_name, "").strip()
    if not value:
        return None
    root = Path(value)
    if not root.is_dir():
        raise SystemExit(f"{env_name}={root} is not a directory")
    return root


def resolve_paths(args) -> dict[str, Path]:
    """Resolve every source/output root; fail loud with the env contract."""
    external = _resolve_data_root("WILDAWM_SOURCE_ROOT",
                                  "the read-only synthesis source tree")
    tasks = (Path(args.tasks_dir) if args.tasks_dir
             else Path(cfg.TASKS_XAI_DIR)
             if external is None else external / "tasks_xai")
    masters = (Path(args.masters_dir) if args.masters_dir
               else Path(cfg.WS_MASTERS_DIR)
               if external is None else external / "worlds" / "_masters")
    seed_cache = (Path(args.seed_cache) if args.seed_cache
                  else Path(cfg.WEBHARBOR_SEED_CACHE_DIR)
                  if external is None else external / "webharbor_seeds")
    out = Path(args.out_dir) if args.out_dir else cfg.OUTPUTS_DIR / "harbor-packages"

    for label, path, flag, env_hint in (
            ("tasks root", tasks, "--tasks-dir", "AWM_TASKS_XAI_DIR"),
            ("masters dir", masters, "--masters-dir", "AWM_WS_MASTERS_DIR"),
            ("seed cache", seed_cache, "--seed-cache",
             "AWM_WEBHARBOR_SEED_CACHE_DIR")):
        if not path.is_dir():
            raise SystemExit(
                f"{label} not found: {path}. Pass {flag}, or set the env "
                f"contract ({env_hint} / WILDAWM_DATA_ROOT / "
                "WILDAWM_SOURCE_ROOT); see README.md")

    blob_dir = out / "_world-blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    paths = {"tasks": tasks, "masters": masters, "seed_cache": seed_cache,
             "out": out, "blob_cache": blob_dir / "cache",
             "blob_index": blob_dir / "index.json",
             "progress_log": out / "export_progress.log",
             "emission_manifest": out / "export_manifest.json"}
    paths["blob_cache"].mkdir(parents=True, exist_ok=True)
    return paths


def resolve_eval_root(explicit: str | None) -> Path:
    """Resolve the source root of the in-container gateway package."""
    candidates: list[Path] = []
    raw = (explicit or os.environ.get("WILDAWM_EVAL_SOURCE_ROOT", "")).strip()
    if raw:
        candidates.append(Path(raw))
    else:
        candidates.append(PROJECT_ROOT)
    for root in candidates:
        pkg = root / "wildawm"
        if (pkg / "const_config.py").is_file():
            return pkg
    raise SystemExit(
        "gateway source does not resolve to a wildawm package (looked for wildawm/const_config.py). Use --eval-source-root only when overriding this source release")


def resolve_node_archive(explicit: str | None) -> Path:
    """Resolve the pinned Playwright-Node runtime archive; verify sha256."""
    candidates: list[Path] = []
    raw = (explicit or os.environ.get("WILDAWM_HARBOR_NODE_ARCHIVE", "")).strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    else:
        candidates.append(RUNTIME_DIR / "playwright-node" / NODE_ARCHIVE_NAME)
    for path in candidates:
        if path.is_file():
            digest = sha256_file(path)
            if digest != NODE_ARCHIVE_SHA256:
                raise SystemExit(
                    f"{path}: sha256 {digest} != pinned "
                    f"{NODE_ARCHIVE_SHA256} for {NODE_ARCHIVE_NAME}; "
                    "refusing to stage an unpinned Node runtime")
            return path
    raise SystemExit(
        f"the pinned Playwright-Node runtime archive {NODE_ARCHIVE_NAME} "
        f"(sha256 {NODE_ARCHIVE_SHA256}) was not found. Download it from "
        f"{NODE_ARCHIVE_URL} and pass its path as the node archive argument "
        "(or set WILDAWM_HARBOR_NODE_ARCHIVE) — the emitted task images boot the "
        "in-container browser MCP from it")


def enumerate_accepted(tasks_root: Path) -> dict[str, dict]:
    """Scan <tasks_root>/<persona>/dayN/<slug>/ for accepted verdicts."""
    tasks: dict[str, dict] = {}
    for profile in sorted(tasks_root.iterdir()):
        if not profile.is_dir() or profile.name in (
                "sets", "_synthesis_skills"):
            continue
        for task_dir in sorted(profile.glob("day*/*")):
            if not task_dir.is_dir():
                continue
            report = task_dir / "_synthesis_report.json"
            if not report.is_file():
                continue
            try:
                verdict = json.loads(report.read_text()).get("verdict")
            except (OSError, json.JSONDecodeError):
                continue
            if verdict not in ACCEPTED:
                continue
            assert task_dir.parent.name.startswith("day"), task_dir
            day = int(task_dir.parent.name[3:])
            meta = json.loads((task_dir / "meta.json").read_text())
            tasks[task_dir.name] = {
                "slug": task_dir.name,
                "profile": profile.name,
                "day": day,
                "verdict": verdict,
                "dir": task_dir,
                "meta": meta,
                "legs": flow_legs(meta),
                "has_world_patch": (task_dir / "world_patch").is_dir(),
            }
    return tasks




def slot_map(tasks: dict[str, dict]) -> dict[str, dict]:
    slots: dict[str, dict] = {}
    for task in tasks.values():
        key = f"{task['profile']}/day_{task['day']}"
        slot = slots.setdefault(key, {"profile": task["profile"],
                                      "day": task["day"], "slugs": []})
        slot["slugs"].append(task["slug"])
    return slots


def stage_a(tasks: dict[str, dict], paths: dict[str, Path]) -> dict:
    print("== Stage A: task pool validation")
    if not tasks:
        raise SystemExit(
            "no accepted tasks under the tasks root (verdict accept|"
            "accept_after_fix in _synthesis_report.json); nothing to emit")
    for slug, task in tasks.items():
        meta = task["meta"]
        if meta.get("difficulty") not in ("hard", "medium"):
            raise RuntimeError(f"{slug}: bad difficulty {meta.get('difficulty')}")
        sites = [SITE_NORMALIZE.get(s, s) for s in meta.get("sites", [])]
        unknown = [s for s in sites if s not in KNOWN_SITES]
        if unknown:
            raise RuntimeError(f"{slug}: unknown site(s) {unknown}")
        if not task["legs"]:
            raise RuntimeError(f"{slug}: no flow legs in interface_flow")
        for required in ("title", "area", "archetype", "world_digest"):
            if not meta.get(required):
                raise RuntimeError(f"{slug}: missing meta.{required}")
    report = {"accepted_pool": len(tasks),
              "world_patch_slugs": sorted(
                  t["slug"] for t in tasks.values() if t["has_world_patch"])}
    print(f"  accepted pool: {len(tasks)} tasks")
    print(f"  world_patch tasks: {report['world_patch_slugs'] or 'none'}")
    log_progress(paths["progress_log"], "A",
                 f"pool OK: {len(tasks)} accepted tasks")
    return report


def _build_blob_worker(args) -> dict:
    profile, day, paths = args
    output = paths["blob_cache"] / f"{profile}__day_{day}.tar.zst"
    return build_world(
        profile=profile,
        day=day,
        masters_dir=paths["masters"],
        seed_cache=paths["seed_cache"],
        wildawm_root=paths["masters"].parent.parent,
        output=output,
        mcp_gateway_url="http://127.0.0.1:18080",
        browser_mcp_port=8931,
    )


def stage_b(tasks: dict[str, dict], workers: int,
            paths: dict[str, Path]) -> tuple[dict, dict]:
    slots = slot_map(tasks)
    print(f"== Stage B: {len(slots)} unique (persona, day) world blobs")
    index: dict = {}
    if paths["blob_index"].is_file():
        index = json.loads(paths["blob_index"].read_text())

    todo = []
    for key, slot in sorted(slots.items()):
        output = (paths["blob_cache"]
                  / f"{slot['profile']}__day_{slot['day']}.tar.zst")
        entry = index.get(key, {})
        if (output.is_file() and entry.get("blob_sha256")
                and sha256_file(output) == entry["blob_sha256"]):
            continue
        todo.append((slot["profile"], slot["day"], paths))
    print(f"  {len(todo)} blobs to build, {len(slots) - len(todo)} reused from cache")

    built = 0
    if todo:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_build_blob_worker, item): item
                       for item in todo}
            for future in as_completed(futures):
                manifest = future.result()
                built += 1
                key = f"{manifest['profile']}/day_{manifest['day']}"
                index[key] = manifest
                log_progress(paths["progress_log"], "B",
                             f"{built}/{len(todo)} built {key} "
                             f"sha256={manifest['blob_sha256'][:16]}… "
                             f"{manifest['bytes']} bytes "
                             f"(seeded={manifest['seeded_sites']})")

    for key, slot in sorted(slots.items()):
        output = (paths["blob_cache"]
                  / f"{slot['profile']}__day_{slot['day']}.tar.zst")
        entry = index.get(key, {})
        if not output.is_file() or not entry.get("blob_sha256"):
            raise RuntimeError(f"blob missing for {key}")
        if sha256_file(output) != entry["blob_sha256"]:
            raise RuntimeError(f"blob sha256 drift for {key}")

    paths["blob_index"].write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n")
    total_bytes = sum(index[k]["bytes"] for k in slots)
    print(f"  {len(slots)} blobs ready ({built} built this run), "
          f"{total_bytes / 1e6:.1f} MB total")
    log_progress(paths["progress_log"], "B",
                 f"all {len(slots)} blobs ready ({built} built)")
    return slots, index


def _patch_scope(patch_dir: Path) -> set[str]:
    """DB basenames the task's world_patch may touch (manifest applies_to)."""
    try:
        manifest = json.loads((patch_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(name) for name in manifest.get("applies_to", [])}


def _digest_worker(args) -> dict:
    (key, profile, day, blob_path, task_entries) = args
    # task_entries: [(slug, meta_path, patch_dir | None)]
    from wildawm.synthesis.tasks.freeze import snapshot_digest

    scratch = Path(tempfile.gettempdir()) / f"wildawm-harbor-digest/{profile}__day_{day}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    subprocess.run(["tar", "--zstd", "-xf", str(blob_path), "-C", str(scratch)],
                   check=True, timeout=cfg.HARBOR_DIGEST_TIMEOUT_S)
    world = scratch / "world"
    now = snapshot_digest(world)

    per_task = {}
    patch_rechecks: dict[str, dict] = {}
    for slug, meta_path, patch_dir in task_entries:
        stamped = json.loads(Path(meta_path).read_text())["world_digest"]
        db_mismatch = {k: {"stamped": v, "now": now["dbs"].get(k)}
                       for k, v in stamped["dbs"].items()
                       if now["dbs"].get(k) != v}
        per_task[slug] = {
            "n_stamped_dbs": len(stamped["dbs"]),
            "db_mismatch": db_mismatch,
            "stamped_workspace_paths": stamped.get("workspace_paths"),
            "stamped_n_files": stamped.get("n_files"),
            "packaged_workspace_paths": now["workspace_paths"],
            "packaged_n_files": now["n_files"],
            "workspace_delta_files": now["n_files"] - stamped.get("n_files", 0),
            "overlay_match": (stamped.get("overlay_paths") == now["overlay_paths"]
                              and stamped.get("overlay_n_files")
                              == now["overlay_n_files"]),
        }
        if patch_dir is None:
            continue
        # Patched tasks are stamped post-patch: re-apply the patch and require an exact match.
        patch_scratch = scratch.parent / f"{scratch.name}__{slug}"
        if patch_scratch.exists():
            shutil.rmtree(patch_scratch)
        patch_scratch.mkdir(parents=True)
        subprocess.run(["tar", "--zstd", "-xf", str(blob_path),
                        "-C", str(patch_scratch)],
                       check=True, timeout=cfg.HARBOR_DIGEST_TIMEOUT_S)
        patched_world = patch_scratch / "world"
        scope = _patch_scope(Path(patch_dir))
        allowed = ({k for k in stamped["dbs"] if Path(k).name in scope}
                   if scope else set(stamped["dbs"]))
        env = dict(os.environ, AWM_WORLD_PATCH_PHASE="data")
        result = subprocess.run(
            [sys.executable, str(Path(patch_dir) / "patch_data.py"),
             "--instance-dir", str(patched_world),
             "--workspace", str(patched_world / "workspace"),
             "--data-dir", str(patched_world / "data"),
             "--webharbor-dir", str(patched_world / "webharbor")],
            capture_output=True, text=True, env=env,
            timeout=cfg.HARBOR_DIGEST_TIMEOUT_S)
        if result.returncode != 0:
            raise RuntimeError(
                f"world patch failed for {key}/{slug}: "
                f"{result.stderr[-2000:]}")
        patched = snapshot_digest(patched_world)
        post_mismatch = {k: {"stamped": v, "patched": patched["dbs"].get(k)}
                         for k, v in stamped["dbs"].items()
                         if patched["dbs"].get(k) != v}
        pre_out_of_scope = sorted(
            k for k in db_mismatch if k not in allowed)
        patch_rechecks[slug] = {
            "patch_exit_code": result.returncode,
            "pre_patch_out_of_scope_mismatch": pre_out_of_scope,
            "post_patch_db_mismatch": post_mismatch,
            "post_patch_n_files": patched["n_files"],
        }
        shutil.rmtree(patch_scratch, ignore_errors=True)

    shutil.rmtree(scratch, ignore_errors=True)
    return {"key": key, "per_task": per_task, "patch_rechecks": patch_rechecks}


def stage_c(tasks: dict[str, dict], slots: dict[str, dict],
            index: dict, workers: int, paths: dict[str, Path]) -> dict:
    print(f"== Stage C: digest verification over {len(slots)} "
          f"(persona, day) worlds")

    jobs = []
    for key, slot in sorted(slots.items()):
        blob_path = (paths["blob_cache"]
                     / f"{slot['profile']}__day_{slot['day']}.tar.zst")
        entries = []
        for slug in sorted(slot["slugs"]):
            patch_dir = (str(tasks[slug]["dir"] / "world_patch")
                         if tasks[slug]["has_world_patch"] else None)
            entries.append((slug, str(tasks[slug]["dir"] / "meta.json"),
                            patch_dir))
        jobs.append((key, slot["profile"], slot["day"], str(blob_path),
                     entries))

    results = {}
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_digest_worker, job): job[0] for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            results[result["key"]] = result
            if done % 25 == 0 or done == len(jobs):
                log_progress(paths["progress_log"], "C",
                             f"{done}/{len(jobs)} (persona, day) worlds verified")

    failures = []
    for key, result in sorted(results.items()):
        for slug, check in result["per_task"].items():
            if slug not in result["patch_rechecks"] and check["db_mismatch"]:
                failures.append(f"{key} / {slug}: DB digest drift "
                                f"{check['db_mismatch']}")
            if check["workspace_delta_files"] != 3:
                failures.append(
                    f"{key} / {slug}: workspace file delta "
                    f"{check['workspace_delta_files']} != 3 "
                    "(expected exactly the WORLD.md + USER.md + AGENTS.md "
                    "additions)")
            if not check["overlay_match"]:
                failures.append(f"{key} / {slug}: overlay digest mismatch")
        for slug, recheck in result["patch_rechecks"].items():
            if recheck["pre_patch_out_of_scope_mismatch"]:
                failures.append(
                    f"{key} / {slug}: PRE-patch drift outside the patch "
                    f"scope: {recheck['pre_patch_out_of_scope_mismatch']}")
            if recheck["post_patch_db_mismatch"]:
                failures.append(
                    f"{key} / {slug}: POST-patch DB digest drift "
                    f"{recheck['post_patch_db_mismatch']}")
    if failures:
        raise RuntimeError("digest verification FAILED:\n  " + "\n  ".join(failures))

    n_task_checks = 0
    for key, result in results.items():
        entry = index[key]
        entry["emitted_to"] = slots[key]["slugs"]
        digest_check = {"per_task": result["per_task"],
                        "patch_rechecks": result["patch_rechecks"]}
        digest_check["all_db_digests_match"] = all(
            (not c["db_mismatch"]) or slug in result["patch_rechecks"]
            for slug, c in result["per_task"].items())
        digest_check["workspace_delta_expected"] = all(
            c["workspace_delta_files"] == 3
            for c in result["per_task"].values())
        digest_check["overlay_match"] = all(
            c["overlay_match"] for c in result["per_task"].values())
        entry["digest_check"] = digest_check
        n_task_checks += len(result["per_task"])

    paths["blob_index"].write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(f"  {len(results)}/{len(slots)} worlds verified against "
          f"{n_task_checks} task world_digest stamps (0 unexplained mismatches)")
    log_progress(paths["progress_log"], "C",
                 f"digest verification OK: {len(results)} worlds, "
                 f"{n_task_checks} task stamps")
    return results


def _stage_runtime_template(eval_pkg: Path) -> Path:
    """Assemble the shared environment/runtime template once."""
    template = Path(tempfile.gettempdir()) / "wildawm-harbor-runtime-template"
    if template.exists():
        shutil.rmtree(template)
    (template / "wildawm-src" / "wildawm").mkdir(parents=True)
    (template / "agent-config").mkdir(parents=True)

    dest_pkg = template / "wildawm-src" / "wildawm"
    for rel in ("__init__.py", "const_config.py", "json_utils.py",
                "core/__init__.py", "core/paths.py",
                "cli/__init__.py", "cli/gateway.py"):
        target = dest_pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(eval_pkg / rel, target)
    shutil.copy2(PROJECT_ROOT / "docker" / "mcp_call.py",
                 template / "agent-config" / "mcp_call.py")
    shutil.copy2(RUNTIME_DIR / "agent-config" / "describe_image_mcp.py",
                 template / "agent-config" / "describe_image_mcp.py")
    for name in ("entrypoint.sh", "healthcheck.sh", "restore_webharbor.sh"):
        shutil.copy2(RUNTIME_DIR / name, template / name)
    shutil.copy2(RUNTIME_DIR / "check_playwright_mcp.py",
                 template / "check_playwright_mcp.py")
    return template


def stage_d(tasks: dict[str, dict], slots: dict[str, dict],
            index: dict, paths: dict[str, Path], eval_pkg: Path,
            node_archive: Path) -> dict:
    print(f"== Stage D: emitting {len(tasks)} task packages")
    out_root = paths["out"]
    out_root.mkdir(parents=True, exist_ok=True)
    runtime_template = _stage_runtime_template(eval_pkg)

    (runtime_template / "playwright-node").mkdir(exist_ok=True)
    shutil.copy2(node_archive,
                 runtime_template / "playwright-node" / node_archive.name)

    # docker COPY keeps source modes; make everything readable by non-root container users.
    for path in sorted(runtime_template.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if os.access(path, os.X_OK) else 0o644)

    emission: dict[str, dict] = {}
    done = 0
    for slug in sorted(tasks):
        task = tasks[slug]
        src_dir = task["dir"]
        meta = task["meta"]
        out_dir = out_root / slug
        key = f"{task['profile']}/day_{task['day']}"
        blob = (paths["blob_cache"]
                / f"{task['profile']}__day_{task['day']}.tar.zst")
        manifest = index[key]

        if out_dir.exists():
            shutil.rmtree(out_dir)
        (out_dir / "environment").mkdir(parents=True)
        (out_dir / "solution").mkdir()
        (out_dir / "tests").mkdir()

        shutil.copyfile(src_dir / "task.md", out_dir / "instruction.md")

        # The Node archive is hardlinked rather than copied.
        env_runtime = out_dir / "environment" / "runtime"
        shutil.copytree(runtime_template, env_runtime,
                        ignore=shutil.ignore_patterns("playwright-node"))
        node_dst = env_runtime / "playwright-node" / node_archive.name
        node_dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(node_archive, node_dst)
        if task["has_world_patch"]:
            patch_dst = env_runtime / "world-patch"
            patch_dst.mkdir()
            for name in ("manifest.json", "patch_data.py", "README.md"):
                shutil.copyfile(src_dir / "world_patch" / name,
                                patch_dst / name)

        (out_dir / "environment" / "Dockerfile").write_text(
            DOCKERFILE.format(
                patch_copy=PATCH_COPY_BLOCK if task["has_world_patch"] else ""))

        # Hardlink: one inode per (persona, day) blob.
        world_tar = out_dir / "environment" / "world.tar.zst"
        if world_tar.exists():
            world_tar.unlink()
        os.link(blob, world_tar)

        sites = [SITE_NORMALIZE.get(s, s) for s in meta.get("sites", [])]
        legs = task["legs"]
        (out_dir / "task.toml").write_text(TASK_TOML_TEMPLATE.format(
            slug=slug,
            description=toml_str(meta["title"]),
            keywords=toml_list(["wildawm", "xai", meta["difficulty"]]
                              + sites + list(meta.get("mcp_domains", []))
                              + ["cli", "mcp", "web"]),
            difficulty=meta["difficulty"],
            persona=task["profile"],
            day=task["day"],
            sites=toml_list(sites),
            mcp_domains=toml_list(meta.get("mcp_domains", [])),
            starting_interface=legs[0],
            hops=len(legs),
            archetype=toml_str(meta.get("archetype", "")),
            area=toml_str(str(meta.get("area", "")).lower()),
            interface_flow=toml_str(meta.get("interface_flow", "")),
            frozen_clock_date=manifest["frozen_clock_date"],
            frozen_clock_time=manifest.get("frozen_clock_time", "10:00:00"),
            source_task=f"tasks_xai/{task['profile']}/day{task['day']}/{slug}",
            world_blob_sha256=manifest["blob_sha256"],
            mcp_servers=_mcp_servers_toml(list(meta.get("mcp_domains", []))),
        ))

        solve = out_dir / "solution" / "solve.sh"
        shutil.copyfile(RUNTIME_DIR / "solve_template.sh", solve)
        shutil.copyfile(src_dir / "golden.py", out_dir / "solution" / "golden.py")
        if task["has_world_patch"]:
            # golden.py re-applies the patch from Path(__file__).parent / "world_patch".
            patch_dst = out_dir / "solution" / "world_patch"
            patch_dst.mkdir()
            for name in ("manifest.json", "patch_data.py", "README.md"):
                shutil.copyfile(src_dir / "world_patch" / name,
                                patch_dst / name)

        test_sh = out_dir / "tests" / "test.sh"
        test_sh.write_text(TEST_SH)
        shutil.copyfile(RUNTIME_DIR / "verify_wrapper.py",
                        out_dir / "tests" / "verify_wrapper.py")
        shutil.copyfile(RUNTIME_DIR / "collect_webharbor.sh",
                        out_dir / "tests" / "collect_webharbor.sh")
        scripts_dir = src_dir / "verifier" / "scripts"
        n_checks = 0
        for py in sorted(scripts_dir.glob("*.py")):
            shutil.copyfile(py, out_dir / "tests" / py.name)
            if py.name != "_verify_sdk.py":
                n_checks += 1

        for script in (solve, test_sh,
                       out_dir / "tests" / "collect_webharbor.sh",
                       env_runtime / "entrypoint.sh",
                       env_runtime / "healthcheck.sh",
                       env_runtime / "restore_webharbor.sh"):
            script.chmod(0o755)

        emission[slug] = {
            "persona": task["profile"], "day": task["day"],
            "difficulty": meta["difficulty"],
            "starting_interface": legs[0], "hops": len(legs), "sites": sites,
            "mcp_domains": meta.get("mcp_domains", []),
            "world_blob_sha256": manifest["blob_sha256"],
            "world_blob_bytes": manifest["bytes"],
            "frozen_clock_date": manifest["frozen_clock_date"],
            "has_world_patch": task["has_world_patch"],
            "n_check_scripts": n_checks,
            "verdict": task["verdict"],
        }
        done += 1
        if done % 25 == 0 or done == len(tasks):
            log_progress(paths["progress_log"], "D",
                         f"{done}/{len(tasks)} packages emitted")

    paths["emission_manifest"].write_text(
        json.dumps({"emitted_at": datetime.now(timezone.utc).isoformat(),
                    "n_tasks": len(emission), "tasks": emission},
                   indent=2, sort_keys=True) + "\n")
    print(f"  emitted {len(emission)} packages under {out_root}")
    log_progress(paths["progress_log"], "D",
                 f"emission complete: {len(emission)} packages")
    return emission


def run_export(args) -> int:
    paths = resolve_paths(args)
    stages = {s.strip().upper() for s in args.stages.split(",")}
    tasks = enumerate_accepted(paths["tasks"])
    print(f"accepted pool: {len(tasks)} tasks")

    started = time.time()
    log_progress(paths["progress_log"], "main",
                 f"export start: stages={sorted(stages)} "
                 f"workers={args.workers} only={args.only or 'all'}")

    report: dict = {}
    if "A" in stages:
        report["pool"] = stage_a(tasks, paths)
    slots, index = (stage_b(tasks, args.workers, paths) if "B" in stages
                    else (slot_map(tasks),
                          json.loads(paths["blob_index"].read_text())
                          if paths["blob_index"].is_file() else {}))
    if "C" in stages:
        stage_c(tasks, slots, index, args.workers, paths)
    if "D" in stages:
        selected = tasks
        if args.only:
            wanted = [s for s in args.only.split(",") if s in tasks]
            selected = {s: tasks[s] for s in wanted}
        eval_pkg = resolve_eval_root(args.eval_source_root)
        node_archive = resolve_node_archive(args.node_archive)
        report["emission"] = stage_d(selected, slots, index,
                                     paths, eval_pkg, node_archive)

    log_progress(paths["progress_log"], "main",
                 f"export done in {time.time() - started:.1f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks-dir", default="",
                        help="accepted-task tree (default: AWM_TASKS_XAI_DIR "
                             "/ WILDAWM_DATA_ROOT, else "
                             "$WILDAWM_SOURCE_ROOT/tasks_xai)")
    parser.add_argument("--masters-dir", default="",
                        help="worlds/_masters dir (default: AWM_WS_MASTERS_DIR "
                             "/ WILDAWM_DATA_ROOT, else "
                             "$WILDAWM_SOURCE_ROOT/worlds/_masters)")
    parser.add_argument("--seed-cache", default="",
                        help="webharbor_seeds dir (default: "
                             "AWM_WEBHARBOR_SEED_CACHE_DIR / "
                             "WILDAWM_DATA_ROOT, else "
                             "$WILDAWM_SOURCE_ROOT/webharbor_seeds)")
    parser.add_argument("--out-dir", default="",
                        help="output root (default: AWM_OUTPUTS_DIR/"
                             "harbor-packages)")
    parser.add_argument("--eval-source-root", default="",
                        help="gateway source root containing wildawm/ "
                             "(default: $WILDAWM_EVAL_SOURCE_ROOT, else this "
                             "source release)")
    parser.add_argument("--node-archive", default="",
                        help="pinned playwright Node archive (default: "
                             "$WILDAWM_HARBOR_NODE_ARCHIVE, else "
                             "runtime/playwright-node/ in this module)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--stages", default="A,B,C,D",
                        help="comma list among A,B,C,D")
    parser.add_argument("--only", default="",
                        help="comma list of slugs (debug: restrict emission)")
    args = parser.parse_args()
    return run_export(args)


if __name__ == "__main__":
    raise SystemExit(main())
