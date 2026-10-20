#!/usr/bin/env python3
"""Build the deterministic (persona, day) world.tar.zst used by Harbor task packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import date, timedelta
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.webharbor import lib as wh_lib

DB_FILE_BY_SITE = wh_lib.DB_FILE_BY_SITE
SITE_ORDER = tuple(wh_lib.all_sites())

_CANONICAL_WORLD_ID = "wildawm-harbor-initial-state"
_CANONICAL_CREATED_AT = "1970-01-01T00:00:00Z"

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()




def ensure_all_webharbor_seeds(world: Path, seed_cache: Path) -> list[str]:
    """Fill missing site DBs from the seed cache; existing world DBs are kept."""
    destination_dir = world / "webharbor"
    destination_dir.mkdir(parents=True, exist_ok=True)
    missing_cache = [
        str(seed_cache / site / DB_FILE_BY_SITE[site])
        for site in SITE_ORDER
        if not (destination_dir / DB_FILE_BY_SITE[site]).exists()
        and not (seed_cache / site / DB_FILE_BY_SITE[site]).is_file()
    ]
    if missing_cache:
        raise RuntimeError(
            "missing required WebHarbor seed cache entries: "
            + ", ".join(missing_cache))
    seeded: list[str] = []
    for site in SITE_ORDER:
        destination = destination_dir / DB_FILE_BY_SITE[site]
        if not destination.exists():
            shutil.copy2(seed_cache / site / DB_FILE_BY_SITE[site], destination)
            seeded.append(site)
        wh_lib._apply_seed_patches(site, destination)
    return seeded


def _stage_world(master_day: Path, world: Path, seed_cache: Path) -> list[str]:
    for name in ("data", "workspace", "webharbor", "overlay"):
        source = master_day / name
        if source.is_dir():
            shutil.copytree(source, world / name, symlinks=True)
    return ensure_all_webharbor_seeds(world, seed_cache)


def _days_map(master: Path) -> dict[int, str]:
    raw = json.loads((master / "days.json").read_text())
    return {int(key): str(value) for key, value in raw.items()}


def _world_meta(profile: str, day: int, days: dict[int, str]) -> dict:
    started = days.get(day)
    frozen = days.get(day + 1)
    if frozen is None and started is not None:
        frozen = (date.fromisoformat(started) + timedelta(days=1)).isoformat()
    return {
        "id": _CANONICAL_WORLD_ID,
        "profile": profile,
        "started_at_day": day,
        "started_at_date": started,
        "frozen_clock_date": frozen,
        "frozen_clock_time": "10:00:00",
        "created_at": _CANONICAL_CREATED_AT,
    }


def _render_world_md(wildawm_root: Path, world: Path, meta: dict,
                     mcp_gateway_url: str, browser_mcp_port: int) -> str:
    """Render the world briefing for the single-container Harbor runtime."""
    os.environ["AWM_MCP_GATEWAY_URL"] = mcp_gateway_url
    os.environ["AWM_BROWSER_MCP_PORT"] = str(browser_mcp_port)
    os.environ["AWM_WS_PROFILES_DIR"] = str(wildawm_root / "worlds" / "_profiles")
    from wildawm.synthesis.harbor_export import world_md  # noqa: E402
    return world_md.render_world_md(
        profile_name=meta["profile"],
        meta=meta,
        data_dir=world / "data",
        workspace_dir=world / "workspace",
    )


def _sorted_tree(path: Path) -> list[Path]:
    """Deterministic walk of root and its descendants sorted by posix path; symlinks are not followed."""
    paths = [path]
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        for name in dirnames + filenames:
            paths.append(base / name)
    return sorted(paths, key=lambda row: row.relative_to(path.parent).as_posix())


def _normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}
    return info


def _write_deterministic_tar(world: Path, output_tar: Path,
                             arc_root: str = "world") -> None:
    with tarfile.open(
        output_tar, mode="w", format=tarfile.GNU_FORMAT, dereference=False,
    ) as archive:
        for item in _sorted_tree(world):
            archive.add(
                item,
                arcname=f"{arc_root}/{item.relative_to(world).as_posix()}",
                recursive=False,
                filter=_normalize_tar_info,
            )


def build_world(
    *,
    profile: str,
    day: int,
    masters_dir: Path,
    seed_cache: Path,
    wildawm_root: Path,
    output: Path,
    mcp_gateway_url: str,
    browser_mcp_port: int,
    zstd_level: int = 3,
) -> dict:
    master = masters_dir / profile
    master_day = master / f"day_{day}"
    if not master_day.is_dir():
        raise FileNotFoundError(f"day snapshot not found: {master_day}")
    days = _days_map(master)

    manifest: dict = {
        "schema_version": 1,
        "profile": profile,
        "day": day,
        "frozen_clock_date": None,
        "frozen_clock_time": "10:00:00",
    }

    with tempfile.TemporaryDirectory(prefix="wildawm-harbor-world-") as raw:
        staging = Path(raw) / "world"
        staging.mkdir()
        seeded = _stage_world(master_day, staging, seed_cache)
        meta = _world_meta(profile, day, days)
        (staging / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        world_md = _render_world_md(
            wildawm_root, staging, meta, mcp_gateway_url, browser_mcp_port)
        (staging / "workspace" / "WORLD.md").write_text(world_md)
        (staging / "workspace" / "USER.md").write_text(world_md)
        # OpenCode auto-loads AGENTS.md from /workspace, which delivers the briefing.
        (staging / "workspace" / "AGENTS.md").write_text(world_md)
        manifest["frozen_clock_date"] = meta["frozen_clock_date"]
        manifest["seeded_sites"] = seeded

        plain_tar = Path(raw) / "world.tar"
        _write_deterministic_tar(staging, plain_tar)

        output.parent.mkdir(parents=True, exist_ok=True)
        with plain_tar.open("rb") as stdin, output.open("wb") as stdout:
            zstd = subprocess.run(
                ["zstd", "-q", "-T1", f"-{zstd_level}", "-"],
                stdin=stdin, stdout=stdout, check=True,
                timeout=cfg.HARBOR_BLOB_TIMEOUT_S)
        assert zstd.returncode == 0

    zstd_version = subprocess.run(
        ["zstd", "--version"], capture_output=True, text=True,
        check=True, timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S).stdout.strip()

    manifest.update({
        "blob_sha256": _sha256_file(output),
        "bytes": output.stat().st_size,
        "zstd": {"level": zstd_level, "threads": 1, "version": zstd_version},
        "tar": {"format": "GNU", "mtime": 0, "uid": 0, "gid": 0,
                "entries": "sorted-by-posix-path", "arc_root": "world"},
        "sources": {
            "day_snapshot": str(master_day),
            "seed_cache": str(seed_cache),
            "renderer": str(Path(__file__).resolve().parent / "world_md.py"),
            "days_sha256": _sha256_file(master / "days.json"),
        },
        "output": str(output),
    })
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True,
                        help="persona profile dir name under --masters-dir")
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--masters-dir", type=Path, default=None,
                        help="worlds/_masters dir; default: $AWM_WS_MASTERS_DIR "
                             "(WILDAWM_DATA_ROOT-aware), else "
                             "$WILDAWM_SOURCE_ROOT/worlds/_masters")
    parser.add_argument("--seed-cache", type=Path, default=None,
                        help="webharbor_seeds dir; default: "
                             "$AWM_WEBHARBOR_SEED_CACHE_DIR "
                             "(WILDAWM_DATA_ROOT-aware), else "
                             "$WILDAWM_SOURCE_ROOT/webharbor_seeds")
    parser.add_argument("--wildawm-root", type=Path, default=None,
                        help="WildAWM source root (recorded in the manifest; "
                             "also anchors --masters-dir/--seed-cache when "
                             "they are not given); default: "
                             "$WILDAWM_SOURCE_ROOT")
    parser.add_argument("--output", type=Path, required=True,
                        help="output world.tar.zst path")
    parser.add_argument("--mcp-gateway-url", default="http://127.0.0.1:18080")
    parser.add_argument("--browser-mcp-port", type=int, default=8931)
    parser.add_argument("--zstd-level", type=int, default=3)
    parser.add_argument("--index", type=Path, default=None,
                        help="append the blob manifest to this JSON index")
    args = parser.parse_args()

    source_root = os.environ.get("WILDAWM_SOURCE_ROOT", "").strip()
    source_root_path = Path(source_root) if source_root else None

    masters_dir = args.masters_dir
    seed_cache = args.seed_cache
    if masters_dir is None or seed_cache is None:
        # Prefer const_config data roots, then WILDAWM_SOURCE_ROOT.
        from wildawm import const_config as cfg
        if masters_dir is None:
            masters_dir = (Path(cfg.WS_MASTERS_DIR)
                           if source_root_path is None
                           else source_root_path / "worlds" / "_masters")
        if seed_cache is None:
            seed_cache = (Path(cfg.WEBHARBOR_SEED_CACHE_DIR)
                          if source_root_path is None
                          else source_root_path / "webharbor_seeds")
    wildawm_root = args.wildawm_root or source_root_path
    if wildawm_root is None:
        parser.error(
            "--wildawm-root not given and WILDAWM_SOURCE_ROOT is unset. Pass "
            "--wildawm-root, or point WILDAWM_SOURCE_ROOT at the read-only "
            "WildAWM source tree (--masters-dir/--seed-cache otherwise default "
            "to WILDAWM_DATA_ROOT / AWM_WS_MASTERS_DIR / "
            "AWM_WEBHARBOR_SEED_CACHE_DIR).")

    manifest = build_world(
        profile=args.profile,
        day=args.day,
        masters_dir=masters_dir,
        seed_cache=seed_cache,
        wildawm_root=wildawm_root,
        output=args.output,
        mcp_gateway_url=args.mcp_gateway_url,
        browser_mcp_port=args.browser_mcp_port,
        zstd_level=args.zstd_level,
    )
    print(json.dumps(manifest, indent=2))

    if args.index is not None:
        args.index.parent.mkdir(parents=True, exist_ok=True)
        entries = {}
        if args.index.is_file():
            entries = json.loads(args.index.read_text())
        key = f"{args.profile}/day_{args.day}"
        entries[key] = manifest
        args.index.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
