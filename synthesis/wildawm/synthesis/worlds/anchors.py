"""Directory-anchor resolver for pre-baked world snapshots."""
from __future__ import annotations

import datetime as _dt
import contextlib
import fcntl
import hashlib
import json
import re
import shutil
import stat
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.webharbor import lib as _wh_lib

_DEFAULT_START_DATE = "2026-01-02"


def master_root(profile: str) -> Path:
    """The profile's master root (day_N snapshots, days.json, scripts/ marker)."""
    return cfg.WS_MASTERS_DIR / profile


def master_snapshot_dir(profile: str, day: int) -> Path:
    """The pre-baked cumulative day-N snapshot dir (data/ workspace/ webharbor/ overlay/)."""
    return cfg.WS_MASTERS_DIR / profile / f"day_{day}"


def master_scripts_dir(profile: str) -> Path:
    """The scripts/ marker dir at the master root; its presence marks a pre-baked profile."""
    return master_root(profile) / "scripts"


def days_json_path(profile: str) -> Path:
    """``{day:int -> date}`` map for the profile."""
    return cfg.WS_MASTERS_DIR / profile / "days.json"


def start_date(profile: str) -> str:
    """The persona's day-0 frozen-clock date, read from its signature.json."""
    sig = cfg.WS_PROFILES_DIR / profile / "signature.json"
    if sig.exists():
        try:
            v = json.loads(sig.read_text()).get("start_date")
            if isinstance(v, str) and len(v) >= 10:
                return v[:10]
        except (OSError, ValueError):
            pass
    return _DEFAULT_START_DATE


def day_date(profile: str, day: int) -> str:
    """The frozen-clock date for day N = persona start_date + N days (deterministic)."""
    try:
        base = _dt.date.fromisoformat(start_date(profile))
    except ValueError:
        base = _dt.date.fromisoformat(_DEFAULT_START_DATE)
    return (base + _dt.timedelta(days=day)).isoformat()


@contextlib.contextmanager
def _inspection_world_lock(profile_safe: str, day: int):
    cfg.SCRATCH.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.SCRATCH / f"ws_inspect_{profile_safe}_d{day}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _rmtree_missing_ok(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def _seed_cache_signature() -> str:
    """Bind inspection copies to the exact pristine WebHarbor seed inputs."""

    records = []
    for site in _wh_lib.SITE_ORDER:
        path = _wh_lib.cached_seed_path(site)
        try:
            info = path.lstat()
        except FileNotFoundError:
            records.append({"path": str(path), "state": "missing"})
            continue
        records.append({
            "ctime_ns": info.st_ctime_ns,
            "ino": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
            "path": str(path),
            "size": info.st_size,
            "state": "present",
        })
    return hashlib.sha256(json.dumps(
        records, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def inspection_world(profile: str, day: int) -> Path:
    """Seed-filled, cached scratch copy of the day-N snapshot; the master is never mutated."""
    snap = master_snapshot_dir(profile, day)
    if not snap.is_dir():
        return snap
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", profile)
    copy = cfg.SCRATCH / f"ws_inspect_{safe}_d{day}"
    marker = copy.parent / f"{copy.name}.src"
    src_sig = (
        f"{snap.stat().st_mtime_ns}:{snap.stat().st_size}:"
        f"{_seed_cache_signature()}"
    )
    with _inspection_world_lock(safe, day):
        if copy.is_dir() and marker.exists() and marker.read_text() == src_sig:
            return copy
        if copy.exists():
            _rmtree_missing_ok(copy)
        shutil.copytree(snap, copy, symlinks=True)
        _wh_lib.ensure_all_webharbor_seeds(copy)
        marker.write_text(src_sig)
    return copy
