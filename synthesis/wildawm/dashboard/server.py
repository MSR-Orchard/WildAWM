"""WildAWM dashboard: FastAPI backend serving the UI and the world/agent/verify REST API."""

import asyncio
import base64
import concurrent.futures
import hashlib
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta as _td, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from simpleArgParser import parse_args

from wildawm_common import DESCRIBE_IMAGE_MCP

from wildawm import const_config as runtime_cfg
from wildawm.core.paths import (
    EVENTS_DIR,
    INSTANCES_DIR,
    NANOBOT_STATE_DIR,
    OUTPUTS_DIR,
    PROJECT_ROOT,
    STORIES_DIR,
    TASKS_XAI_DIR,
    WORLDS_DIR,
)
from wildawm.const_config import (
    AGENT_READY_TIMEOUT_S,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DEFAULT_AGENT_TURN_TIMEOUT_S,
    DOCKER_COMMAND_TIMEOUT_S,
    ENV_PORT_BIND_ATTEMPTS,
    ENV_PATH,
    HEAVY_JUDGE_MODEL,
    HTTP_MEDIUM_TIMEOUT_S,
    HTTP_PROBE_TIMEOUT_S,
    HTTP_SHORT_TIMEOUT_S,
    LLM_PROXY_UPSTREAM_TIMEOUT_S,
    LLM_PROXY_PORT,
    LOG_DIR as ROOT_LOG_DIR,
    MCP_INTERNAL_PORT,
    MCP_PORT_BASE,
    OPENCLAW_PROVIDER_TIMEOUT_S,
    RUNTIME_PROVISION_TIMEOUT_S,
    RUNTIME_SHORT_TIMEOUT_S,
    SUBPROCESS_FAST_TIMEOUT_S,
    WEBHARBOR_NETWORK_CONNECT_RETRIES,
    WEBHARBOR_NETWORK_CONNECT_TIMEOUT_S,
    WEBHARBOR_CTRL_PORT,
    WEBHARBOR_PORT_BASE,
    WORLDCTL_PORT,
    WORLDCTL_PORT_BASE,
)
from wildawm.dashboard.world_md import render_world_md
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import tasks_index as _xai_index
from wildawm.synthesis.tasks import task_skill_pool as _xai_task_skills
from wildawm.synthesis.tasks import xai_pool as _xai_pool
from wildawm.synthesis.tasks import world_patch as _xai_world_patch
from wildawm.verify_stats import (
    agent_stats_from_history_proxy,
    finalize_stats,
    judge_stats_from_meta,
)
from wildawm.webharbor import lib as _wh
from wildawm.webharbor import control as _wc
from wildawm.synthesis.worlds import anchors as _ws_anchors
from wildawm.synthesis.worlds import overlay as _ws_overlay
from wildawm.synthesis.worlds import primitives as _ws_primitives


@dataclass
class DashboardArgs:
    port: int = DASHBOARD_PORT
    host: str = DASHBOARD_HOST


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the project .env into os.environ without overriding set vars."""
    env_path = ENV_PATH
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

INSTANCES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="WildAWM Dashboard", version="0.1.0")

# Running per-world compose stacks: one shared environment, any number of attached agents.
_runtimes: dict[str, dict] = {}
# Allocation runs in worker threads; reserve ports under a lock before the slow compose up.
_alloc_lock = threading.Lock()
_reserved_ports: set[int] = set()
_MCP_PORT_BASE = MCP_PORT_BASE
_WEBHARBOR_PORT_BASE = WEBHARBOR_PORT_BASE
_WORLDCTL_PORT_BASE = WORLDCTL_PORT_BASE

from wildawm.dashboard import agents as _agents  # noqa: E402  (after app/state init)
from wildawm.dashboard import worldctl_client as _worldctl  # noqa: E402
from wildawm.dashboard import models as _models  # noqa: E402  (model registry)


LOG_DIR = ROOT_LOG_DIR
LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("wildawm.dashboard")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s dashboard %(levelname)s %(message)s")
    _fh = logging.FileHandler(LOG_DIR / "wildawm.dashboard.log")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# Every compose profile, so a full teardown leaves no profile-gated container behind.
_ALL_PROFILES = ["webharbor", *(b.KIND for b in _agents.REGISTRY.values())]
_ALL_PROFILE_ARGS = [a for p in _ALL_PROFILES for a in ("--profile", p)]


def _world_path(world_id: str) -> Path:
    """Resolve a world id to its instance directory."""
    p = INSTANCES_DIR / world_id
    if not p.is_dir():
        raise HTTPException(404, f"Instance not found: {world_id}")
    return p


def _master_path(profile: str) -> Path:
    p = _ws_anchors.master_root(profile)
    if not p.is_dir():
        raise HTTPException(404, f"Profile master not found: {profile}")
    return p


def _available_days(profile: str) -> list[int]:
    """Days that actually EXIST for a profile, read from its ``days.json``."""
    return sorted(_ws_primitives.read_days(_ws_anchors.master_root(profile)).keys())


def _available_days_safe(profile: str) -> list[int]:
    """Like _available_days, but an unknown profile reports no days instead of raising."""
    try:
        return _available_days(profile)
    except HTTPException:
        return []


def _ensure_day_snapshot(profile: str, day: int) -> Path:
    """Resolve the pre-baked day-N snapshot (built by `awm syn prebake`), else 404."""
    snap = _ws_anchors.master_snapshot_dir(profile, day)
    if snap.is_dir():
        return snap
    avail = _available_days(profile)
    span = f"{avail[0]}..{avail[-1]}" if avail else "(none)"
    raise HTTPException(404,
        f"profile {profile!r} day {day} is not pre-baked at {snap} "
        f"(days.json covers {span}). Run `awm syn prebake --profile {profile}` first.")


def _db_path(world_id: str, app_id: str) -> Path:
    p = _world_path(world_id) / "data" / f"{app_id}.db"
    if not p.exists():
        raise HTTPException(404, f"Database not found: {app_id}.db")
    return p


def _safe_sql(sql: str):
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        raise HTTPException(400, "Only SELECT queries are allowed")
    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH"]
    tokens = re.split(r"\s+", stripped)
    for t in tokens:
        if t in dangerous:
            raise HTTPException(400, f"Forbidden SQL keyword: {t}")


def _query_db(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_table_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _get_row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()


def _get_table_schema(db_path: Path, table: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [{"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "default": r[4], "pk": r[5]} for r in rows]
    finally:
        conn.close()


def _list_master_profiles() -> list[str]:
    if not runtime_cfg.WS_MASTERS_DIR.is_dir():
        return []
    return sorted(p.name for p in runtime_cfg.WS_MASTERS_DIR.iterdir()
                  if p.is_dir() and (p / "scripts").is_dir())


def _profile_days(profile: str) -> list[dict]:
    """List {day, date, event_count, events} per day for a profile, from its ``days.json``."""
    master = _master_path(profile)
    days: dict[int, dict] = {}
    for day, date in sorted(_ws_primitives.read_days(master).items()):
        events = []
        manifest = runtime_cfg.WS_PLAN_DIR / profile / f"day_{day}.skills.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text())
                events = [
                    {
                        "event": str(row.get("skill") or ""),
                        "description": str(row.get("activity") or ""),
                        "interfaces": list(row.get("interfaces") or []),
                        "mcp_apps": list(row.get("mcp_apps") or []),
                    }
                    for row in payload.get("events", [])
                    if isinstance(row, dict)
                ]
            except (OSError, ValueError):
                events = []
        days[day] = {"day": day, "date": date, "events": events}
    # Mark days with a ready snapshot and count eligible XAI tasks per day.
    xai_registry = _load_xai_registry()
    for day in days:
        snap = _ws_anchors.master_snapshot_dir(profile, day)
        days[day]["snapshot_ready"] = snap.is_dir()
        days[day]["event_count"] = len(days[day]["events"])
        xai_count = 0
        for _task_name, info in xai_registry.items():
            for rule in info.get("eligible", []):
                if rule.get("profile") == profile and day in rule.get("days", []):
                    xai_count += 1
                    break
        days[day]["xai_task_count"] = xai_count
    return [days[k] for k in sorted(days)]


def _profile_data_stats(profile: str) -> dict:
    """Count, size and row totals of the profile's latest built day databases (best-effort)."""
    master = _master_path(profile)
    days = _available_days_safe(profile)
    snap = None
    for d in reversed(days):
        candidate = master / f"day_{d}"
        if candidate.is_dir():
            snap = candidate
            break
    dbs, total_bytes, total_rows = 0, 0, 0
    if snap is not None:
        for sub in ("data", "webharbor"):
            sub_dir = snap / sub
            if not sub_dir.is_dir():
                continue
            for db in sorted(sub_dir.glob("*.db")):
                dbs += 1
                total_bytes += db.stat().st_size
                try:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    try:
                        tables = [r[0] for r in con.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'").fetchall()]
                        for t in tables:
                            total_rows += con.execute(
                                f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    finally:
                        con.close()
                except sqlite3.Error:
                    pass
    return {"db_count": dbs, "data_bytes": total_bytes, "data_rows": total_rows}


def _profile_md_path(name: str) -> Path:
    """Resolve a generated persona bible."""
    return runtime_cfg.WS_PROFILES_DIR / name / "persona.md"


def _generated_profile_names() -> list[str]:
    root = runtime_cfg.WS_PROFILES_DIR
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith("_")
        and (d / "persona.md").is_file()
    )


@app.get("/api/profiles")
async def list_profiles():
    """List generated personas, including profiles not materialized yet."""
    profiles = []
    seen: set[str] = set()
    for name in _list_master_profiles():
        seen.add(name)
        try:
            days = _profile_days(name)
        except HTTPException:
            days = []
        profile_md = _profile_md_path(name)
        title, summary = _profile_md_summary(profile_md)
        profiles.append({
            "name": name,
            "title": title or name,
            "summary": summary,
            "master_ready": True,
            "days": days,
            "ready_days": [d["day"] for d in days if d.get("snapshot_ready")],
            "total_events": sum(d.get("event_count", 0) for d in days),
            "has_stories": (STORIES_DIR / name).is_dir(),
            **_profile_data_stats(name),
        })
    # Personas generated but not materialized yet.
    for name in _generated_profile_names():
        if name in seen:
            continue
        path = _profile_md_path(name)
        title, summary = _profile_md_summary(path)
        profiles.append({
            "name": name,
            "title": title or name,
            "summary": summary,
            "master_ready": False,
            "days": [],
            "ready_days": [],
            "has_stories": (STORIES_DIR / name).is_dir(),
        })
    return {"profiles": profiles}


def _profile_md_summary(p: Path) -> tuple[str, str]:
    """Return (first H1 / first non-blank, summary) for a profile markdown."""
    if not p.exists():
        return "", ""
    title = ""
    summary = ""
    for line in p.read_text().splitlines():
        if not title:
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                title = stripped[:160]
                continue
        if not summary:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped[:200]
                break
    return title, summary


@app.get("/api/profiles/{name}")
async def get_profile(name: str):
    profile_md = _profile_md_path(name)
    days: list[dict] = []
    master_ready = _ws_anchors.master_scripts_dir(name).is_dir()
    if master_ready:
        try:
            days = _profile_days(name)
        except HTTPException:
            days = []
    elif not profile_md.exists():
        raise HTTPException(404, f"Profile {name!r} has no master and no markdown")
    return {
        "name": name,
        "master_ready": master_ready,
        "days": days,
        "profile_md": profile_md.read_text() if profile_md.exists() else "",
    }


@app.get("/api/models")
async def list_models():
    """List configured models (keys redacted) for the model picker."""
    try:
        return _models.list_models()
    except _models.ModelError as e:
        raise HTTPException(500, str(e))


_INSTANCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _read_instance_meta(world_id: str) -> dict:
    p = _world_path(world_id) / "meta.json"
    if not p.exists():
        return {}
    try:
        return load_jsonc(p)
    except (ValueError, OSError):
        return {}


def _patch_path_for_meta(patch_dir: Path) -> str:
    patch_dir = patch_dir.resolve()
    return (str(patch_dir.relative_to(PROJECT_ROOT))
            if patch_dir.is_relative_to(PROJECT_ROOT) else str(patch_dir))


def _resolve_patch_meta_path(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        return _xai_world_patch.resolve_patch_dir(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


def _same_patch(a: Path | None, b: Path | None) -> bool:
    if not a or not b:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _write_instance_meta(target: Path, meta: dict) -> None:
    (target / "meta.json").write_text(json.dumps(meta, indent=2))


def _apply_patch_data_to_instance(target: Path, meta: dict, patch_dir: Path) -> None:
    result = _xai_world_patch.apply_data_patch(patch_dir, target)
    meta["world_patch"] = {
        "path": _patch_path_for_meta(patch_dir),
        "data_applied": result.to_dict(),
        "applied_at": _utc_now_iso(),
    }
    _write_instance_meta(target, meta)
    if not result.ok:
        raise HTTPException(500, f"world_patch/patch_data.py failed: {result.to_dict()}")


def _create_instance(profile: str, day: int, requested_name: str | None,
                     world_patch: str | Path | None = None) -> str:
    """Create a new world instance from a profile's pre-baked day-N state; returns its id."""
    snap = _ensure_day_snapshot(profile, day)
    patch_dir = _resolve_patch_meta_path(str(world_patch)) if world_patch else None
    master = _master_path(profile)
    scripts_src = master / "scripts"
    if not scripts_src.is_dir():
        raise HTTPException(500, f"profile {profile!r} master has no scripts/")

    # Instance ids are prefixed with profile and day so names never collide across profiles.
    if requested_name:
        if not _INSTANCE_NAME_RE.match(requested_name):
            raise HTTPException(400,
                "name must be lowercase letters / digits / -._ and ≤80 chars")
        instance_id = requested_name
        target = INSTANCES_DIR / instance_id
        if target.exists():
            raise HTTPException(409, f"instance {instance_id!r} already exists")
        target.mkdir(parents=True)
    else:
        # mkdir is atomic, so retrying makes concurrent same-second creates collision-free.
        target = None
        for _ in range(8):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            cand_id = f"{profile}-day{day}-{ts}-{secrets.token_hex(2)}"
            cand = INSTANCES_DIR / cand_id
            try:
                cand.mkdir(parents=True)
            except FileExistsError:
                continue
            instance_id = cand_id
            target = cand
            break
        if target is None:
            raise HTTPException(409, "could not allocate a unique instance id")
    shutil.copytree(snap / "data", target / "data", symlinks=True)
    shutil.copytree(snap / "workspace", target / "workspace", symlinks=True)
    # Web seeds ride along so env start can load them into the WebHarbor container.
    if (snap / "webharbor").is_dir():
        shutil.copytree(snap / "webharbor", target / "webharbor")
    _wh.ensure_all_webharbor_seeds(target)
    # The code overlay rides along so the gateway can serve overlay MCP apps.
    if (snap / "overlay").is_dir():
        shutil.copytree(snap / "overlay", target / "overlay")
    # Copied, not symlinked, so recompiling the master never changes this instance.
    shutil.copytree(scripts_src, target / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))

    # Day N's events have all happened: the clock reads the morning after (day N+1, 10:00).
    days_meta = _profile_days(profile)
    started_meta = next((d for d in days_meta if d["day"] == day), None)
    started_date = started_meta["date"] if started_meta else None
    next_meta = next((d for d in days_meta if d["day"] == day + 1), None)
    if next_meta and next_meta.get("date"):
        frozen_date = next_meta["date"]
    elif started_date:
        frozen_date = (_date.fromisoformat(started_date) + _td(days=1)).isoformat()
    else:
        frozen_date = None
    frozen_time = "10:00:00"

    meta = {
        "id": instance_id,
        "profile": profile,
        "name": requested_name,
        "started_at_day": day,
        "started_at_date": started_date,
        "frozen_clock_date": frozen_date,
        "frozen_clock_time": frozen_time,
        "created_at": _utc_now_iso(),
    }
    _write_instance_meta(target, meta)

    if patch_dir:
        try:
            _apply_patch_data_to_instance(target, meta, patch_dir)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    try:
        world_md = render_world_md(
            profile_name=profile,
            meta=meta,
            data_dir=target / "data",
            workspace_dir=target / "workspace",
        )
        (target / "workspace" / "WORLD.md").write_text(world_md)
        # nanobot reads USER.md as part of its system prompt.
        (target / "workspace" / "USER.md").write_text(world_md)
    except Exception as e:
        print(f"warning: could not generate WORLD.md for {instance_id}: {e}")

    return instance_id


def _restore_instance(archive: str, requested_name: str | None,
                      state: str = "after") -> str:
    """Materialise a new world instance from an archived run folder (before/after state)."""
    arc = Path(archive)
    if not arc.is_absolute():
        arc = PROJECT_ROOT / archive
    if not arc.is_dir():
        raise HTTPException(404, f"archive folder not found: {arc}")
    state = state if state in ("before", "after") else "after"
    state_tar = arc / "state" / f"{state}.tar.gz"
    if not state_tar.exists():
        raise HTTPException(404, f"no state/{state}.tar.gz in {arc}")

    manifest: dict = {}
    if (arc / "manifest.json").exists():
        try:
            manifest = load_jsonc(arc / "manifest.json")
        except (ValueError, OSError):
            manifest = {}
    world_meta = manifest.get("world", {}) or {}
    profile = world_meta.get("profile")
    day = world_meta.get("started_at_day")

    tmp = Path(tempfile.mkdtemp(prefix="awm-restore-"))
    try:
        with tarfile.open(state_tar, "r:gz") as tf:
            tf.extractall(tmp)
        snap = tmp / state
        if not (snap / "data").is_dir():
            raise HTTPException(400,
                f"archive {state}.tar.gz has no data/ — cannot restore")

        if requested_name:
            if not _INSTANCE_NAME_RE.match(requested_name):
                raise HTTPException(400,
                    "name must be lowercase letters / digits / -._ and ≤80 chars")
            instance_id = requested_name
            target = INSTANCES_DIR / instance_id
            if target.exists():
                raise HTTPException(409, f"instance {instance_id!r} already exists")
            target.mkdir(parents=True)
        else:
            base = f"{profile}-day{day}" if profile and day is not None else "restored"
            target = None
            for _ in range(8):
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                cand_id = f"{base}-restore-{ts}-{secrets.token_hex(2)}"
                cand = INSTANCES_DIR / cand_id
                try:
                    cand.mkdir(parents=True)
                except FileExistsError:
                    continue
                instance_id, target = cand_id, cand
                break
            if target is None:
                raise HTTPException(409, "could not allocate a unique instance id")

        shutil.move(str(snap / "data"), str(target / "data"))
        if (snap / "workspace").is_dir():
            shutil.move(str(snap / "workspace"), str(target / "workspace"))
        else:
            (target / "workspace").mkdir()

        # Scripts are not archived; re-copy them from the profile master (best-effort).
        if profile:
            scripts_src = _master_path(profile) / "scripts"
            if scripts_src.is_dir():
                shutil.copytree(scripts_src, target / "scripts",
                                ignore=shutil.ignore_patterns("__pycache__"))

        # Prefer the archived meta.json, re-keyed with provenance.
        meta: dict = {}
        if (snap / "meta.json").exists():
            try:
                meta = load_jsonc(snap / "meta.json")
            except (ValueError, OSError):
                meta = {}
        if not meta:
            meta = dict(world_meta)
        meta["id"] = instance_id
        meta["name"] = requested_name
        meta["restored_from"] = (
            str(arc.relative_to(PROJECT_ROOT)) if arc.is_relative_to(PROJECT_ROOT)
            else str(arc))
        meta["restored_state"] = state
        meta.setdefault("profile", profile)
        task_info = manifest.get("task") or {}
        if not meta.get("world_patch") and task_info.get("kind") == "xai":
            asset_dir = task_info.get("asset_dir")
            if asset_dir:
                task_dir = PROJECT_ROOT / asset_dir
                patch_dir = task_dir / _xai_world_patch.PATCH_DIRNAME
                if _xai_world_patch.has_patch(task_dir):
                    meta["world_patch"] = {
                        "path": _patch_path_for_meta(patch_dir),
                        "restored_runtime_patch": True,
                    }
        _write_instance_meta(target, meta)

        if (snap / "webharbor.tar.gz").exists():
            shutil.move(str(snap / "webharbor.tar.gz"),
                        str(target / "webharbor_seed.tar.gz"))

        return instance_id
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _alloc_replica_id(profile: str | None, day, src_id: str) -> str:
    base = f"{profile}-day{day}" if profile and day is not None else src_id
    for _ in range(8):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        cand = f"{base}-rep-{ts}-{secrets.token_hex(2)}"
        if not (INSTANCES_DIR / cand).exists():
            return cand
    raise HTTPException(409, "could not allocate a unique replica id")


def _clone_instance_dir(src: Path, target: Path, new_id: str) -> None:
    """Copy the on-disk world into a new instance dir with a re-keyed meta id."""
    target.mkdir(parents=True)
    for sub in ("data", "workspace", "scripts", "webharbor"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, target / sub, symlinks=True)
    meta: dict = {}
    if (src / "meta.json").exists():
        try:
            meta = load_jsonc(src / "meta.json")
        except (ValueError, OSError):
            meta = {}
    meta["id"] = new_id
    meta["replicated_from"] = src.name
    (target / "meta.json").write_text(json.dumps(meta, indent=2))


def _destroy_instance(world_id: str) -> None:
    """Stop the runtime if any, then rmtree the instance dir + state."""
    info = _runtimes.get(world_id)
    project = info["project"] if info else _project_name(world_id)
    # Try every backend compose file; each is a no-op if the project is absent.
    for cf in {b.compose_file() for b in _agents.REGISTRY.values()}:
        try:
            # Pass every profile so profile-gated services are torn down too.
            subprocess.run(
                ["docker", "compose", "-f", cf, *_ALL_PROFILE_ARGS,
                 "-p", project, "down", "--remove-orphans"],
                cwd=str(PROJECT_ROOT), check=False,
                capture_output=True, timeout=RUNTIME_SHORT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            pass
    _cleanup_empty_project_network(project)
    with _alloc_lock:
        _runtimes.pop(world_id, None)

    target = INSTANCES_DIR / world_id
    if target.exists():
        # Force-remove read-only files such as git objects.
        def _rm(func, path, exc_info):
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        shutil.rmtree(target, onerror=_rm)
    eps = _episodes_dir(world_id)
    if eps.exists():
        shutil.rmtree(eps, ignore_errors=True)
    # Wipe the per-world state dir; the next runtime start regenerates it.
    state_dir = NANOBOT_STATE_DIR / world_id
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)


@app.get("/api/worlds")
async def list_worlds():
    """List all world instances grouped by profile."""
    instances: list[dict] = []
    if not INSTANCES_DIR.is_dir():
        return {"worlds": [], "by_profile": {}}
    world_dirs = [d for d in sorted(INSTANCES_DIR.iterdir()) if d.is_dir()]
    running_ids = [d.name for d in world_dirs if d.name in _runtimes]
    running_rows = await asyncio.gather(*(
        asyncio.to_thread(_runtime_info, world_id)
        for world_id in running_ids
    ))
    runtime_by_id = dict(zip(running_ids, running_rows))
    for d in world_dirs:
        meta = _read_instance_meta(d.name)
        runtime = runtime_by_id.get(d.name, {"running": False})
        # Stat the live instance data, not the master, so agent changes show up.
        scripts_dir = d / "scripts"
        data_dir = d / "data"
        ws_dir = d / "workspace"
        instances.append({
            "id": d.name,
            "profile": meta.get("profile", ""),
            "name": meta.get("name"),
            "started_at_day": meta.get("started_at_day"),
            "started_at_date": meta.get("started_at_date"),
            "created_at": meta.get("created_at"),
            "running": runtime.get("running", False),
            "mcp_port": runtime.get("mcp_port"),
            "script_count": len(list(scripts_dir.glob("*.py"))) if scripts_dir.is_dir() else 0,
            "db_count": len(list(data_dir.glob("*.db"))) if data_dir.is_dir() else 0,
            "has_workspace": ws_dir.is_dir(),
        })
    by_profile: dict[str, list[dict]] = {}
    for inst in instances:
        by_profile.setdefault(inst.get("profile") or "(unknown)", []).append(inst)
    return {"worlds": instances, "by_profile": by_profile}


@app.post("/api/worlds")
async def create_world(request: Request):
    """Create a new instance from a profile's pre-baked day snapshot."""
    body = await request.json()
    name = (body.get("name") or "").strip() or None

    path = (body.get("path") or "").strip()
    if path:
        state = (body.get("state") or "after").strip()
        instance_id = await asyncio.to_thread(
            _restore_instance, path, name, state)
        meta = _read_instance_meta(instance_id)
        return {"id": instance_id, "meta": meta, "restored": True,
                "restored_from": path, "state": state}

    profile = body.get("profile")
    day = body.get("day")
    if not profile or day is None:
        raise HTTPException(400, "profile and day are required (or pass path to restore)")
    try:
        day = int(day)
    except (TypeError, ValueError):
        raise HTTPException(400, "day must be an integer")
    instance_id = await asyncio.to_thread(
        _create_instance, profile, day, name, body.get("world_patch"))
    meta = _read_instance_meta(instance_id)
    return {"id": instance_id, "meta": meta}


@app.delete("/api/worlds/{world_id}")
async def destroy_world(world_id: str, request: Request):
    _world_path(world_id)
    await asyncio.to_thread(_destroy_instance, world_id)
    return {"destroyed": True, "id": world_id}


@app.post("/api/worlds/{world_id}/replicate")
async def replicate_world(world_id: str, request: Request):
    """Clone a world N times with its WebHarbor site DBs; body {n, start, webharbor}."""
    src = _world_path(world_id)
    body = await request.json() if (await request.body()) else {}
    n = max(1, int(body.get("n", 1)))
    start = body.get("start")
    start = True if start is None else bool(start)
    webharbor = body.get("webharbor")
    webharbor = True if webharbor is None else bool(webharbor)
    meta = _read_instance_meta(world_id)
    profile, day = meta.get("profile"), meta.get("started_at_day")

    # Capture the source's WebHarbor site DBs once, if its container is up.
    wh_tmp = Path(tempfile.mkdtemp(prefix="awm-rep-wh-"))
    await asyncio.to_thread(_snapshot_webharbor, world_id, wh_tmp)
    wh_seed = wh_tmp / "webharbor.tar.gz"
    has_wh = wh_seed.exists()

    created: list[str] = []
    started: list[str] = []
    try:
        for _ in range(n):
            new_id = _alloc_replica_id(profile, day, world_id)
            target = INSTANCES_DIR / new_id
            await asyncio.to_thread(_clone_instance_dir, src, target, new_id)
            if has_wh:
                shutil.copyfile(wh_seed, target / "webharbor_seed.tar.gz")
            created.append(new_id)
        if start:
            for nid in created:
                try:
                    await asyncio.to_thread(_ensure_env, nid, webharbor)
                    started.append(nid)
                except HTTPException as e:
                    log.warning("replica %s start failed: %s", nid, e.detail)
        return {"source": world_id, "created": created, "started": started,
                "webharbor_captured": bool(has_wh)}
    finally:
        shutil.rmtree(wh_tmp, ignore_errors=True)


@app.post("/api/worlds/destroy-all")
async def destroy_all_worlds(request: Request):
    """Destroy every world instance, optionally scoped to one profile."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    profile = (body.get("profile") or "").strip() or None

    if not INSTANCES_DIR.is_dir():
        return {"destroyed": [], "failed": [], "scope": profile or "all"}

    targets: list[str] = []
    for d in sorted(INSTANCES_DIR.iterdir()):
        if not d.is_dir():
            continue
        if profile is not None:
            meta = _read_instance_meta(d.name)
            if meta.get("profile") != profile:
                continue
        targets.append(d.name)

    destroyed: list[str] = []
    failed: list[dict] = []
    for wid in targets:
        try:
            await asyncio.to_thread(_destroy_instance, wid)
            destroyed.append(wid)
        except Exception as e:
            failed.append({"id": wid, "error": str(e)})

    return {"destroyed": destroyed, "failed": failed, "scope": profile or "all"}


@app.get("/api/worlds/{world_id}")
async def get_world(world_id: str):
    return {
        "id": world_id,
        "meta": _read_instance_meta(world_id),
    }


@app.get("/api/worlds/{world_id}/overview")
async def get_world_overview(world_id: str):
    wp = _world_path(world_id)
    data_dir = wp / "data"
    ws_dir = wp / "workspace"
    inst_meta = _read_instance_meta(world_id)
    deployed_day = inst_meta.get("started_at_day")
    profile_name = str(inst_meta.get("profile") or "")
    day_breakdown = {}
    if profile_name:
        for row in _profile_days(profile_name):
            day = int(row["day"])
            day_breakdown[f"{day:02d}"] = {
                "day_num": day,
                "date": row.get("date"),
                "events": list(row.get("events") or []),
                "total_files": 0,
            }
    all_event_types = {
        event["event"]
        for row in day_breakdown.values()
        for event in row["events"]
        if event.get("event")
    }

    db_rows = {}
    total_rows = 0
    if data_dir.exists():
        for db_file in sorted(data_dir.glob("*.db")):
            app_id = db_file.stem
            try:
                tables = _get_table_names(db_file)
                count = sum(_get_row_count(db_file, t) for t in tables)
                db_rows[app_id] = count
                total_rows += count
            except Exception:
                db_rows[app_id] = 0

    ws_file_count = 0
    ws_total_size = 0
    if ws_dir.exists():
        for f in ws_dir.rglob("*"):
            if f.is_file() and ".git/" not in str(f):
                ws_file_count += 1
                ws_total_size += f.stat().st_size

    # Count only events up to started_at_day; later events have not happened yet.
    deployed_events = 0
    deployed_event_types = set()
    deployed_files_produced = 0
    horizon = deployed_day if deployed_day is not None else -1
    for dk, day_info in day_breakdown.items():
        if day_info["day_num"] <= horizon:
            deployed_events += len(day_info["events"])
            for ev in day_info["events"]:
                deployed_event_types.add(ev["event"])
            deployed_files_produced += day_info["total_files"]

    stories = {}
    profile_md = ""
    if profile_name:
        profile_path = _profile_md_path(profile_name)
        if profile_path.exists():
            profile_md = profile_path.read_text()
        story_dir = STORIES_DIR / profile_name
        if story_dir.exists():
            for day_info in day_breakdown.values():
                day = day_info["day_num"]
                story_file = story_dir / f"day_{day}.md"
                if story_file.exists():
                    stories[str(day_info.get("date") or f"day_{day}")] = story_file.read_text()

    state_md = ""
    state_path = wp / "state.md"
    if state_path.exists():
        state_md = state_path.read_text()

    xai_tasks = _xai_tasks_for_world(world_id)
    xai_task_count = len(xai_tasks)
    xai_task_ready = sum(1 for t in xai_tasks if t.get("has_assets"))

    return {
        "world_id": world_id,
        "deployed_day": deployed_day,
        "profile": profile_name,
        "xai_task_count": xai_task_count,
        "xai_task_ready": xai_task_ready,
        "total_days": len([d for d in day_breakdown if d != "00"]),
        "total_events_all": sum(len(d["events"]) for d in day_breakdown.values()),
        "total_events_deployed": deployed_events,
        "unique_event_types_all": sorted(all_event_types),
        "unique_event_type_count_all": len(all_event_types),
        "unique_event_type_count_deployed": len(deployed_event_types),
        "total_files_produced_deployed": deployed_files_produced,
        "total_db_rows": total_rows,
        "db_rows_by_app": db_rows,
        "ws_file_count": ws_file_count,
        "ws_total_size_bytes": ws_total_size,
        "days": {k: v for k, v in sorted(day_breakdown.items())},
        "stories": stories,
        "state_md": state_md,
        "profile_md": profile_md,
    }


@app.get("/api/worlds/{world_id}/db")
async def list_databases(world_id: str):
    wp = _world_path(world_id)
    data_dir = wp / "data"
    if not data_dir.exists():
        return {"databases": []}
    dbs = []
    for db_file in sorted(data_dir.glob("*.db")):
        tables = _get_table_names(db_file)
        total_rows = 0
        for t in tables:
            try:
                total_rows += _get_row_count(db_file, t)
            except Exception:
                pass
        dbs.append({"app_id": db_file.stem, "tables": len(tables), "total_rows": total_rows})
    return {"databases": dbs}


@app.get("/api/worlds/{world_id}/db/{app_id}/tables")
async def list_tables(world_id: str, app_id: str):
    db = _db_path(world_id, app_id)
    tables = []
    for t in _get_table_names(db):
        schema = _get_table_schema(db, t)
        count = _get_row_count(db, t)
        tables.append({"name": t, "columns": schema, "row_count": count})
    return {"app_id": app_id, "tables": tables}


@app.get("/api/worlds/{world_id}/db/{app_id}/table/{table}")
async def get_table_data(world_id: str, app_id: str, table: str,
                         page: int = Query(1, ge=1),
                         per_page: int = Query(50, ge=1, le=500)):
    db = _db_path(world_id, app_id)
    valid_tables = _get_table_names(db)
    if table not in valid_tables:
        raise HTTPException(404, f"Table not found: {table}")
    total = _get_row_count(db, table)
    offset = (page - 1) * per_page
    rows = _query_db(db, f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (per_page, offset))
    schema = _get_table_schema(db, table)
    return {
        "table": table,
        "columns": schema,
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


@app.get("/api/worlds/{world_id}/db/{app_id}/query")
async def query_database(world_id: str, app_id: str,
                         sql: str = Query(..., description="SELECT query"),
                         limit: int = Query(100, ge=1, le=1000)):
    _safe_sql(sql)
    db = _db_path(world_id, app_id)
    if "LIMIT" not in sql.upper():
        sql = f"{sql.rstrip(';')} LIMIT {limit}"
    try:
        rows = _query_db(db, sql)
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"SQL error: {e}")
    return {"sql": sql, "rows": rows, "count": len(rows)}


@app.get("/api/worlds/{world_id}/files")
async def list_files(world_id: str, path: str = Query("/", description="Relative path")):
    wp = _world_path(world_id)
    ws = wp / "workspace"
    if not ws.exists():
        return {"path": path, "entries": []}
    target = (ws / path.lstrip("/")).resolve()
    if not str(target).startswith(str(ws.resolve())):
        raise HTTPException(403, "Path traversal not allowed")
    if not target.exists():
        raise HTTPException(404, f"Path not found: {path}")
    if not target.is_dir():
        raise HTTPException(400, "Not a directory. Use /files/read for file content.")

    entries = []
    for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
        if ".git" in item.parts:
            continue
        rel = str(item.relative_to(ws))
        if item.is_dir():
            child_count = sum(1 for _ in item.iterdir() if ".git" not in str(_))
            entries.append({"name": item.name, "path": rel, "type": "dir", "children": child_count})
        else:
            entries.append({
                "name": item.name, "path": rel, "type": "file",
                "size": item.stat().st_size,
                "ext": item.suffix,
            })
    return {"path": path, "entries": entries}


@app.get("/api/worlds/{world_id}/files/read")
async def read_file(world_id: str, path: str = Query(..., description="Relative file path")):
    wp = _world_path(world_id)
    ws = wp / "workspace"
    target = (ws / path.lstrip("/")).resolve()
    if not str(target).startswith(str(ws.resolve())):
        raise HTTPException(403, "Path traversal not allowed")
    if not target.exists():
        raise HTTPException(404, f"File not found: {path}")
    if target.is_dir():
        raise HTTPException(400, "Path is a directory")
    try:
        content = target.read_text(errors="replace")
        return {"path": path, "content": content, "size": target.stat().st_size}
    except Exception:
        content = base64.b64encode(target.read_bytes()).decode()
        return {"path": path, "content": content, "size": target.stat().st_size, "binary": True}


@app.get("/api/worlds/{world_id}/files/tree")
async def file_tree(world_id: str):
    wp = _world_path(world_id)
    ws = wp / "workspace"
    if not ws.exists():
        return {"tree": []}

    def build_tree(dir_path: Path, depth: int = 0) -> list:
        if depth > 6:
            return []
        items = []
        for item in sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if ".git" in item.parts or item.name.startswith(".git"):
                continue
            rel = str(item.relative_to(ws))
            if item.is_dir():
                children = build_tree(item, depth + 1)
                items.append({"name": item.name, "path": rel, "type": "dir", "children": children})
            else:
                items.append({"name": item.name, "path": rel, "type": "file", "size": item.stat().st_size})
        return items

    return {"tree": build_tree(ws)}


def _safe_world_id(world_id: str) -> str:
    # Compose project names must be lowercase [a-z0-9_-].
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in world_id.lower())


def _project_name_for(kind: str, world_id: str) -> str:
    """Resolve the docker compose project name for (kind, world) via its backend."""
    return _agents.get(kind).project_name(_safe_world_id(world_id))


def _project_name(world_id: str) -> str:
    """Return the compose project name owned by the default environment backend."""
    return _project_name_for(_agents.DEFAULT_KIND, world_id)


def _docker_host_port(container: str, container_port: int) -> int | None:
    """Ask docker which host port a container's published port maps to."""
    try:
        out = subprocess.check_output(
            ["docker", "port", container, str(container_port)],
            stderr=subprocess.DEVNULL, timeout=HTTP_PROBE_TIMEOUT_S,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
    return None


def _container_env_value(container: str, key: str) -> str | None:
    """Read one env var from a running container (recovers the worldctl token after a restart)."""
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f",
             "{{range .Config.Env}}{{println .}}{{end}}", container],
            stderr=subprocess.DEVNULL, timeout=HTTP_PROBE_TIMEOUT_S,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    prefix = f"{key}="
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _resync_runtimes_from_docker() -> None:
    """Rebuild _runtimes from live containers (keyed off each mcp container) after a restart."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"],
            stderr=subprocess.DEVNULL, timeout=SUBPROCESS_FAST_TIMEOUT_S,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return

    names = [n.strip() for n in out.strip().splitlines() if n.strip()]
    mcp_prefix = "wildawm-mcp-"
    for name in names:
        if not name.startswith(mcp_prefix):
            continue
        world_id = name[len(mcp_prefix):]
        if not world_id:
            continue
        mcp_port = _docker_host_port(name, MCP_INTERNAL_PORT)
        if not mcp_port:
            continue

        webharbor_name = f"wildawm-webharbor-{world_id}"
        webharbor_port = _docker_host_port(webharbor_name, WEBHARBOR_CTRL_PORT) \
            if webharbor_name in names else None

        # Recover worldctl's host port and token so agent calls keep working after a restart.
        worldctl_name = f"wildawm-worldctl-{world_id}"
        worldctl_port = _docker_host_port(worldctl_name, WORLDCTL_PORT) \
            if worldctl_name in names else None
        worldctl_token = _container_env_value(worldctl_name, "AWM_WORLDCTL_TOKEN") \
            if worldctl_port else None

        agents: dict[str, dict] = {}
        for backend in _agents.REGISTRY.values():
            cname = backend.container_name(world_id)
            if cname not in names:
                continue
            port = None
            if backend.publishes_port():
                port = _docker_host_port(cname, backend.internal_port())
            agents[backend.KIND] = {
                "agent_port": port, "started_at": None,
                "model": _models.ref_for(
                    _container_env_value(cname, "AWM_OPENAI_BASE_URL"),
                    _container_env_value(cname, "AWM_OPENAI_MODEL")),
                "reasoning_effort": (
                    _container_env_value(cname, "AWM_REASONING_EFFORT") or None),
            }

        _runtimes[world_id] = {
            "project": _project_name(world_id),
            "mcp_port": mcp_port,
            "webharbor_port": webharbor_port,
            "worldctl_port": worldctl_port,
            "worldctl_token": worldctl_token,
            "started_at": None,
            "agents": agents,
        }


def _port_bindable(p: int) -> bool:
    """True iff host port `p` is free at the OS level, including non-docker listeners."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _allocate_port(base: int, used: set[int]) -> int:
    p = base
    while p in used or not _port_bindable(p):
        p += 1
    return p


def _docker_published_ports(
    process_env: Mapping[str, str] | None = None,
) -> set[int]:
    """All host ports published by any docker container, even when `_runtimes` is stale."""
    used: set[int] = set()
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Ports}}"],
            stderr=subprocess.DEVNULL, timeout=SUBPROCESS_FAST_TIMEOUT_S,
            env=process_env,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return used
    for m in re.finditer(r":(\d+)->", out):
        try:
            used.add(int(m.group(1)))
        except ValueError:
            pass
    return used


def _compose_wildawm_networks(
    process_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Docker networks created by this compose file's `wildawm` network."""
    names: list[str] = []
    try:
        out = subprocess.check_output(
            [
                "docker", "network", "ls",
                "--filter", "label=com.docker.compose.network=wildawm",
                "--format", "{{.Name}}",
            ],
            stderr=subprocess.DEVNULL, timeout=SUBPROCESS_FAST_TIMEOUT_S,
            env=process_env,
        ).decode()
        names = [line.strip() for line in out.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        names = []
    if names:
        return names

    # Unlabelled leftovers: keep the fallback narrow so unrelated networks are never touched.
    try:
        out = subprocess.check_output(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            stderr=subprocess.DEVNULL, timeout=SUBPROCESS_FAST_TIMEOUT_S,
            env=process_env,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [
        line.strip() for line in out.splitlines()
        if line.strip().startswith("wildawm-") and line.strip().endswith("_wildawm")
    ]


def _empty_docker_networks(
    names: list[str], process_env: Mapping[str, str] | None = None,
) -> list[str]:
    if not names:
        return []
    empty: list[str] = []
    for i in range(0, len(names), 100):
        batch = names[i:i + 100]
        try:
            out = subprocess.check_output(
                ["docker", "network", "inspect", *batch,
                 "--format", "{{.Name}} {{len .Containers}}"],
                stderr=subprocess.DEVNULL, timeout=SUBPROCESS_FAST_TIMEOUT_S,
                env=process_env,
            ).decode()
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            parts = line.rsplit(" ", 1)
            if len(parts) == 2 and parts[1] == "0":
                empty.append(parts[0])
    return empty


def _remove_docker_networks(
    names: list[str], process_env: Mapping[str, str] | None = None,
) -> int:
    removed = 0
    for i in range(0, len(names), 100):
        batch = names[i:i + 100]
        try:
            result = subprocess.run(
                ["docker", "network", "rm", *batch],
                cwd=str(PROJECT_ROOT), capture_output=True,
                timeout=RUNTIME_SHORT_TIMEOUT_S,
                env=process_env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode in (0, 1):
            removed += sum(
                1 for line in result.stdout.decode(errors="replace").splitlines()
                if line.strip()
            )
    return removed


def _cleanup_empty_project_network(
    project: str, process_env: Mapping[str, str] | None = None,
) -> int:
    """Remove this compose project's bridge network after down/failed up."""
    name = f"{project}_wildawm"
    empty = _empty_docker_networks([name], process_env)
    return _remove_docker_networks(empty, process_env)


def _cleanup_empty_wildawm_networks(
    process_env: Mapping[str, str] | None = None,
) -> int:
    """Recover address-pool capacity without touching active worlds."""
    empty = _empty_docker_networks(
        _compose_wildawm_networks(process_env), process_env)
    removed = _remove_docker_networks(empty, process_env)
    if removed:
        log.warning("removed %s empty wildawm docker networks", removed)
    return removed


def _docker_address_pool_exhausted(stderr: str) -> bool:
    return "all predefined address pools have been fully subnetted" in stderr


def _docker_port_bind_failed(stderr: str) -> bool:
    blob = stderr.lower()
    return (
        "failed to bind host port" in blob
        or "address already in use" in blob
        or "port is already allocated" in blob
    )


def _compose_down_project(project: str, env: dict) -> None:
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, *_ALL_PROFILE_ARGS,
         "-p", project, "down", "--remove-orphans"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True,
        timeout=RUNTIME_SHORT_TIMEOUT_S,
    )


def _compose_up_env_with_network_recovery(up_cmd: list[str], env: dict, project: str) -> None:
    for attempt in (1, 2, 3):
        try:
            subprocess.run(up_cmd, cwd=str(PROJECT_ROOT), env=env,
                           check=True, capture_output=True,
                           timeout=RUNTIME_SHORT_TIMEOUT_S)
            return
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")
            stdout = (e.stdout or b"").decode(errors="replace")
            detail = stderr + stdout
            if attempt == 1 and _docker_address_pool_exhausted(detail):
                removed = _cleanup_empty_wildawm_networks(env)
                _cleanup_empty_project_network(project, env)
                log.warning(
                    "env compose up hit docker address-pool exhaustion for %s; "
                    "removed %s empty wildawm networks and retrying",
                    project, removed,
                )
                continue
            if _docker_port_bind_failed(detail):
                _compose_down_project(project, env)
                log.warning(
                    "env compose up hit host-port bind failure for %s; "
                    "cleaned project and returning to port allocator",
                    project,
                )
                raise HTTPException(500, f"env compose up failed: {detail}")
            _compose_down_project(project, env)
            _cleanup_empty_project_network(project, env)
            detail = stderr or stdout or str(e)
            log.error("env compose up failed project=%s detail=%s", project, detail)
            raise HTTPException(500, f"env compose up failed: {detail}")
        except subprocess.TimeoutExpired:
            _compose_down_project(project, env)
            _cleanup_empty_project_network(project, env)
            raise HTTPException(504, "env compose up timed out")


def _container_status(
    project: str, process_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return {service_name: state} from `docker compose ps --format json`."""
    try:
        out = subprocess.check_output(
            ["docker", "compose", "-p", project, "ps", "--format", "json"],
            cwd=str(PROJECT_ROOT), stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_FAST_TIMEOUT_S,
            env=process_env,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    services: dict[str, str] = {}
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            for item in obj:
                services[item.get("Service", "")] = item.get("State", "")
        else:
            services[obj.get("Service", "")] = obj.get("State", "")
    return services


def _runtime_info(world_id: str) -> dict:
    info = _runtimes.get(world_id)
    if not info:
        return {"running": False, "env_running": False, "agents": {}}
    services = _container_status(info["project"])
    mcp_state = services.get("mcp", "missing")
    env_running = mcp_state == "running"
    webharbor_port = info.get("webharbor_port")
    webharbor_state = services.get("webharbor", "missing") if webharbor_port else "absent"

    # A kind is running iff its agent-<kind> compose service is up; reconcile with _runtimes.
    tracked = info.get("agents", {})
    agents_out: dict[str, dict] = {}
    any_agent = False
    for backend in _agents.REGISTRY.values():
        kind = backend.KIND
        svc_state = services.get(backend.service_name(), "missing")
        a_running = svc_state == "running"
        if a_running:
            any_agent = True
        agents_out[kind] = {
            "running": a_running,
            "state": svc_state,
            "agent_port": (tracked.get(kind) or {}).get("agent_port"),
            "started_at": (tracked.get(kind) or {}).get("started_at"),
            "model": (tracked.get(kind) or {}).get("model"),
            "reasoning_effort": (tracked.get(kind) or {}).get("reasoning_effort"),
            "publishes_port": backend.publishes_port(),
        }

    return {
        # `running` means the world is usable, i.e. the environment is up.
        "running": env_running,
        "env_running": env_running,
        "any_agent_running": any_agent,
        "agents": agents_out,
        "project": info["project"],
        "mcp_port": info["mcp_port"],
        "webharbor_port": webharbor_port,
        "webharbor_state": webharbor_state,
        "worldctl_port": info.get("worldctl_port"),
        "worldctl_state": services.get("worldctl", "missing"),
        "started_at": info.get("started_at"),
        "services": services,
    }


@app.get("/api/worlds/{world_id}/runtime")
async def runtime_status(world_id: str, request: Request):
    _world_path(world_id)
    return await asyncio.to_thread(_runtime_info, world_id)


_COMPOSE_FILE_RAW = Path(_agents.get(_agents.DEFAULT_KIND).compose_file())
COMPOSE_FILE = str(
    _COMPOSE_FILE_RAW if _COMPOSE_FILE_RAW.is_absolute()
    else PROJECT_ROOT / _COMPOSE_FILE_RAW
)


def _base_compose_env(
        world_id: str, agent_turn_timeout_s: int | None = None) -> dict:
    """Env shared by every compose invocation for a world."""
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if (any(fragment in upper for fragment in (
                "SECRET", "PASSWORD", "TOKEN", "API_KEY", "CREDENTIAL"))
                or key in {
                "SANDBOX_API_KEY",
                "SANDBOX_BASE_URL",
            }):
            env.pop(key, None)
    turn_timeout_s = (
        DEFAULT_AGENT_TURN_TIMEOUT_S
        if agent_turn_timeout_s is None else int(agent_turn_timeout_s)
    )
    env.setdefault("AWM_AGENT_IMAGE", runtime_cfg.OPENCODE_AGENT_IMAGE)
    # The mcp gateway must be the overlay-aware image unless MCP_IMAGE is pinned.
    env.setdefault("MCP_IMAGE", "wildawm-mcp:worlds")
    env.update({
        # Containers write as the host user so the workspace stays writable from both sides.
        "AWM_HOST_UID": str(os.getuid()),
        "AWM_HOST_GID": str(os.getgid()),
        "AWM_WORLD_ID": world_id,
        # Keep compose bind mounts aligned with the dashboard's runtime state dirs.
        "AWM_INSTANCES_DIR": str(INSTANCES_DIR),
        "AWM_NANOBOT_STATE_DIR": str(NANOBOT_STATE_DIR),
        "AWM_AGENT_TURN_TIMEOUT": str(turn_timeout_s),
        "AWM_LLM_PROXY_UPSTREAM_TIMEOUT": str(LLM_PROXY_UPSTREAM_TIMEOUT_S),
        "AWM_OPENCLAW_PROVIDER_TIMEOUT": str(OPENCLAW_PROVIDER_TIMEOUT_S),
        # Reasoning knobs reach containers only after per-model validation in _attach_agent().
        "AWM_REASONING_EFFORT": "",
        "AWM_REASONING_CHAT_PARAM": "reasoning_effort",
        "AWM_REASONING_RESPONSES_PARAM": "reasoning",
        "AWM_REASONING_FAIL_IF_STRIPPED": "1",
    })
    return env


def _runtime_port_env(info: dict) -> dict[str, str]:
    env = {"AWM_MCP_PORT": str(info["mcp_port"])}
    if info.get("webharbor_port"):
        env["AWM_WEBHARBOR_CTRL_HOST_PORT"] = str(info["webharbor_port"])
        env["AWM_WEBHARBOR_CTRL_PORT"] = str(WEBHARBOR_CTRL_PORT)
    return env


def _agent_compose_up_cmd(kind: str, project: str, service: str) -> list[str]:
    return [
        "docker", "compose", "-f", COMPOSE_FILE, "--profile", kind,
        "-p", project, "up", "-d", "--no-deps", service,
    ]


def _worldctl_for(world_id: str) -> tuple[int, str | None]:
    """Return (host_port, token) for a world's worldctl, or raise 409 if the env is down."""
    info = _runtimes.get(world_id)
    if not info or not info.get("worldctl_port"):
        raise HTTPException(409, "World control server is not running — start the world first")
    return info["worldctl_port"], info.get("worldctl_token")


def _wait_worldctl_ready(port: int, timeout_s: float = AGENT_READY_TIMEOUT_S) -> None:
    """Block until worldctl's /health answers, or raise 504."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            _worldctl.health(port)
            return
        except _worldctl.WorldctlError as e:
            last = str(e)
        time.sleep(1)
    raise HTTPException(504, f"worldctl not ready in {timeout_s:.0f}s on :{port}: {last}")


def _wait_agent_ready(
        world_id: str, kind: str, timeout_s: float = AGENT_READY_TIMEOUT_S) -> bool:
    """Poll worldctl until the agent's in-container adapter reports ready."""
    port, token = _worldctl_for(world_id)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _worldctl.agent_ready(port, token, kind):
            return True
        time.sleep(2)
    return False


def _world_patch_dir_for_instance(world_id: str) -> Path | None:
    meta = _read_instance_meta(world_id)
    wp_meta = meta.get("world_patch") or {}
    return _resolve_patch_meta_path(wp_meta.get("path"))


def _apply_runtime_patch_for_world(world_id: str, project: str) -> dict:
    patch_dir = _world_patch_dir_for_instance(world_id)
    if not patch_dir:
        return {"ok": True, "skipped": True}
    result = _xai_world_patch.apply_runtime_patch(
        patch_dir,
        world_id=world_id,
        project=project,
        instance_dir=_world_path(world_id),
    )
    target = _world_path(world_id)
    meta = _read_instance_meta(world_id)
    meta.setdefault("world_patch", {})["path"] = _patch_path_for_meta(patch_dir)
    meta["world_patch"]["runtime_applied"] = result.to_dict()
    meta["world_patch"]["runtime_applied_at"] = _utc_now_iso()
    _write_instance_meta(target, meta)
    if not result.ok:
        raise HTTPException(500, f"world_patch/patch_runtime.py failed: {result.to_dict()}")
    return result.to_dict()


def _apply_overlay_for_world(world_id: str) -> dict:
    """Apply the world overlay's WebHarbor code patches to the live container (no-op without one)."""
    wp = _world_path(world_id)
    if not _ws_overlay.has_overlay(wp):
        return {"ok": True, "skipped": True}
    wh_container = f"wildawm-webharbor-{world_id}"
    if not _wc.is_running(wh_container):
        return {"ok": True, "skipped": True, "reason": "webharbor not running"}
    res = _ws_overlay.apply_runtime(wp, webharbor_container=wh_container)
    meta = _read_instance_meta(world_id)
    meta.setdefault("overlay", {})["runtime_applied"] = res
    meta["overlay"]["runtime_applied_at"] = _utc_now_iso()
    _write_instance_meta(wp, meta)
    if res.get("errors"):
        log.warning("overlay apply errors for %s: %s", world_id, res["errors"])
    return res


def _reserve_env_ports(
    enable_webharbor: bool,
    process_env: Mapping[str, str] | None = None,
) -> tuple[int, int | None, int, set[int]]:
    """Allocate and reserve the host ports for one env start attempt."""
    with _alloc_lock:
        docker_used = _docker_published_ports(process_env)
        live = list(_runtimes.values())
        used_mcp = {r["mcp_port"] for r in live if r.get("mcp_port")} \
            | docker_used | _reserved_ports
        mcp_port = _allocate_port(_MCP_PORT_BASE, used_mcp)
        webharbor_port: int | None = None
        if enable_webharbor:
            used_wh = {r["webharbor_port"] for r in live if r.get("webharbor_port")} \
                | docker_used | _reserved_ports | {mcp_port}
            webharbor_port = _allocate_port(_WEBHARBOR_PORT_BASE, used_wh)

        used_wc = {r["worldctl_port"] for r in live if r.get("worldctl_port")} \
            | docker_used | _reserved_ports | {mcp_port}
        if webharbor_port:
            used_wc.add(webharbor_port)
        worldctl_port = _allocate_port(_WORLDCTL_PORT_BASE, used_wc)

        reserved = {mcp_port, worldctl_port} \
            | ({webharbor_port} if webharbor_port else set())
        _reserved_ports.update(reserved)
    return mcp_port, webharbor_port, worldctl_port, reserved


def _ensure_env(world_id: str, enable_webharbor: bool) -> dict:
    """Bring up (or reuse) a world's agent-agnostic environment: mcp, webharbor, worldctl."""
    wp = _world_path(world_id)
    if not (wp / "data").exists() or not any((wp / "data").glob("*.db")):
        raise HTTPException(400, "World has no compiled data/ — materialize it first")

    project = _project_name(world_id)

    existing = _runtimes.get(world_id)
    if existing:
        existing_status = _container_status(project)
        if existing_status.get("mcp") == "running":
            # Env already up; webharbor is only decided at first start.
            return existing

    worldctl_token = ((existing or {}).get("worldctl_token") if existing else None) \
        or secrets.token_hex(24)

    last_bind_error: HTTPException | None = None
    bind_attempts = max(1, ENV_PORT_BIND_ATTEMPTS)
    for bind_attempt in range(1, bind_attempts + 1):
        # Reserve ports avoiding _runtimes and live bindings; on a bind race, retry with fresh ports.
        mcp_port, webharbor_port, worldctl_port, _my_reserved = (
            _reserve_env_ports(enable_webharbor)
        )

        try:
            # Freeze the container clock to the world's date (shared faketimerc).
            state_dir = NANOBOT_STATE_DIR / world_id
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "logs").mkdir(parents=True, exist_ok=True)
            faketimerc = state_dir / "faketimerc"
            today = _world_current_day(world_id)
            fdate = today.get("current_date")
            ftime = today.get("current_time") or "10:00:00"
            if faketimerc.is_dir():
                shutil.rmtree(faketimerc)
            faketimerc.write_text(f"@{fdate} {ftime}\n" if fdate else "\n")

            env = _base_compose_env(world_id)
            env["AWM_MCP_PORT"] = str(mcp_port)
            env["AWM_WORLDCTL_PORT_HOST"] = str(worldctl_port)
            env["AWM_WORLDCTL_TOKEN"] = worldctl_token
            services = ["mcp", "worldctl"]
            profile_args: list[str] = []
            if enable_webharbor:
                env["AWM_WEBHARBOR_CTRL_HOST_PORT"] = str(webharbor_port)
                env["AWM_WEBHARBOR_CTRL_PORT"] = str(WEBHARBOR_CTRL_PORT)
                (state_dir / "caddy-ca").mkdir(parents=True, exist_ok=True)
                profile_args = ["--profile", "webharbor"]
                services.append("webharbor")

            # Remove stale env containers from a half-failed prior start.
            subprocess.run(
                ["docker", "compose", "-f", COMPOSE_FILE, *profile_args, "-p", project,
                 "rm", "-sf", *services],
                cwd=str(PROJECT_ROOT), env=env, capture_output=True,
                timeout=RUNTIME_SHORT_TIMEOUT_S,
            )

            up_cmd = ["docker", "compose", "-f", COMPOSE_FILE, *profile_args,
                      "-p", project, "up", "-d", *services]
            print(f"[env_start] {world_id}: {' '.join(up_cmd[-4:])}")
            try:
                _compose_up_env_with_network_recovery(up_cmd, env, project)
            except HTTPException as e:
                detail = str(getattr(e, "detail", ""))
                if (getattr(e, "status_code", None) == 500
                        and _docker_port_bind_failed(detail)
                        and bind_attempt < bind_attempts):
                    last_bind_error = e
                    log.warning(
                        "env compose up hit persistent host-port bind failure "
                        "for %s on attempt %s; reallocating ports mcp=%s "
                        "webharbor=%s worldctl=%s",
                        project, bind_attempt, mcp_port, webharbor_port,
                        worldctl_port,
                    )
                    continue
                raise

            # Profile-gated webharbor can miss the compose network; connect it with site DNS aliases.
            if enable_webharbor:
                try:
                    _connect_webharbor_network(world_id, project)
                except HTTPException:
                    _compose_down_project(project, env)
                    _cleanup_empty_project_network(project)
                    raise

            _wait_worldctl_ready(worldctl_port, timeout_s=AGENT_READY_TIMEOUT_S)

            rec = {
                "project": project,
                "mcp_port": mcp_port,
                "webharbor_port": webharbor_port,
                "worldctl_port": worldctl_port,
                "worldctl_token": worldctl_token,
                "started_at": time.time(),
                "agents": (existing or {}).get("agents", {}) if existing else {},
            }
            with _alloc_lock:
                _runtimes[world_id] = rec
            log.info("env up world=%s mcp=%s webharbor=%s worldctl=%s",
                     world_id, mcp_port, webharbor_port, worldctl_port)
            # Re-apply captured or first-class WebHarbor site DBs (cheap no-op otherwise).
            if enable_webharbor:
                # Overlay-replaced sites skip the byte-verify (the upstream app fails on their seed).
                _wp = _world_path(world_id)
                _skip = (set(_ws_overlay.list_overlay_webharbor_sites(_wp))
                         if _ws_overlay.has_overlay(_wp) else set())
                _restore_webharbor(world_id, project, skip_verify_sites=_skip)
            _apply_runtime_patch_for_world(world_id, project)
            # Apply the overlay's WebHarbor patches once the site DBs are loaded.
            if enable_webharbor:
                _apply_overlay_for_world(world_id)
            return rec
        finally:
            with _alloc_lock:
                _reserved_ports.difference_update(_my_reserved)

    if last_bind_error is not None:
        raise last_bind_error
    raise HTTPException(500, "env compose up failed after port reallocation")


def _webharbor_sites() -> list[str]:
    """Site ids from sites.json (single source of truth)."""
    try:
        cfg = load_jsonc(PROJECT_ROOT / "docker" / "webharbor" / "sites.json")
        return [s["id"] for s in cfg.get("sites", [])]
    except (OSError, ValueError, KeyError):
        return []


def _restore_webharbor(world_id: str, project: str, *,
                      skip_verify_sites: set[str] | None = None) -> None:
    """Push a world's WebHarbor DBs into the freshly booted container and restart each site."""
    skip = skip_verify_sites or set()
    inst = _world_path(world_id)
    seed = inst / "webharbor_seed.tar.gz"
    # A world instance carries cumulative webharbor/<dbfile> seeds; load and reset each site.
    webharbor_dbs = sorted((inst / "webharbor").glob("*.db")) \
        if (inst / "webharbor").is_dir() else []
    if not seed.exists() and not webharbor_dbs:
        return
    port = (_runtimes.get(world_id) or {}).get("webharbor_port")
    if not port:
        return
    container = f"wildawm-webharbor-{_safe_world_id(world_id)}"
    # Wait until the sites are alive, using the provisioning budget: booting all sites can be slow.
    if not _wc.wait_ready(container, timeout=RUNTIME_PROVISION_TIMEOUT_S):
        log.warning("webharbor restore: control plane not ready for %s after %ds "
                    "(needs >=12/15 sites alive)", world_id,
                    RUNTIME_PROVISION_TIMEOUT_S)
        return

    # Copy into instance_seed/ and instance/, reset and restart, then verify the live bytes match.

    def _hit(path: str, timeout: float = HTTP_SHORT_TIMEOUT_S) -> bool:
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}{path}", method="POST")
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:  # noqa: BLE001
            return False

    def _served_matches_seed(site: str, db: Path) -> bool:
        try:
            expected = hashlib.sha256(db.read_bytes()).hexdigest()
        except OSError:
            return False
        code = (
            "import hashlib,pathlib;"
            f"p=pathlib.Path('/opt/WebSyn/{site}/instance/{db.name}');"
            "print(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else '')"
        )
        r = subprocess.run(
            ["docker", "exec", container, "python3", "-c", code],
            capture_output=True, text=True, timeout=DOCKER_COMMAND_TIMEOUT_S)
        return r.returncode == 0 and r.stdout.strip() == expected

    if webharbor_dbs:
        try:
            site_by_db = {_wh.dbfile_for_site(s): s for s in _wh.all_sites()}
            loaded, failed = 0, []
            for db in webharbor_dbs:
                site = site_by_db.get(db.name)
                if not site:
                    continue
                ok = False
                for attempt in range(8):
                    cp_ok = True
                    cp_errors: list[str] = []
                    for dest in (f"instance_seed/{db.name}", f"instance/{db.name}"):
                        cp = subprocess.run(
                            ["docker", "cp", str(db),
                             f"{container}:/opt/WebSyn/{site}/{dest}"],
                            capture_output=True, timeout=DOCKER_COMMAND_TIMEOUT_S)
                        cp_ok = cp_ok and cp.returncode == 0
                        if cp.returncode != 0:
                            cp_errors.append(cp.stderr.decode("utf-8", "ignore"))
                    if cp_ok:
                        # /reset reloads instance/ from instance_seed/; /restart reopens the file.
                        reset_ok = _hit(f"/reset/{site}")
                        restart_ok = _hit(f"/restart/{site}")
                        if site in skip:
                            ok = True
                            break
                        if _served_matches_seed(site, db):
                            ok = True
                            break
                        log.info(
                            "webharbor restore verify retry world=%s site=%s "
                            "attempt=%s reset_ok=%s restart_ok=%s",
                            world_id, site, attempt + 1, reset_ok, restart_ok)
                    elif cp_errors:
                        log.warning(
                            "webharbor restore copy retry world=%s site=%s "
                            "attempt=%s errors=%s",
                            world_id, site, attempt + 1, cp_errors)
                    time.sleep(min(2.0 + attempt, 10.0))
                if ok:
                    loaded += 1
                else:
                    failed.append(site)
            if failed:
                log.error("webharbor restore: could NOT verify persona DB served "
                          "for %s sites=%s (world=%s) — cart/persona tasks on these "
                          "sites will fail", len(failed), failed, world_id)
            log.info("webharbor first-class seeds loaded+verified for %s (%d sites)",
                     world_id, loaded)
        except Exception as e:  # noqa: BLE001
            log.error("webharbor first-class load failed for %s: %s", world_id, e)

    if not seed.exists():
        return
    try:
        with open(seed, "rb") as fh:
            r = subprocess.run(
                ["docker", "exec", "-i", container, "sh", "-c",
                 "tar xzf - -C /opt/WebSyn"],
                stdin=fh, capture_output=True,
                timeout=DOCKER_COMMAND_TIMEOUT_S)
        if r.returncode != 0:
            log.warning("webharbor restore tar failed for %s: %s",
                        world_id, r.stderr.decode("utf-8", "ignore"))
            return
    except Exception as e:  # noqa: BLE001
        log.warning("webharbor restore exec failed for %s: %s", world_id, e)
        return
    # Restart each site without a DB reset to reopen the new file.
    restarted = 0
    for site in _webharbor_sites():
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/restart/{site}", method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_SHORT_TIMEOUT_S):
                restarted += 1
        except Exception:  # noqa: BLE001
            pass
    log.info("webharbor restored for %s (%d sites)", world_id, restarted)


def _webharbor_domain_aliases() -> list[str]:
    sites_json = PROJECT_ROOT / "docker" / "webharbor" / "sites.json"
    domains: list[str] = []
    for s in load_jsonc(sites_json).get("sites", []):
        domains.extend(s.get("domains", []))
    return sorted(set(domains))


def _container_network_aliases(
    container: str,
    network: str,
    process_env: Mapping[str, str] | None = None,
) -> set[str]:
    try:
        r = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_FAST_TIMEOUT_S,
            env=process_env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return set()
    if r.returncode != 0:
        return set()
    try:
        data = json.loads(r.stdout)
        net = ((data[0] or {}).get("NetworkSettings") or {}).get("Networks") or {}
        info = net.get(network) or {}
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
        return set()
    aliases = set(info.get("Aliases") or [])
    aliases.update(info.get("DNSNames") or [])
    return aliases


def _missing_network_aliases(
    container: str,
    network: str,
    required: list[str],
    process_env: Mapping[str, str] | None = None,
) -> list[str]:
    aliases = _container_network_aliases(container, network, process_env)
    return [d for d in required if d not in aliases]


def _connect_webharbor_network(
    world_id: str,
    project: str,
    process_env: Mapping[str, str] | None = None,
) -> None:
    wh_container = f"wildawm-webharbor-{_safe_world_id(world_id)}"
    wh_network = f"{project}_wildawm"
    domains = _webharbor_domain_aliases()
    if not domains:
        print(f"[env_start] {world_id}: webharbor network has no configured aliases")
        return

    alias_args = [a for d in domains for a in ("--alias", d)]
    attempts = max(1, WEBHARBOR_NETWORK_CONNECT_RETRIES)
    last_detail = ""
    missing: list[str] = []
    for attempt in range(1, attempts + 1):
        missing = _missing_network_aliases(
            wh_container, wh_network, domains, process_env)
        if not missing:
            print(f"[env_start] {world_id}: webharbor network aliases verified "
                  f"({len(domains)} aliases)")
            return

        try:
            r = subprocess.run(
                ["docker", "network", "connect"] + alias_args
                + [wh_network, wh_container],
                capture_output=True,
                text=True,
                timeout=WEBHARBOR_NETWORK_CONNECT_TIMEOUT_S,
                env=process_env,
            )
            stderr = (r.stderr or "")[:500]
            stdout = (r.stdout or "")[:500]
            if r.returncode != 0 and "already" not in stderr.lower():
                last_detail = stderr or stdout or f"docker network connect exited {r.returncode}"
            else:
                last_detail = stderr or stdout
        except subprocess.TimeoutExpired as e:
            last_detail = f"docker network connect timed out after {e.timeout}s"
        except (FileNotFoundError, OSError) as e:
            last_detail = f"{type(e).__name__}: {e}"

        missing = _missing_network_aliases(
            wh_container, wh_network, domains, process_env)
        if not missing:
            if last_detail:
                log.warning(
                    "webharbor network connect recovered world=%s attempt=%s detail=%s",
                    world_id, attempt, last_detail,
                )
            print(f"[env_start] {world_id}: webharbor network aliases verified "
                  f"({len(domains)} aliases)")
            return

        log.warning(
            "webharbor network aliases missing world=%s attempt=%s/%s missing=%s detail=%s",
            world_id, attempt, attempts,
            missing[:5], last_detail,
        )
        if attempt < attempts:
            time.sleep(min(2.0 * attempt, 10.0))

    raise HTTPException(
        504,
        "webharbor network aliases not ready for "
        f"{world_id}: missing={missing[:10]} detail={last_detail}",
    )


def _attach_agent(world_id: str, kind: str, model: str | None = None,
                  reasoning_effort: str | None = None,
                  timeout_s: int | None = None,
                  context_window: int | None = None,
                  max_output_tokens: int | None = None) -> dict:
    """Attach one agent harness to a running env on a model; a model switch recreates the container."""
    info = _runtimes.get(world_id)
    if not info:
        raise HTTPException(409, "World environment is not started — start the world first")
    try:
        backend = _agents.get(kind)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(400, str(e))
    try:
        requested_timeout_s = (
            DEFAULT_AGENT_TURN_TIMEOUT_S
            if timeout_s is None else int(timeout_s)
        )
    except (TypeError, ValueError):
        raise HTTPException(400, "timeout_s must be an integer")
    if requested_timeout_s < 0:
        raise HTTPException(400, "timeout_s must be >= 0")
    effective_reasoning_effort = (
        reasoning_effort if reasoning_effort is not None
        else runtime_cfg.DEFAULT_REASONING_EFFORT
    )
    try:
        spec = _models.resolve(model)
    except _models.ModelError as e:
        raise HTTPException(400, f"model error: {e}")
    configured_context_window = (
        context_window if context_window is not None
        else spec.get("context_window")
    )
    if configured_context_window is not None:
        try:
            configured_context_window = int(configured_context_window)
        except (TypeError, ValueError):
            raise HTTPException(400, "context_window must be an integer")
        if configured_context_window < 4096:
            raise HTTPException(400, "context_window must be >= 4096")
        spec["context_window"] = configured_context_window
    configured_max_output_tokens = (
        max_output_tokens if max_output_tokens is not None
        else spec.get("max_output_tokens")
    )
    if configured_max_output_tokens is not None:
        try:
            configured_max_output_tokens = int(configured_max_output_tokens)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_output_tokens must be an integer")
        if configured_max_output_tokens <= 0:
            raise HTTPException(400, "max_output_tokens must be positive")
        if (configured_context_window is not None
                and configured_max_output_tokens > configured_context_window):
            raise HTTPException(
                400, "max_output_tokens must not exceed context_window")
        spec["max_output_tokens"] = configured_max_output_tokens
    try:
        reasoning_policy = runtime_cfg.reasoning_effort_policy(
            spec, effective_reasoning_effort, harness=kind)
    except runtime_cfg.ReasoningEffortError as e:
        raise HTTPException(400, str(e))
    spec = dict(spec)
    if reasoning_policy:
        spec["reasoning_effort"] = reasoning_policy["effort"]
        spec["reasoning_policy"] = reasoning_policy

    project = info["project"]
    agents = info.setdefault("agents", {})

    # Already running on the requested model? (checked against live docker state)
    running_status = _container_status(project)
    running = running_status.get(backend.service_name()) == "running"
    running_rec = agents.get(kind) or {}
    requested_effort = (reasoning_policy or {}).get("effort") or ""
    if (running and running_rec.get("model") == spec["ref"]
            and (running_rec.get("reasoning_effort") or "") == requested_effort
            and running_rec.get("timeout_s") == requested_timeout_s
            and running_rec.get("context_window") == configured_context_window
            and running_rec.get("max_output_tokens") == configured_max_output_tokens):
        return agents[kind]

    state_dir = NANOBOT_STATE_DIR / world_id

    # Ship the shell MCP helper: subprocess agents cannot reach the MCP gateway with plain curl.
    try:
        cfg_dir = state_dir / "agent-config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        helper_src = PROJECT_ROOT / "docker" / "mcp_call.py"
        if helper_src.exists():
            shutil.copyfile(helper_src, cfg_dir / "mcp_call.py")
        vision_src = DESCRIBE_IMAGE_MCP
        vision_dst = cfg_dir / "describe_image_mcp.py"
        # Make the vision MCP helper available to every model.
        if vision_src.exists():
            shutil.copyfile(vision_src, vision_dst)
    except OSError:
        pass

    try:
        backend.prepare(world_id, PROJECT_ROOT, state_dir, spec)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(500, f"backend prepare failed: {e}")

    env = _base_compose_env(world_id, requested_timeout_s)
    # Pass the env ports so compose does not recreate mcp/webharbor via `depends_on`.
    env.update(_runtime_port_env(info))
    # The compose file maps AWM_OPENAI_* onto the container's OPENAI_* variables as well.
    env["AWM_OPENAI_BASE_URL"] = spec["base_url"]
    env["AWM_OPENAI_API_KEY"] = spec["api_key"]
    env["AWM_OPENAI_MODEL"] = spec["api_model"]
    env["AWM_CHAT_TOKEN_LIMIT_PARAM"] = runtime_cfg.chat_token_limit_param(spec)
    env["AWM_MODEL_TEXT_ONLY"] = "1" if runtime_cfg.model_needs_vision_shim(spec["ref"]) else "0"
    env["AWM_MODEL_CONTEXT_WINDOW"] = (
        str(configured_context_window) if configured_context_window is not None else "")
    env.update(runtime_cfg.reasoning_effort_env(reasoning_policy))
    env.update(runtime_cfg.claude_code_model_env(
        spec["api_model"], configured_context_window))
    try:
        vision_spec = runtime_cfg.opencode_vision_spec()
        env["AWM_VISION_BASE_URL"] = runtime_cfg.container_host_url(
            vision_spec["base_url"])
        env["AWM_VISION_API_KEY"] = vision_spec.get("api_key", "")
        env["AWM_VISION_MODEL"] = vision_spec["api_model"]
        env.update(runtime_cfg.vision_reasoning_env())
    except Exception:  # noqa: BLE001
        env["AWM_VISION_BASE_URL"] = runtime_cfg.container_host_url(
            runtime_cfg.VISION_MCP_BASE_URL)
        env["AWM_VISION_API_KEY"] = runtime_cfg.VISION_MCP_API_KEY
        env["AWM_VISION_MODEL"] = runtime_cfg.VISION_MCP_MODEL

    # Reserve the agent's host port under the lock so parallel attaches never collide.
    agent_port: int | None = None
    if backend.publishes_port():
        with _alloc_lock:
            docker_used = _docker_published_ports()
            used = {a.get("agent_port") for r in list(_runtimes.values())
                    for a in r.get("agents", {}).values() if a.get("agent_port")} \
                | docker_used | _reserved_ports
            agent_port = _allocate_port(backend.host_port_base(), used)
            _reserved_ports.add(agent_port)
        env[backend.PORT_ENV] = str(agent_port)
    _my_agent_reserved = {agent_port} if agent_port else set()

    try:
        svc = backend.service_name()
        subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "--profile", kind,
             "-p", project, "rm", "-sf", svc],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            timeout=RUNTIME_SHORT_TIMEOUT_S,
        )
        up_cmd = _agent_compose_up_cmd(kind, project, svc)
        print(f"[agent_attach] {world_id}/{kind}: {' '.join(up_cmd[-3:])}")
        try:
            subprocess.run(
                up_cmd, cwd=str(PROJECT_ROOT), env=env,
                check=True, capture_output=True,
                timeout=RUNTIME_SHORT_TIMEOUT_S,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"agent compose up failed: {e.stderr.decode()}")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "agent compose up timed out")

        # Wait for the in-container adapter, reached through worldctl, to report ready.
        if not _wait_agent_ready(world_id, kind, timeout_s=AGENT_READY_TIMEOUT_S):
            raise HTTPException(504,
                f"agent {kind} adapter did not become ready in "
                f"{AGENT_READY_TIMEOUT_S}s (see worldctl logs)")

        rec = {"agent_port": agent_port, "started_at": time.time(),
               "model": spec["ref"],
               "reasoning_effort": requested_effort or None,
               "timeout_s": requested_timeout_s,
               "context_window": configured_context_window,
               "max_output_tokens": configured_max_output_tokens}
        agents[kind] = rec
        log.info("agent attached world=%s kind=%s model=%s reasoning_effort=%s",
                 world_id, kind, spec["ref"], requested_effort or "")
        return rec
    finally:
        with _alloc_lock:
            _reserved_ports.difference_update(_my_agent_reserved)


@app.post("/api/worlds/{world_id}/runtime/start")
async def runtime_start(world_id: str, request: Request):
    """Start the world environment (mcp, webharbor); optionally attach one agent via `kind`."""
    body = await request.json() if (await request.body()) else {}
    enable_webharbor = body.get("enable_webharbor")
    enable_webharbor = True if enable_webharbor is None else bool(enable_webharbor)

    await asyncio.to_thread(_ensure_env, world_id, enable_webharbor)

    kind = body.get("kind")
    if kind:
        await asyncio.to_thread(
            _attach_agent,
            world_id,
            kind,
            body.get("model"),
            body.get("reasoning_effort"),
            body.get("timeout_s"),
            context_window=body.get("context_window"),
            max_output_tokens=body.get("max_output_tokens"),
        )

    return await asyncio.to_thread(_runtime_info, world_id)


@app.post("/api/worlds/{world_id}/agents/{kind}/start")
async def agent_start(world_id: str, kind: str, request: Request):
    """Attach an agent harness on a chosen model, starting the environment if needed."""
    body = await request.json() if (await request.body()) else {}
    if world_id not in _runtimes or \
            (await asyncio.to_thread(_container_status,
                                     _project_name(world_id))).get("mcp") != "running":
        enable_webharbor = body.get("enable_webharbor")
        enable_webharbor = True if enable_webharbor is None else bool(enable_webharbor)
        await asyncio.to_thread(_ensure_env, world_id, enable_webharbor)
    await asyncio.to_thread(
        _attach_agent,
        world_id,
        kind,
        body.get("model"),
        body.get("reasoning_effort"),
        body.get("timeout_s"),
        context_window=body.get("context_window"),
        max_output_tokens=body.get("max_output_tokens"),
    )
    return await asyncio.to_thread(_runtime_info, world_id)


@app.post("/api/worlds/{world_id}/agents/{kind}/stop")
async def agent_stop(world_id: str, kind: str, request: Request):
    """Detach one agent harness, leaving the world and other agents running."""
    try:
        backend = _agents.get(kind)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(400, str(e))
    project = _project_name(world_id)
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-f", COMPOSE_FILE, "--profile", kind,
             "-p", project, "rm", "-sf", backend.service_name()],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True,
            timeout=RUNTIME_SHORT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"agent stop failed: {e.stderr.decode()}")
    info = _runtimes.get(world_id)
    if info:
        info.get("agents", {}).pop(kind, None)
    return await asyncio.to_thread(_runtime_info, world_id)


_COMPLETION_MARKER_PATH_RE = re.compile(
    r"/tmp/wildawm_completion_[0-9a-f]{32}\.md")
_COMPLETION_MARKER_COMMAND_ATTEMPTS = 3


def _run_completion_marker_command(command: list[str]) -> subprocess.CompletedProcess:
    for attempt in range(1, _COMPLETION_MARKER_COMMAND_ATTEMPTS + 1):
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_FAST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as error:
            if attempt == _COMPLETION_MARKER_COMMAND_ATTEMPTS:
                raise HTTPException(
                    504,
                    "completion marker command timed out after "
                    f"{_COMPLETION_MARKER_COMMAND_ATTEMPTS} attempts",
                ) from error
        except (FileNotFoundError, OSError) as error:
            raise HTTPException(
                502, f"completion marker command failed: {error}") from error

    raise AssertionError("unreachable")


def _agent_completion_marker(world_id: str, kind: str, marker_path: str,
                             action: str) -> dict:
    """Read or remove a completion marker in this worker's agent container."""
    _world_path(world_id)
    if not isinstance(marker_path, str) or not _COMPLETION_MARKER_PATH_RE.fullmatch(
            marker_path):
        raise HTTPException(400, "invalid completion marker path")
    if action not in {"read", "remove"}:
        raise HTTPException(400, "completion marker action must be read or remove")
    try:
        backend = _agents.get(kind)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(400, str(e))
    container = backend.container_name(world_id)

    if action == "remove":
        result = _run_completion_marker_command(
            ["docker", "exec", container, "rm", "-f", "--", marker_path])
        if result.returncode != 0:
            raise HTTPException(
                502, f"completion marker remove failed: {(result.stderr or '').strip()}")
        return {"ok": True}

    exists = _run_completion_marker_command(
        ["docker", "exec", container, "test", "-f", marker_path])
    if exists.returncode != 0:
        detail = (exists.stderr or "").strip()
        if detail:
            raise HTTPException(502, f"completion marker probe failed: {detail}")
        return {"exists": False, "content": ""}
    read = _run_completion_marker_command(
        ["docker", "exec", container, "cat", marker_path])
    if read.returncode != 0:
        raise HTTPException(
            502, f"completion marker read failed: {(read.stderr or '').strip()}")
    return {"exists": True, "content": read.stdout or ""}


@app.post("/api/worlds/{world_id}/agents/{kind}/completion-marker")
async def agent_completion_marker(world_id: str, kind: str, request: Request):
    body = await request.json()
    return await asyncio.to_thread(
        _agent_completion_marker,
        world_id,
        kind,
        body.get("path"),
        body.get("action") or "read",
    )


@app.post("/api/worlds/{world_id}/runtime/stop")
async def runtime_stop(world_id: str, request: Request):
    """Stop the world environment and every attached agent."""
    info = _runtimes.get(world_id)
    project = info["project"] if info else _project_name(world_id)
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-f", COMPOSE_FILE, *_ALL_PROFILE_ARGS,
             "-p", project, "down", "--remove-orphans"],
            cwd=str(PROJECT_ROOT),
            check=True, capture_output=True, timeout=RUNTIME_SHORT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"docker compose down failed: {e.stderr.decode()}")
    await asyncio.to_thread(_cleanup_empty_project_network, project)
    with _alloc_lock:
        _runtimes.pop(world_id, None)
    return {"running": False, "project": project}


@app.post("/api/worlds/{world_id}/runtime/reset")
async def runtime_reset(world_id: str, request: Request):
    """Destroy and recreate the world under the same id, optionally from another profile/day or archive."""
    body = await request.json() if (await request.body()) else {}
    meta = _read_instance_meta(world_id)
    if not meta:
        raise HTTPException(404, f"instance {world_id!r} has no meta.json")

    reset_path = (body.get("path") or "").strip()
    reset_state = (body.get("state") or "after").strip()
    profile = body.get("profile") or meta.get("profile")
    day = body.get("day")
    day = int(day) if day is not None else meta.get("started_at_day")
    reset_patch = body.get("world_patch") or (meta.get("world_patch") or {}).get("path")
    if not reset_path and (profile is None or day is None):
        raise HTTPException(500,
            "instance meta missing profile/started_at_day; can't reset.")

    info = _runtimes.get(world_id)
    was_running = info is not None
    # Remember attached agents, their models and webharbor, to restore the same topology.
    prev_agents = {k: {
                       "model": (a or {}).get("model"),
                       "reasoning_effort": (a or {}).get("reasoning_effort"),
                   }
                   for k, a in (info or {}).get("agents", {}).items()} if info else {}
    prev_webharbor = bool((info or {}).get("webharbor_port")) if info else True

    await asyncio.to_thread(_destroy_instance, world_id)

    # Recreate under the same id: from `path`, a new profile/day, or the original state.
    if reset_path:
        new_id = await asyncio.to_thread(
            _restore_instance, reset_path, world_id, reset_state)
    else:
        new_id = await asyncio.to_thread(_create_instance, profile, day, world_id,
                                         reset_patch)

    restarted = False
    if was_running:
        try:
            await asyncio.to_thread(_ensure_env, world_id, prev_webharbor)
            for kind, prev in prev_agents.items():
                try:
                    await asyncio.to_thread(_attach_agent, world_id, kind,
                                            prev.get("model"),
                                            prev.get("reasoning_effort"))
                except HTTPException:
                    pass
            restarted = True
        except HTTPException:
            pass

    return {
        "success": True,
        "id": new_id,
        "was_running": was_running,
        "restarted": restarted,
    }


# WebHarbor control-plane proxies for the dashboard; agents reach the sites via DNS aliases.

def _webharbor_port(world_id: str) -> int:
    info = _runtimes.get(world_id)
    if not info or not info.get("webharbor_port"):
        raise HTTPException(404,
            f"world {world_id!r} has no webharbor running — start it with "
            f"enable_webharbor=true (the default)")
    return info["webharbor_port"]


@app.get("/api/worlds/{world_id}/webharbor/health")
async def webharbor_health(world_id: str):
    """Per-site PID and alive status."""
    port = _webharbor_port(world_id)
    try:
        with urllib.request.urlopen(
                f"http://localhost:{port}/health",
                timeout=HTTP_PROBE_TIMEOUT_S) as resp:
            return JSONResponse(json.loads(resp.read().decode("utf-8", "ignore")))
    except Exception as e:
        raise HTTPException(502, f"webharbor health probe failed: {e}")


@app.post("/api/worlds/{world_id}/webharbor/reset")
async def webharbor_reset(world_id: str, request: Request):
    """Reset every WebHarbor site's instance/ from instance_seed/."""
    port = _webharbor_port(world_id)
    body = await request.json() if (await request.body()) else {}
    site = body.get("site")
    url = f"http://localhost:{port}/reset/{site}" if site else f"http://localhost:{port}/reset-all"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_SHORT_TIMEOUT_S) as resp:
            return JSONResponse(json.loads(resp.read().decode("utf-8", "ignore")))
    except Exception as e:
        raise HTTPException(502, f"webharbor reset failed: {e}")


def _default_session_id(world_id: str, kind: str) -> str:
    """Per-(world, kind) default session, so concurrent agents keep separate conversations."""
    return f"{world_id}:{kind}"


@app.post("/api/worlds/{world_id}/agent/chat")
async def agent_chat(world_id: str, request: Request):
    """Send a chat turn to one agent running in this world."""
    info = _runtimes.get(world_id)
    if not info:
        raise HTTPException(409, "World environment is not started")

    payload = await request.json()
    message = payload.get("message", "")
    kind = payload.get("kind") or _agents.DEFAULT_KIND
    try:
        backend = _agents.get(kind)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(400, str(e))
    session_id = payload.get("session_id") or _default_session_id(world_id, kind)
    model = payload.get("model")
    # Per-turn budgets are enforced by the in-container adapter; just pass them through.
    timeout_s = payload.get("timeout_s")
    max_turns = payload.get("max_turns")
    reasoning_effort = payload.get("reasoning_effort")
    context_window = payload.get("context_window")

    # Lazily attach; _attach_agent no-ops on the same model and re-attaches on a different one.
    rec = info.get("agents", {}).get(kind)
    running = bool(rec) and \
        (await asyncio.to_thread(_container_status, info["project"])).get(
            backend.service_name()) == "running"
    current_effort = (rec or {}).get("reasoning_effort") if rec else None
    current_timeout_s = (rec or {}).get("timeout_s") if rec else None
    current_context_window = (rec or {}).get("context_window") if rec else None
    if (not running or model
            or (reasoning_effort is not None
                and (current_effort or "") != (str(reasoning_effort or "").strip()))
            or (timeout_s is not None
                and str(current_timeout_s) != str(timeout_s))
            or (context_window is not None
                and str(current_context_window) != str(context_window))):
        attach_model = model if model is not None else (rec or {}).get("model")
        attach_context_window = (
            context_window if context_window is not None
            else current_context_window
        )
        await asyncio.to_thread(
            _attach_agent, world_id, kind, attach_model,
            reasoning_effort, timeout_s, attach_context_window)

    # The chat turn goes through worldctl to the agent's in-container adapter.
    wc_port, wc_token = _worldctl_for(world_id)
    log.info("chat world=%s kind=%s session=%s msg_len=%d",
             world_id, kind, session_id, len(message))
    try:
        result = await _worldctl.chat(wc_port, wc_token, kind, session_id, message,
                                      timeout_s=timeout_s, max_turns=max_turns,
                                      reasoning_effort=reasoning_effort)
    except _worldctl.WorldctlError as e:
        log.warning("chat failed world=%s kind=%s: %s", world_id, kind, e)
        raise HTTPException(502, str(e))
    return {
        "content": result.get("content", ""),
        "raw": result.get("raw"),
        "session_id": result.get("session_id", session_id),
        "kind": kind,
        "stop_reason": result.get("stop_reason"),
        "budget_started": result.get("budget_started"),
        "budget_remaining_s": result.get("budget_remaining_s"),
    }


@app.get("/api/worlds/{world_id}/agent/history")
async def agent_history(world_id: str, request: Request, session_id: str | None = None,
                        kind: str | None = None):
    """Return a structured transcript for one agent's session."""
    _world_path(world_id)
    info = _runtimes.get(world_id) or {}
    if not kind:
        kind = next(iter(info.get("agents", {})), None) or _agents.DEFAULT_KIND
    try:
        backend = _agents.get(kind)
    except _agents.BackendRuntimeError as e:
        raise HTTPException(400, str(e))
    sid = session_id or _default_session_id(world_id, kind)
    wc = _runtimes.get(world_id) or {}
    wc_port = wc.get("worldctl_port")
    history = []
    if wc_port:
        try:
            history = await _worldctl.history(wc_port, wc.get("worldctl_token"), kind, sid)
        except _worldctl.WorldctlError:
            history = []
    return {"history": history, "session_id": sid, "kind": kind}


def _world_current_day(world_id: str) -> dict:
    """Return the agent's frozen 'today': the morning of day N+1 for an instance created at day N."""
    meta = _read_instance_meta(world_id)
    started_day = meta.get("started_at_day")
    started_date = meta.get("started_at_date")
    frozen_date = meta.get("frozen_clock_date")
    frozen_time = meta.get("frozen_clock_time", "10:00:00")
    if not frozen_date and started_date:
        try:
            frozen_date = (_date.fromisoformat(started_date) + _td(days=1)).isoformat()
        except (TypeError, ValueError):
            frozen_date = None
    current_day = (started_day + 1) if started_day is not None else None
    return {
        "current_date": frozen_date,
        "current_time": frozen_time,
        "current_day": current_day,
        "started_at_day": started_day,
        "started_at_date": started_date,
        "source": "meta" if frozen_date else "missing",
    }


@app.get("/api/worlds/{world_id}/today")
async def world_today(world_id: str):
    """Return the world's frozen current-date and the day-N it maps to."""
    _world_path(world_id)
    return _world_current_day(world_id)


def _episodes_dir(world_id: str) -> Path:
    return NANOBOT_STATE_DIR / world_id / "episodes"


def _snapshot_webharbor(world_id: str, dest: Path) -> dict:
    """Capture the WebHarbor in-container site DBs into webharbor.tar.gz (best-effort)."""
    container = f"wildawm-webharbor-{world_id}"
    info = {"captured": False}
    try:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{container}$",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=SUBPROCESS_FAST_TIMEOUT_S)
        present = container in (ps.stdout or "").split()
    except Exception as e:  # noqa: BLE001
        present = False
        info["error"] = f"docker ps failed: {e}"
    if not present:
        (dest / "webharbor.absent").write_text(
            "no webharbor container for this world at snapshot time\n")
        return info
    out_path = dest / "webharbor.tar.gz"
    try:
        with open(out_path, "wb") as fh:
            proc = subprocess.run(
                ["docker", "exec", container, "sh", "-c",
                 "cd /opt/WebSyn && tar czf - */instance 2>/dev/null"],
                stdout=fh, stderr=subprocess.PIPE, timeout=DOCKER_COMMAND_TIMEOUT_S)
        if proc.returncode == 0 and out_path.stat().st_size > 0:
            info["captured"] = True
            info["bytes"] = out_path.stat().st_size
        else:
            info["error"] = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
            if out_path.exists() and out_path.stat().st_size == 0:
                out_path.unlink()
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
        if out_path.exists():
            out_path.unlink(missing_ok=True)
    return info


def _snapshot_world(world_id: str, label: str, episode_id: str,
                    include_webharbor: bool = True) -> Path:
    """Snapshot the restorable state: data, workspace (with .git), WebHarbor DBs and meta."""
    src = _world_path(world_id)
    dest = _episodes_dir(world_id) / episode_id / label
    if dest.exists():
        # Force-remove leftovers, including read-only git objects.
        def _force_remove(func, path, exc_info):
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        shutil.rmtree(dest, onerror=_force_remove)
    dest.mkdir(parents=True, exist_ok=True)

    src_data = src / "data"
    if src_data.is_dir():
        shutil.copytree(src_data, dest / "data", symlinks=True)

    src_ws = src / "workspace"
    if src_ws.is_dir():
        # Keep `.git` so the scene restores faithfully.
        shutil.copytree(src_ws, dest / "workspace", symlinks=True)

    src_meta = src / "meta.json"
    if src_meta.is_file():
        shutil.copyfile(src_meta, dest / "meta.json")

    if include_webharbor:
        wh = _snapshot_webharbor(world_id, dest)
        (dest / "webharbor.meta.json").write_text(json.dumps(wh, indent=2))
    return dest


@app.post("/api/worlds/{world_id}/episodes/start")
async def generic_episode_start(world_id: str, request: Request):
    """Start an episode (snapshot 'before') not tied to a task."""
    _world_path(world_id)
    body = await request.json() if (await request.body()) else {}
    label = _safe_component(body.get("label") or "run", "run")
    episode_id = f"{label}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    await asyncio.to_thread(_snapshot_world, world_id, "before", episode_id)
    return {"episode_id": episode_id}


@app.get("/api/worlds/{world_id}/episodes/active")
async def get_active_episode(world_id: str):
    """Return the latest episode with a before/ snapshot and no ended.flag."""
    _world_path(world_id)
    eps = _episodes_dir(world_id)
    if not eps.is_dir():
        return {"active": None}
    best = None
    for d in eps.iterdir():
        if not d.is_dir():
            continue
        if not (d / "before").is_dir():
            continue
        if (d / "ended.flag").exists():
            continue
        mtime = d.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, d.name)
    if best is None:
        return {"active": None}
    episode_id = best[1]
    # Episode ids are `{event}_{unix_ts}_{hex6}`; event names may contain underscores.
    parts = episode_id.rsplit("_", 2)
    event = parts[0] if len(parts) == 3 else episode_id
    return {"active": {"episode_id": episode_id, "event": event}}


@app.post("/api/worlds/{world_id}/episodes/{episode_id}/end")
async def end_episode(world_id: str, episode_id: str, request: Request):
    """Mark an episode as ended without verifying it."""
    _world_path(world_id)
    ep_root = _episodes_dir(world_id) / episode_id
    if not ep_root.is_dir():
        raise HTTPException(404, f"episode {episode_id} not found")
    (ep_root / "ended.flag").touch()
    return {"ok": True, "episode_id": episode_id}


def _runtime_agent_stats(world_id: str, kind: str | None, session_id: str,
                         seconds: float | int | None = None) -> dict:
    """Best-effort live solver stats for a just-run agent session."""
    kind = kind or _agents.DEFAULT_KIND
    history: list[dict] = []
    info = _runtimes.get(world_id) or {}
    wc_port, wc_token = info.get("worldctl_port"), info.get("worldctl_token")
    if wc_port:
        try:
            history = _worldctl_sync_history(wc_port, wc_token, kind, session_id)
        except Exception:  # noqa: BLE001
            history = []
    proxy_trajectory = None
    try:
        cname = f"wildawm-agent-{kind}-{world_id}"
        pj = subprocess.run(
            ["docker", "exec", cname, "curl", "-s",
             f"http://localhost:{LLM_PROXY_PORT}/trajectory"],
            capture_output=True, text=True, timeout=HTTP_SHORT_TIMEOUT_S)
        if pj.returncode == 0 and pj.stdout.strip().startswith("{"):
            proxy_trajectory = json.loads(pj.stdout)
    except Exception:  # noqa: BLE001
        proxy_trajectory = None
    return agent_stats_from_history_proxy(history, proxy_trajectory,
                                          seconds=seconds)


def _read_agent_last_reply(world_id: str, session_id: str,
                           kind: str | None = None) -> str:
    """Read the last reply from an agent kind (default: first running agent)."""
    info = _runtimes.get(world_id) or {}
    if not kind:
        kind = next(iter(info.get("agents", {})), None) or _agents.DEFAULT_KIND
    wc_port = info.get("worldctl_port")
    if not wc_port:
        return ""
    try:
        return _worldctl.reply(wc_port, info.get("worldctl_token"), kind, session_id)
    except _worldctl.WorldctlError:
        return ""


# XAI tasks live in tasks_xai/<profile>/dayN/<slug>/ and span Web, CLI and MCP interfaces.


def _scan_tasks_xai() -> dict:
    """Discover accepted tasks under tasks_xai/ as registry entries keyed by task name."""
    out: dict = {}
    if not TASKS_XAI_DIR.exists():
        return out
    for report in sorted(TASKS_XAI_DIR.glob("*/day*/*/_synthesis_report.json")):
        try:
            if load_jsonc(report).get("verdict") not in _xai_index.ACCEPTED:
                continue
        except (ValueError, OSError):
            continue
        d = report.parent
        try:
            meta = load_jsonc(d / "meta.json")
        except (ValueError, OSError):
            meta = {}
        profile = d.parts[-3]
        try:
            day = int(d.parts[-2].replace("day", ""))
        except ValueError:
            continue
        name = meta.get("slug") or d.name
        if name in out:
            name = f"{d.parts[-2]}-{name}"
        ifaces = [s.split(":", 1)[-1] for s in meta.get("interfaces", [])]
        out[name] = {
            "title": meta.get("title", name),
            "description": meta.get("title", ""),
            "interfaces": ifaces,
            "interface_flow": meta.get("interface_flow", ""),
            "difficulty": meta.get("difficulty", "unknown"),
            "eligible": [{"profile": profile, "days": [day]}],
            "files": {"task": str(d / "task.md"), "verifier": str(d / "verifier"),
                      "golden": str(d / "golden.py"),
                      "world_patch": str(d / _xai_world_patch.PATCH_DIRNAME)
                      if (d / _xai_world_patch.PATCH_DIRNAME).is_dir() else None},
            "has_world_patch": _xai_world_patch.has_patch(d),
            "_dir": str(d),
        }
    return out


def _task_has_assets(name: str, info: dict) -> bool:
    """Asset check: task.md, verifier/ and golden.py under the task dir."""
    d = Path(info["_dir"])
    return ((d / "task.md").exists() and (d / "verifier").is_dir()
            and (d / "golden.py").exists())


def _load_xai_registry() -> dict:
    """Discover all accepted 3-layer XAI tasks under tasks_xai/."""
    return _scan_tasks_xai()


@app.get("/api/xai-synthesis-pool")
async def xai_synthesis_pool():
    """List compact accepted tasks plus live synthesis locks for this workspace."""
    return await asyncio.to_thread(lambda: _xai_pool.list_pool(purge_expired=False))


@app.post("/api/xai-synthesis-pool/reserve-many")
async def xai_synthesis_pool_reserve_many(request: Request):
    """Atomically reserve up to k globally-diverse XAI sketches."""
    body = await request.json() if (await request.body()) else {}
    try:
        return await asyncio.to_thread(
            _xai_pool.reserve_many,
            body.get("candidates") or [],
            int(body.get("k") or 0),
            owner=body.get("owner") or "unknown",
            profile=body.get("profile") or "",
            day=int(body.get("day") or 0),
            lease_seconds=int(body.get("lease_seconds") or _xai_pool.DEFAULT_LEASE_SECONDS),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"xai synthesis reserve-many failed: {e}")


@app.post("/api/xai-synthesis-pool/{lock_id}/heartbeat")
async def xai_synthesis_pool_heartbeat(lock_id: str, request: Request):
    body = await request.json() if (await request.body()) else {}
    result = await asyncio.to_thread(
        _xai_pool.heartbeat,
        lock_id,
        owner=body.get("owner"),
        lease_seconds=int(body.get("lease_seconds") or _xai_pool.DEFAULT_LEASE_SECONDS),
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("reason") or "heartbeat failed")
    return result


@app.post("/api/xai-synthesis-pool/{lock_id}/finalize")
async def xai_synthesis_pool_finalize(lock_id: str, request: Request):
    body = await request.json() if (await request.body()) else {}
    result = await asyncio.to_thread(
        _xai_pool.finalize,
        lock_id,
        owner=body.get("owner"),
        verdict=body.get("verdict") or "",
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("reason") or "finalize failed")
    return result


@app.delete("/api/xai-synthesis-pool/{lock_id}")
async def xai_synthesis_pool_release(lock_id: str, request: Request):
    body = await request.json() if (await request.body()) else {}
    result = await asyncio.to_thread(
        _xai_pool.release,
        lock_id,
        owner=body.get("owner"),
        reason=body.get("reason") or "",
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("reason") or "release failed")
    return result


@app.get("/api/xai-synthesis-skills")
async def xai_synthesis_skills():
    """List workspace-local evolving task-synthesis skills."""
    docs = await asyncio.to_thread(_xai_task_skills.list_docs)
    return {"root": str(_xai_task_skills.root()), "count": len(docs), "docs": docs}


@app.post("/api/xai-synthesis-skills/update")
async def xai_synthesis_skills_update(request: Request):
    """Apply a bounded task-skill update under the shared edit lock."""
    body = await request.json() if (await request.body()) else {}
    try:
        return await asyncio.to_thread(
            _xai_task_skills.apply_updates,
            body.get("updates") or [],
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"xai synthesis skill update failed: {e}")


def _xai_task_dir(task_name: str, info: dict | None = None) -> Path:
    """Resolve a task's dir via `_dir`; unknown tasks map to a nonexistent path."""
    if info is None:
        info = _load_xai_registry().get(task_name) or {}
    d = info.get("_dir")
    return Path(d) if d else (TASKS_XAI_DIR / "__missing__" / task_name)


def _ensure_xai_task_patch(world_id: str, task_dir: Path) -> dict:
    """Apply a task-local world_patch before task start; a world cannot mix task patches."""
    if not _xai_world_patch.has_patch(task_dir):
        return {"ok": True, "skipped": True}
    patch_dir = task_dir / _xai_world_patch.PATCH_DIRNAME
    existing = _world_patch_dir_for_instance(world_id)
    if existing:
        if _same_patch(existing, patch_dir):
            return {"ok": True, "skipped": False, "already_applied": True,
                    "patch_dir": _patch_path_for_meta(patch_dir)}
        raise HTTPException(
            409,
            f"world {world_id!r} already has a different task-local world_patch applied; create a fresh world for this XAI task",
        )
    target = _world_path(world_id)
    meta = _read_instance_meta(world_id)
    _apply_patch_data_to_instance(target, meta, patch_dir)
    runtime = _runtimes.get(world_id)
    runtime_result = {"ok": True, "skipped": True}
    if runtime:
        if runtime.get("webharbor_port"):
            _restore_webharbor(world_id, runtime["project"])
        runtime_result = _apply_runtime_patch_for_world(world_id, runtime["project"])
    return {"ok": True, "skipped": False,
            "patch_dir": _patch_path_for_meta(patch_dir),
            "runtime": runtime_result}


def _xai_modules():
    """Lazy-import the shared task verifier and judge stack."""
    return (importlib.import_module("wildawm.synthesis.tasks.verifier"),
            importlib.import_module("wildawm.synthesis.tasks.llm_judge"),
            importlib.import_module("wildawm.synthesis.tasks.judge"),
            importlib.import_module("wildawm.const_config"))


def _xai_baseline_dir(world_id: str, task_dir: Path | None = None) -> Path | None:
    """Verifier baseline: the patched task world, else the day-N master copy, else None."""
    meta = _read_instance_meta(world_id)
    profile = meta.get("profile", "")
    day = meta.get("started_at_day")
    if profile and day is not None:
        if task_dir and _xai_world_patch.has_patch(task_dir):
            return _xai_world_patch.patched_baseline_dir(profile, int(day), task_dir)
        inst_patch = _world_patch_dir_for_instance(world_id)
        if inst_patch and inst_patch.name == _xai_world_patch.PATCH_DIRNAME:
            task_guess = inst_patch.parent
            if _xai_world_patch.has_patch(task_guess):
                return _xai_world_patch.patched_baseline_dir(profile, int(day), task_guess)
        # Use a scratch copy: the verifier and its gateway would otherwise mutate the master.
        snap = _ws_anchors.inspection_world(profile, int(day))
        if snap and snap.is_dir():
            return snap
    return None


def _xai_verifier_rd(task_dir: Path, world_id: str, wp: Path,
                     baseline_dir: Path) -> dict:
    """Run a task's Layer-1 verifier on the world's after-state and return the raw rule_result."""
    vf, _, _, _ = _xai_modules()
    verifier_dir = task_dir / "verifier"
    wh = None
    try:  # snapshot the served webharbor for web tasks
        prov = importlib.import_module("wildawm.synthesis.tasks.provision")
        whdest = _episodes_dir(world_id) / "_verify_webharbor"
        prov.snapshot_webharbor_served(world_id, whdest)
        if whdest.is_dir() and any(whdest.iterdir()):
            wh = whdest
    except Exception:  # noqa: BLE001
        wh = None
    bwh = baseline_dir / "webharbor"
    return vf.run_verifier(verifier_dir, wp / "workspace", wp / "data",
                           baseline_dir, wh,
                           baseline_webharbor_dir=bwh if bwh.is_dir() else None)


def _xai_evidence_text(rd: dict) -> str:
    """Human-readable Layer-1 dump for the verify UI (the judge gets a blind digest instead)."""
    lines = [f"LAYER-1 aggregate verdict: {rd.get('verdict')}  "
             f"({rd.get('n_scripts')} checks)"]
    for s in rd.get("scripts", []):
        lines.append(f"\n[{s['verdict']}] {s['name']}")
        lines.append((s.get("stdout") or "").strip()[-1500:])
    return "\n".join(lines)


def _xai_judge_lightweight(task_md: str, rubric_md: str, rule_result: dict,
                           judge_spec: dict, *,
                           provider_retries: int = 3,
                           max_output_tokens: int | None = None,
                           reasoning_effort: str | None = None) -> tuple[dict, dict]:
    """Lightweight judge (as in synthesis): blind evidence, 3-class verdict; returns (verdict, meta)."""
    vf, lj, _, _ = _xai_modules()
    ev = vf.build_lightweight_evidence(rule_result)
    t0 = time.time()
    r = lj.judge(
        task_md, rubric_md, ev, spec=judge_spec,
        fallback_verdict=rule_result.get("verdict"),
        provider_retries=provider_retries,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort)
    verdict = {"verdict": r.get("verdict"),
               "confidence": r.get("confidence"),
               "reasoning": r.get("justification", ""),
               "status": r.get("status")}
    meta = {"mode": "lightweight", "elapsed_s": round(time.time() - t0, 2),
            "input_tokens": r.get("input_tokens"),
            "output_tokens": r.get("output_tokens"), "turns": 1,
            "model": (judge_spec or {}).get("api_model"),
            "reasoning_effort": r.get("reasoning_effort"),
            "reasoning_policy": r.get("reasoning_policy"),
            "status": r.get("status"),
            "parse_fallback": r.get("parse_fallback", False),
            "infra_error": r.get("status") != "ok",
            "trajectory": r.get("trajectory")}
    return verdict, meta


def _xai_judge_heavy(task_md: str, rubric_md: str, skill_md: str,
                     world_before: Path, world_after: Path, rule_result: dict,
                     judge_model: str | None) -> tuple[dict, dict]:
    """Heavy judge (as in synthesis): an agentic session over the before/after snapshots."""
    # Pass a model_ref only when the caller requested one; otherwise the role default applies.
    from wildawm.synthesis import harness as hns
    _, _, jd, xcfg = _xai_modules()
    harness = hns.resolve_harness("heavy_judge")
    proxy = None
    if harness == "claude-code":
        model_ref = judge_model or getattr(xcfg, "HEAVY_JUDGE_MODEL", HEAVY_JUDGE_MODEL)
    else:
        model_ref = judge_model or None
    t0 = time.time()
    r = jd.run_judge(Path(world_before), Path(world_after), task_md, rubric_md,
                     skill_md, rule_result, model_ref=model_ref, proxy_url=proxy)
    call = r.get("call") or {}
    extra = call.get("extra") or {}
    # Record the harness and actual judge model for the verdict file.
    model_label = model_ref or getattr(xcfg, "HEAVY_JUDGE_MODEL", HEAVY_JUDGE_MODEL)
    proxy_label = proxy
    if harness == "opencode":
        om = (hns.opencode_model_from_ref(model_ref)
              if model_ref else hns.resolve_opencode_model("heavy_judge"))
        model_label = (f"opencode/{om.model}"
                       f"+vision:{xcfg.opencode_vision_spec()['api_model']}")
        proxy_label = om.base_url
    verdict = {"verdict": r.get("verdict"),
               "confidence": r.get("confidence"),
               "reasoning": r.get("justification", ""),
               "status": r.get("status"),
               "too_strict": r.get("too_strict", []),
               "genuine_miss": r.get("genuine_miss", [])}
    meta = {"mode": "heavy", "elapsed_s": round(time.time() - t0, 2),
            "input_tokens": extra.get("input_tokens"),
            "output_tokens": extra.get("output_tokens"),
            "total_tokens": extra.get("total_tokens"),
            "turns": call.get("num_turns", 0), "model": model_label,
            "harness": harness, "proxy": proxy_label, "status": r.get("status"),
            "n_tool_uses": extra.get("n_tool_uses"),
            "n_vision_calls": extra.get("n_vision_calls"),
            "infra_error": (r.get("status") not in ("ok",))
                           or r.get("verdict") == "infra",
            "trajectory": r.get("trajectory")}
    return verdict, meta


def _xai_tasks_for_world(world_id: str) -> list[dict]:
    """Return XAI tasks eligible for this world's (profile, day)."""
    meta = _read_instance_meta(world_id)
    profile = meta.get("profile", "")
    day = meta.get("started_at_day")
    if not profile or day is None:
        return []
    registry = _load_xai_registry()
    result = []
    for task_name, info in registry.items():
        has_assets = _task_has_assets(task_name, info)
        eligible = False
        for rule in info.get("eligible", []):
            if rule.get("profile") == profile and day in rule.get("days", []):
                eligible = True
                break
        if not eligible:
            continue
        result.append({
            "task_name": task_name,
            "title": info.get("title", task_name),
            "description": info.get("description", ""),
            "interfaces": info.get("interfaces", []),
            "interface_flow": info.get("interface_flow", ""),
            "difficulty": info.get("difficulty", "unknown"),
            "has_assets": has_assets,
            "has_world_patch": bool(info.get("has_world_patch")),
            "format": "xai3",
            "verification_checks": info.get("verification_checks", []),
        })
    return result


@app.get("/api/xai-tasks")
async def list_xai_tasks_global():
    """List all cross-agent-interface tasks from the registry."""
    registry = _load_xai_registry()
    # Cache each profile's compiled days to avoid re-scanning per task.
    avail_cache: dict[str, list[int]] = {}
    tasks = []
    for task_name, info in registry.items():
        has_assets = _task_has_assets(task_name, info)
        # A task is runnable only if its assets exist and an eligible (profile, day) is on disk.
        eligible = []
        task_ready = False
        for rule in info.get("eligible", []):
            prof = rule.get("profile", "")
            if prof not in avail_cache:
                avail_cache[prof] = _available_days_safe(prof)
            avail = avail_cache[prof]
            days = list(rule.get("days", []))
            days_available = [d for d in days if d in avail]
            rule_ready = has_assets and bool(days_available)
            task_ready = task_ready or rule_ready
            eligible.append({
                **rule,
                "days_available": days_available,
                "ready": rule_ready,
            })
        task = {
            "task_name": task_name,
            "title": info.get("title", task_name),
            "description": info.get("description", ""),
            "interfaces": info.get("interfaces", []),
            "interface_flow": info.get("interface_flow", ""),
            "difficulty": info.get("difficulty", "unknown"),
            "eligible": eligible,
            "verification_checks": info.get("verification_checks", []),
            "files": info.get("files", {}),
            "has_assets": has_assets,
            "has_world_patch": bool(info.get("has_world_patch")),
            "ready": task_ready,
            "format": "xai3",
        }
        if "estimated_steps" in info:
            task["estimated_steps"] = info["estimated_steps"]
        tasks.append(task)
    return {"tasks": tasks, "count": len(tasks)}


@app.get("/api/xai-tasks/{task_name}")
async def get_xai_task_global(task_name: str):
    """Return one XAI task's registry metadata and assets (world-independent)."""
    registry = _load_xai_registry()
    info = registry.get(task_name)
    if info is None:
        raise HTTPException(404, f"XAI task not found in registry: {task_name}")
    task_dir = _xai_task_dir(task_name, info)

    def _read(p: Path) -> str | None:
        return p.read_text() if p.exists() else None
    return {
        "task_name": task_name,
        "title": info.get("title", task_name),
        "description": info.get("description", ""),
        "interfaces": info.get("interfaces", []),
        "interface_flow": info.get("interface_flow", ""),
        "difficulty": info.get("difficulty", "unknown"),
        "eligible": info.get("eligible", []),
        "verification_checks": info.get("verification_checks", []),
        "estimated_steps": info.get("estimated_steps"),
        "files": info.get("files", {}),
        "has_assets": _task_has_assets(task_name, info),
        "has_world_patch": bool(info.get("has_world_patch")),
        "task_md": _read(task_dir / "task.md"),
        "judge_md": _read(task_dir / "verifier" / "rubrics.md"),
        "skill_md": _read(task_dir / "verifier" / "verify.skill.md"),
    }


@app.get("/api/worlds/{world_id}/xai-tasks")
async def list_xai_tasks(world_id: str):
    """List cross-agent-interface tasks eligible for this world."""
    _world_path(world_id)
    tasks = _xai_tasks_for_world(world_id)
    return {"tasks": tasks, "count": len(tasks)}


@app.get("/api/worlds/{world_id}/xai-tasks/{task_name}")
async def get_xai_task(world_id: str, task_name: str):
    """Return XAI task assets, including judge_md and skill_md for 3-layer tasks."""
    _world_path(world_id)
    info = _load_xai_registry().get(task_name) or {}
    task_dir = _xai_task_dir(task_name, info)
    if not task_dir.is_dir():
        raise HTTPException(404, f"XAI task not found: {task_name}")

    def _r(p: Path):
        return p.read_text() if p.exists() else None
    return {"task_name": task_name, "task_md": _r(task_dir / "task.md"),
            "simple_task_md": _r(task_dir / "simple_task.txt"),
            "judge_md": _r(task_dir / "verifier" / "rubrics.md"),
            "skill_md": _r(task_dir / "verifier" / "verify.skill.md")}


@app.post("/api/worlds/{world_id}/xai-tasks/{task_name}/episode/start")
async def xai_task_episode_start(world_id: str, task_name: str, request: Request):
    """Start an XAI task episode: snapshot the world and return task.md."""
    _world_path(world_id)
    task_dir = _xai_task_dir(task_name)
    await asyncio.to_thread(_ensure_xai_task_patch, world_id, task_dir)
    task_md_path = task_dir / "task.md"
    if not task_md_path.exists():
        raise HTTPException(404, f"task.md not found for XAI task {task_name!r}")
    body = await request.json() if (await request.body()) else {}
    simple_task_path = task_dir / "simple_task.txt"
    use_simple = bool(body.get("use_simple_task", True))
    simple_task_md = simple_task_path.read_text() if simple_task_path.exists() else None
    agent_task_md = simple_task_md if use_simple and simple_task_md else task_md_path.read_text()
    episode_id = f"xai_{task_name}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    await asyncio.to_thread(_snapshot_world, world_id, "before", episode_id)
    return {
        "episode_id": episode_id,
        "event": task_name,
        "task_md": task_md_path.read_text(),
        "simple_task_md": simple_task_md,
        "agent_task_md": agent_task_md,
        "agent_task_source": "simple_task.txt" if agent_task_md == simple_task_md else "task.md",
        "is_xai": True,
    }


@app.post("/api/worlds/{world_id}/xai-tasks/{task_name}/episode/{episode_id}/verify")
async def xai_task_episode_verify(world_id: str, task_name: str,
                                  episode_id: str, request: Request):
    """Verify an XAI task episode. Runs the 3-layer verifier then LLM judge."""
    _world_path(world_id)
    body = await request.json() if (await request.body()) else {}
    mode = body.get("mode", "lightweight")
    session_id = body.get("session_id") or world_id
    try:
        judge_spec = _models.resolve(body.get("judge_model"))
    except _models.ModelError as e:
        raise HTTPException(400, f"judge model error: {e}")

    info = _load_xai_registry().get(task_name) or {}
    task_dir = _xai_task_dir(task_name, info)
    task_md_path = task_dir / "task.md"
    judge_md_path = task_dir / "verifier" / "rubrics.md"
    if not (task_md_path.exists() and (task_dir / "verifier").is_dir()):
        raise HTTPException(404, f"task assets incomplete for {task_name!r}")

    episode_root = _episodes_dir(world_id) / episode_id
    before_dir = episode_root / "before"
    if not before_dir.is_dir():
        raise HTTPException(404, f"no 'before' snapshot for episode {episode_id}")

    after_dir = await asyncio.to_thread(_snapshot_world, world_id, "after", episode_id)

    agent_reply = await asyncio.to_thread(
        _read_agent_last_reply, world_id, session_id, body.get("kind"))
    agent_reply_path = episode_root / "agent_reply.txt"
    agent_reply_path.write_text(agent_reply or "")

    wp = _world_path(world_id)
    baseline_dir = _xai_baseline_dir(world_id, task_dir) or before_dir
    # Layer 1: the verifier's raw rule_result, the same evidence synthesis feeds its judges.
    try:
        rd = await asyncio.to_thread(
            _xai_verifier_rd, task_dir, world_id, wp, baseline_dir)
        judge_stdout, judge_stderr = _xai_evidence_text(rd), ""
    except Exception as e:  # noqa: BLE001
        rd = {"verdict": "world_imperfect", "scripts": [], "n_scripts": 0,
              "script_errors": []}
        judge_stdout, judge_stderr = "", f"xai3 verifier error: {e}"

    task_md = task_md_path.read_text()
    judge_md = judge_md_path.read_text()

    # The judges reuse the synthesis implementation.
    if mode == "heavy":
        skill_path = task_dir / "verifier" / "verify.skill.md"
        skill_md = skill_path.read_text() if skill_path.exists() else ""
        verdict, meta = await asyncio.to_thread(
            _xai_judge_heavy, task_md, judge_md, skill_md, before_dir, after_dir,
            rd, body.get("judge_model"))
        judge_input_for_ui = ("[heavy mode — agentic judge explored snapshots; "
                              "see verdict.reasoning]")
    else:
        verdict, meta = await asyncio.to_thread(
            _xai_judge_lightweight, task_md, judge_md, rd, judge_spec)
        judge_input_for_ui = judge_stdout
    agent_stats = await asyncio.to_thread(
        _runtime_agent_stats, world_id, body.get("kind"), session_id,
        body.get("agent_seconds"))
    stats = finalize_stats(agent_stats, judge_stats_from_meta(meta))
    meta["stats"] = stats

    return {
        "mode": mode,
        "episode_id": episode_id,
        "event": task_name,
        "is_xai": True,
        "agent_reply": agent_reply,
        "judge_py_stdout": judge_stdout,
        "judge_py_stderr": judge_stderr,
        "judge_input": judge_input_for_ui,
        "verdict": verdict,
        "meta": meta,
        "verifier": rd,
        "judge": {"verdict": verdict, "meta": meta},
        "stats": stats,
    }


# Episode export: a self-contained, restorable outputs/ folder that supports offline re-verify.

def _safe_component(s: str, fallback: str = "x") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()).strip("-")
    return s or fallback


def _display_path(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _tar_gz_dir(src_dir: Path, dest: Path) -> int:
    """tar.gz a directory (arcname = its basename); returns the byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src_dir, arcname=src_dir.name)
    return dest.stat().st_size


def _repo_commit() -> dict:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(PROJECT_ROOT), capture_output=True,
                              text=True, timeout=SUBPROCESS_FAST_TIMEOUT_S)
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               cwd=str(PROJECT_ROOT), capture_output=True,
                               text=True, timeout=SUBPROCESS_FAST_TIMEOUT_S)
        return {"commit": (head.stdout or "").strip() or None,
                "dirty": bool((dirty.stdout or "").strip())}
    except Exception:  # noqa: BLE001
        return {"commit": None, "dirty": None}


def _agent_model_ref(world_id: str, kind: str) -> str | None:
    rec = (_runtimes.get(world_id) or {}).get("agents", {}).get(kind) or {}
    return rec.get("model")


def _agent_reasoning_effort(world_id: str, kind: str) -> str | None:
    rec = (_runtimes.get(world_id) or {}).get("agents", {}).get(kind) or {}
    return rec.get("reasoning_effort")


@app.post("/api/worlds/{world_id}/episodes/{episode_id}/export")
async def export_episode(world_id: str, episode_id: str, request: Request):
    """Bundle an episode into outputs/<ts>__<task>__<kind>__<model>/."""
    _world_path(world_id)
    body = await request.json() if (await request.body()) else {}
    kind = body.get("kind") or _agents.DEFAULT_KIND
    session_id = body.get("session_id") or _default_session_id(world_id, kind)
    is_xai = bool(body.get("is_xai"))
    verify = body.get("verify") or {}

    # Resolve the task name from the body, else from the episode id.
    task = body.get("task")
    if not task:
        # event: "{event}_{ts}_{hex}"; xai: "xai_{task}_{ts}_{hex}"
        stem = episode_id
        if stem.startswith("xai_"):
            stem = stem[4:]
            is_xai = True
        parts = stem.rsplit("_", 2)
        task = parts[0] if len(parts) == 3 else stem

    episode_root = _episodes_dir(world_id) / episode_id
    before_dir = episode_root / "before"
    after_dir = episode_root / "after"
    if not before_dir.is_dir():
        raise HTTPException(404, f"no 'before' snapshot for episode {episode_id}")
    if not after_dir.is_dir():
        # Verify was not run, so snapshot 'after' now.
        after_dir = await asyncio.to_thread(_snapshot_world, world_id, "after", episode_id)

    asset_dir = _xai_task_dir(task) if is_xai else (EVENTS_DIR / task)
    task_md_path = asset_dir / "task.md"
    simple_task_path = asset_dir / "simple_task.txt"

    # An explicit `name` overrides the default folder naming scheme.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    model_ref = body.get("model") or _agent_model_ref(world_id, kind) or "model"
    reasoning_effort = body.get("reasoning_effort") or _agent_reasoning_effort(world_id, kind)
    name = (_safe_component(body["name"]) if body.get("name")
            else (f"{ts}__{_safe_component(task)}__{_safe_component(kind)}"
                  f"__{_safe_component(model_ref)}"))
    out_root = Path(body.get("out_dir") or OUTPUTS_DIR)
    if not out_root.is_absolute():
        out_root = PROJECT_ROOT / out_root
    out_dir = out_root / name
    for sub in ("trajectory", "state", "verify", "verify/judge_assets", "logs"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    # task.md is the evaluator contract; task_prompt.txt records exactly what the agent was sent.
    if task_md_path.exists():
        shutil.copyfile(task_md_path, out_dir / "task.md")
    simple_task_rel = None
    if simple_task_path.exists():
        shutil.copyfile(simple_task_path, out_dir / "simple_task.txt")
        simple_task_rel = "simple_task.txt"
    agent_task_source = body.get("agent_task_source") or (
        "simple_task.txt" if simple_task_rel else "task.md")
    agent_task_md = body.get("agent_task_md")
    if agent_task_md:
        (out_dir / "task_prompt.txt").write_text(agent_task_md)

    wc_port, wc_token = (_runtimes.get(world_id, {}).get("worldctl_port"),
                         _runtimes.get(world_id, {}).get("worldctl_token"))
    structured, raw_text, raw_ext = [], "", "txt"
    if wc_port:
        try:
            structured = await asyncio.to_thread(
                lambda: _worldctl_sync_history(wc_port, wc_token, kind, session_id))
        except Exception as e:  # noqa: BLE001
            structured = [{"type": "error", "content": f"history fetch failed: {e}"}]
        try:
            rd = await asyncio.to_thread(_worldctl.raw, wc_port, wc_token, kind, session_id)
            raw_text, raw_ext = rd.get("raw", ""), rd.get("ext", "txt")
        except Exception as e:  # noqa: BLE001
            raw_text = f"(raw unavailable: {e})"
    (out_dir / "trajectory" / "structured.json").write_text(
        json.dumps(structured, indent=2, ensure_ascii=False))
    (out_dir / "trajectory" / f"raw.{raw_ext}").write_text(raw_text)
    # Authoritative per-step model I/O from the in-container proxy, fetched via docker exec.
    proxy_trajectory = None
    try:
        cname = f"wildawm-agent-{kind}-{world_id}"
        pj = await asyncio.to_thread(lambda: subprocess.run(
            ["docker", "exec", cname, "curl", "-s",
             f"http://localhost:{LLM_PROXY_PORT}/trajectory"],
            capture_output=True, text=True, timeout=HTTP_SHORT_TIMEOUT_S))
        if pj.returncode == 0 and pj.stdout.strip().startswith("{"):
            (out_dir / "trajectory" / "proxy_trajectory.json").write_text(pj.stdout)
            proxy_trajectory = json.loads(pj.stdout)
    except Exception as e:  # noqa: BLE001
        log.warning("proxy trajectory fetch failed for %s: %s", world_id, e)
    agent_reply = body.get("agent_reply")
    if agent_reply is None:
        agent_reply = verify.get("agent_reply")
    if agent_reply is None:
        agent_reply = await asyncio.to_thread(
            _read_agent_last_reply, world_id, session_id, kind)
    (out_dir / "trajectory" / "agent_reply.txt").write_text(agent_reply or "")
    completion_summary = body.get("completion_summary")
    if completion_summary:
        (out_dir / "trajectory" / "completion_summary.md").write_text(
            completion_summary)

    verdict = verify.get("verdict")
    if verdict is not None:
        (out_dir / "verify" / "verdict.json").write_text(
            json.dumps(verdict, indent=2, ensure_ascii=False))
    (out_dir / "verify" / "judge_stdout.txt").write_text(
        verify.get("judge_py_stdout") or verify.get("judge_input") or "")
    # Copy judge assets so offline verify does not depend on the repo state.
    assets_out = out_dir / "verify" / "judge_assets"
    if is_xai:
        for asset in ("task.md", "simple_task.txt", "golden.py"):
            ap = asset_dir / asset
            if ap.exists():
                shutil.copyfile(ap, assets_out / asset)
        vdir = asset_dir / "verifier"
        if vdir.is_dir():
            shutil.copytree(vdir, assets_out / "verifier", dirs_exist_ok=True)
        patch_dir = asset_dir / _xai_world_patch.PATCH_DIRNAME
        if patch_dir.is_dir():
            shutil.copytree(patch_dir, assets_out / _xai_world_patch.PATCH_DIRNAME,
                            dirs_exist_ok=True)
    else:
        for asset in ("task.md", "judge.md", "judge.py", "skill.md"):
            ap = asset_dir / asset
            if ap.exists():
                shutil.copyfile(ap, assets_out / asset)

    before_sz = await asyncio.to_thread(
        _tar_gz_dir, before_dir, out_dir / "state" / "before.tar.gz")
    after_sz = await asyncio.to_thread(
        _tar_gz_dir, after_dir, out_dir / "state" / "after.tar.gz")

    wc_log = NANOBOT_STATE_DIR / world_id / "logs" / "wildawm.worldctl.log"
    if wc_log.exists():
        try:
            shutil.copyfile(wc_log, out_dir / "logs" / "wildawm.worldctl.log")
        except OSError:
            pass

    meta = _read_instance_meta(world_id)
    day = _world_current_day(world_id)
    verify_meta = verify.get("meta") or {}
    agent_stats = agent_stats_from_history_proxy(
        structured, proxy_trajectory, seconds=body.get("agent_seconds"))
    verify_stats = verify.get("stats") if isinstance(verify.get("stats"), dict) else {}
    stats = finalize_stats(
        verify_stats.get("agent") or agent_stats,
        verify_stats.get("judge") or judge_stats_from_meta(verify_meta),
    )
    (out_dir / "verify" / "stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False))
    patch_dir = asset_dir / _xai_world_patch.PATCH_DIRNAME if is_xai else None
    patch_rel = None
    if patch_dir and patch_dir.is_dir():
        patch_rel = (str(patch_dir.relative_to(PROJECT_ROOT))
                     if patch_dir.is_relative_to(PROJECT_ROOT) else str(patch_dir))
    manifest = {
        "run_id": name,
        "created_at": _utc_now_iso(),
        "episode_id": episode_id,
        "task": {"name": task, "kind": "xai" if is_xai else "event",
                 "asset_dir": _display_path(asset_dir) if asset_dir.exists() else None,
                 "world_patch": patch_rel,
                 "agent_task_source": agent_task_source},
        "world": {
            "id": world_id, "profile": meta.get("profile"),
            "started_at_day": meta.get("started_at_day"),
            "started_at_date": meta.get("started_at_date"),
            "current_day": day.get("current_day"),
            "frozen_clock_date": meta.get("frozen_clock_date"),
            "frozen_clock_time": meta.get("frozen_clock_time"),
        },
        "agent": {"kind": kind, "model_ref": model_ref, "session_id": session_id,
                  "reasoning_effort": reasoning_effort},
        "judge": {"model_ref": body.get("judge_model"),
                  "mode": body.get("mode") or verify.get("mode")},
        "verdict": (verdict or {}).get("verdict") if isinstance(verdict, dict) else None,
        "timing": {"agent_seconds": body.get("agent_seconds"),
                   "verify": verify_meta.get("elapsed_s")},
        "completion_probe": body.get("completion_probe"),
        "agent_error": body.get("agent_error"),
        "agent_error_recovered": body.get("agent_error_recovered"),
        "tokens": {"judge": {"input": verify_meta.get("input_tokens"),
                             "output": verify_meta.get("output_tokens")}},
        "stats": stats,
        "repo": _repo_commit(),
        "artifacts": {
            "task_md": "task.md",
            "simple_task": simple_task_rel,
            "agent_task_prompt": "task_prompt.txt" if agent_task_md else None,
            "trajectory_structured": "trajectory/structured.json",
            "trajectory_raw": f"trajectory/raw.{raw_ext}",
            "trajectory_proxy": "trajectory/proxy_trajectory.json",
            "agent_reply": "trajectory/agent_reply.txt",
            "completion_summary": (
                "trajectory/completion_summary.md" if completion_summary else None),
            "state_before": "state/before.tar.gz",
            "state_after": "state/after.tar.gz",
            "verdict": "verify/verdict.json" if verdict is not None else None,
            "stats": "verify/stats.json",
            "judge_assets": "verify/judge_assets",
        },
        "state_bytes": {"before": before_sz, "after": after_sz},
        "verify_cmd": f"awm verify --dir {_display_path(out_dir)}",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    rel = _display_path(out_dir)
    log.info("exported episode %s -> %s", episode_id, rel)
    return {"output_dir": str(rel), "name": name, "manifest": manifest}


def _worldctl_sync_history(port: int, token: str | None, kind: str, session_id: str):
    """Sync history fetch for the export worker thread."""
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _worldctl._get(port, token, f"/agents/{kind}/history{sid}",
                          timeout=HTTP_MEDIUM_TIMEOUT_S).get("history", [])


@app.get("/api/events")
async def list_events():
    events = []
    if not EVENTS_DIR.is_dir():
        return {"events": [], "count": 0}
    for d in sorted(EVENTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        skill = d / "skill.md"
        if not skill.exists():
            continue
        events.append({"name": d.name, "description": _event_first_paragraph(skill.read_text())})
    events.sort(key=lambda e: e["name"])
    return {"events": events, "count": len(events)}


def _event_first_paragraph(content: str) -> str:
    desc = ""
    for line in content.split("\n"):
        if line.startswith("## Description"):
            continue
        if line.strip() and not line.startswith("#") and not desc:
            desc = line.strip()[:200]
    return desc


@app.get("/api/events/{name}")
async def get_event(name: str):
    p = EVENTS_DIR / name / "skill.md"
    if not p.exists():
        raise HTTPException(404, f"Event skill not found: {name}")
    return {"name": name, "content": p.read_text()}


@app.get("/api/stories/{profile}")
async def list_stories(profile: str):
    sd = STORIES_DIR / profile
    if not sd.exists():
        raise HTTPException(404, f"Stories not found for: {profile}")
    dates = sorted(p.stem for p in sd.glob("*.md") if p.stem != "state")
    return {"profile": profile, "dates": dates}


@app.get("/api/stories/{profile}/{date}")
async def get_story(profile: str, date: str):
    p = STORIES_DIR / profile / f"{date}.md"
    if not p.exists():
        raise HTTPException(404, f"Story not found: {profile}/{date}")
    return {"profile": profile, "date": date, "content": p.read_text()}


STATIC_DIR = Path(__file__).parent / "static"


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# API routes must be registered before this catch-all static mount.

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.on_event("startup")
async def restore_runtime_registry():
    # Blocking work runs in threads; widen the default executor for many parallel runs.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=64, thread_name_prefix="wawm-worker"))

    # Rediscover running worlds after a restart (skippable for a fresh dashboard root).
    if os.environ.get("AWM_DASHBOARD_SKIP_DOCKER_RESYNC") != "1":
        await asyncio.to_thread(_resync_runtimes_from_docker)


@app.on_event("shutdown")
async def shutdown_runtimes():
    # Leave running stacks alone; the next start resyncs the registry.
    pass


if __name__ == "__main__":
    args = parse_args(DashboardArgs)
    print(f"WildAWM Dashboard: http://{args.host}:{args.port}")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Worlds dir:   {WORLDS_DIR}")
    print(f"  Log file:     {LOG_DIR / 'wildawm.dashboard.log'}")
    log.info("dashboard starting on %s:%s", args.host, args.port)
    # Mirror uvicorn's loggers into the dashboard log file.
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _ul = logging.getLogger(_name)
        _ul.addHandler(_fh)
    uvicorn.run(app, host=args.host, port=args.port)
