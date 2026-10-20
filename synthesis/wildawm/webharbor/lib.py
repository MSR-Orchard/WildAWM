#!/usr/bin/env python3
"""WebHarbor as a replayed world artifact: ``webharbor/<dbfile>`` is a peer of ``data/<app>.db``."""
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from simpleArgParser import parse_args_with_commands

from wildawm.const_config import (
    DOCKER_COMMAND_TIMEOUT_S,
    WEBHARBOR_BASE_PORT,
    WEBHARBOR_SEED_CACHE_DIR,
)
from wildawm.core.paths import PROJECT_ROOT
from wildawm.json_utils import load_jsonc

# Upstream WebHarbor image, pinned by digest so the day-0 seed is reproducible.
PINNED_UPSTREAM_IMAGE = (
    "wildawm/wildawm-webharbor-base@sha256:"
    "0c68c23b176d8ce6ab0e69d0a4baf97d1ccd9863b037142b63121125dc4390c5"
)
# Runtime render sidecar image.
WEBHARBOR_IMAGE = "wildawm-webharbor:latest"
# Creator image: upstream WebHarbor plus the tools the in-world synthesizer needs.
CREATOR_IMAGE = "wildawm-creator:latest"

# Per-site DB filenames (most are "<id>.db").
DB_FILE_BY_SITE: dict[str, str] = {
    "allrecipes": "allrecipes.db",
    "amazon": "amazon_store.db",
    "apple": "apple_store.db",
    "arxiv": "arxiv.db",
    "bbc_news": "bbc_news.db",
    "booking": "booking.db",
    "github": "github_mirror.db",
    "google_flights": "google_flights.db",
    "google_map": "gmaps.db",
    "google_search": "google_search.db",
    "huggingface": "hf.db",
    "wolfram_alpha": "wolfram_alpha.db",
    "cambridge_dictionary": "cambridge.db",
    "coursera": "coursera.db",
    "espn": "espn.db",
}

# Boot order equals port order, starting at BASE_PORT.
SITE_ORDER = [
    "allrecipes", "amazon", "apple", "arxiv", "bbc_news", "booking", "github",
    "google_flights", "google_map", "google_search", "huggingface",
    "wolfram_alpha", "cambridge_dictionary", "coursera", "espn",
]
BASE_PORT = WEBHARBOR_BASE_PORT

# Local cache of pristine seed DBs, so the materializer can seed webharbor/ offline.
SEED_CACHE = WEBHARBOR_SEED_CACHE_DIR

_SITES_JSON = PROJECT_ROOT / "docker" / "webharbor" / "sites.json"
_AMAZON_KEYBOARD_CASE_PATCH = {
    "name": "Logitech Combo Touch iPad Keyboard Case",
    "slug": "logitech-combo-touch-ipad-keyboard-case",
    "description": (
        "Logitech Combo Touch iPad Keyboard Case. Folio-style case with "
        "integrated keyboard and trackpad for tablet typing."
    ),
    "features": (
        '["Integrated keyboard and trackpad", "Protective folio case", '
        '"Adjustable kickstand", "Tablet shortcut keys", "Detachable keyboard"]'
    ),
    "specs": (
        '{"Brand": "Logitech", "Compatibility": "iPad", '
        '"Layout": "Keyboard Case", "Trackpad": "Yes"}'
    ),
    "feature_tags": '["ipad", "keyboard case", "folio", "trackpad", "tablet"]',
}


def _load_sites_json() -> list[dict]:
    if _SITES_JSON.exists():
        try:
            return load_jsonc(_SITES_JSON).get("sites", [])
        except ValueError:
            pass
    return []


def site_registry() -> list[dict]:
    """Return [{id, port, dbfile, domains}], merging sites.json with the dbfile map."""
    by_id = {s["id"]: s for s in _load_sites_json()}
    out: list[dict] = []
    for i, sid in enumerate(SITE_ORDER):
        sj = by_id.get(sid, {})
        out.append({
            "id": sid,
            "port": sj.get("port", BASE_PORT + i),
            "dbfile": DB_FILE_BY_SITE[sid],
            "domains": sj.get("domains", []),
        })
    return out


def all_sites() -> list[str]:
    return list(SITE_ORDER)


def port_for_site(site: str) -> int:
    return BASE_PORT + SITE_ORDER.index(site)


def dbfile_for_site(site: str) -> str:
    return DB_FILE_BY_SITE[site]


def app_to_site(app: str) -> str | None:
    """'web_amazon' -> 'amazon'. Returns None for non-web app names."""
    if app and app.startswith("web_"):
        s = app[len("web_"):]
        if s in DB_FILE_BY_SITE:
            return s
    return None


def site_to_app(site: str) -> str:
    return f"web_{site}"


def webharbor_dir(world_path: Path | str) -> Path:
    return Path(world_path) / "webharbor"


def world_db_path(world_path: Path | str, site: str) -> Path:
    """Path to a world's first-class web seed for ``site``."""
    return webharbor_dir(world_path) / DB_FILE_BY_SITE[site]


def cached_seed_path(site: str) -> Path:
    return SEED_CACHE / site / DB_FILE_BY_SITE[site]


def _apply_seed_patches(site: str, db_path: Path) -> None:
    if site != "amazon" or not db_path.exists():
        return
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.DatabaseError:
        return
    try:
        conn.execute(
            """
            UPDATE products
               SET name = ?,
                   slug = ?,
                   description = ?,
                   features = ?,
                   specs = ?,
                   feature_tags = ?
             WHERE id = 185
                OR slug IN (
                    'logitech-mx-keys-wireless-keyboard-backlit',
                    'logitech-combo-touch-ipad-keyboard-case'
                )
            """,
            (
                _AMAZON_KEYBOARD_CASE_PATCH["name"],
                _AMAZON_KEYBOARD_CASE_PATCH["slug"],
                _AMAZON_KEYBOARD_CASE_PATCH["description"],
                _AMAZON_KEYBOARD_CASE_PATCH["features"],
                _AMAZON_KEYBOARD_CASE_PATCH["specs"],
                _AMAZON_KEYBOARD_CASE_PATCH["feature_tags"],
            ),
        )
        conn.commit()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()


def populate_seed_cache(sites: list[str] | None = None,
                        image: str = PINNED_UPSTREAM_IMAGE,
                        force: bool = False) -> dict[str, Path]:
    """Docker-cp each site's pristine seed DB out of the pinned image into ``SEED_CACHE``."""
    sites = sites or all_sites()
    todo = [s for s in sites if force or not cached_seed_path(s).exists()]
    out = {s: cached_seed_path(s) for s in sites}
    if not todo:
        return out

    container = f"wildawm-seedcache-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "create", "--name", container, image, "true"],
                   capture_output=True, check=True,
                   timeout=DOCKER_COMMAND_TIMEOUT_S)
    try:
        for s in todo:
            dbfile = DB_FILE_BY_SITE[s]
            dst = cached_seed_path(s)
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Copy only the canonical dbfile (skip stray *.bak siblings).
            r = subprocess.run(
                ["docker", "cp",
                 f"{container}:/opt/WebSyn/{s}/instance_seed/{dbfile}",
                 str(dst)],
                capture_output=True, text=True,
                timeout=DOCKER_COMMAND_TIMEOUT_S,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"docker cp failed for site {s}: {r.stderr.strip()}")
            _apply_seed_patches(s, dst)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True,
                       timeout=DOCKER_COMMAND_TIMEOUT_S)
    return out


def ensure_seed_cache(sites: list[str]) -> None:
    """Ensure cached pristine seeds exist for ``sites``, populating via docker if missing."""
    missing = [s for s in sites if not cached_seed_path(s).exists()]
    if missing:
        populate_seed_cache(missing)
    for s in sites:
        _apply_seed_patches(s, cached_seed_path(s))


def init_webharbor_seeds(world_path: Path | str, sites: list[str],
                         offline_ok: bool = True) -> list[str]:
    """Copy the cached pristine seed into ``webharbor/<dbfile>`` where absent; returns sites seeded."""
    world_path = Path(world_path)
    wh = webharbor_dir(world_path)
    wh.mkdir(parents=True, exist_ok=True)

    need = [s for s in sites if not world_db_path(world_path, s).exists()]
    if not need:
        return []

    cache_missing = [s for s in need if not cached_seed_path(s).exists()]
    if cache_missing:
        if offline_ok:
            ensure_seed_cache(cache_missing)
        else:
            raise RuntimeError(
                "missing pristine seed cache for "
                f"{cache_missing}; run: python -m wildawm.webharbor.lib populate")

    seeded = []
    for s in need:
        src = cached_seed_path(s)
        dst = world_db_path(world_path, s)
        shutil.copy2(src, dst)
        _apply_seed_patches(s, dst)
        seeded.append(s)
    return seeded


def ensure_all_webharbor_seeds(world_path: Path | str,
                               sites: list[str] | None = None) -> list[str]:
    """Fill missing WebHarbor site DBs with pristine seeds and apply catalog patches."""
    selected = sites or all_sites()
    seeded = init_webharbor_seeds(world_path, selected)
    for site in selected:
        _apply_seed_patches(site, world_db_path(world_path, site))
    return seeded


def _main() -> None:
    @dataclass
    class PopulateArgs:
        sites: list[str] = field(default_factory=list)
        force: bool = False

    @dataclass
    class RegistryArgs:
        pass

    command, args = parse_args_with_commands({
        "populate": PopulateArgs,
        "registry": RegistryArgs,
    })

    if command == ("populate",):
        out = populate_seed_cache(args.sites or None, force=args.force)
        for s, p in out.items():
            sz = p.stat().st_size if p.exists() else 0
            print(f"  {s:22} -> {p}  ({sz/1e6:.1f} MB)")
    elif command == ("registry",):
        print(json.dumps(site_registry(), indent=2))


if __name__ == "__main__":
    _main()
