#!/usr/bin/env python3
"""Freeze integrity: stamp accepted tasks with a digest of the world they were validated on."""
import hashlib
import json
import sqlite3
from pathlib import Path

from wildawm.json_utils import load_jsonc


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _db_digest(db: Path) -> str:
    """Schema and row-count digest per table; ignores volatile cell content."""
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        h = hashlib.sha256()
        for (name,) in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            schema = c.execute("SELECT sql FROM sqlite_master WHERE name=?",
                               (name,)).fetchone()
            n = c.execute(f"SELECT count(*) FROM {_quote_ident(name)}").fetchone()[0]
            h.update(f"{name}:{schema[0]}:{n}\n".encode())
        c.close()
        return h.hexdigest()[:16]
    except sqlite3.DatabaseError as e:
        return f"err:{e}"


def snapshot_digest(snapshot_dir: Path) -> dict:
    """Digest of a day-N snapshot: DB structure, workspace file paths and overlay paths."""
    snapshot_dir = Path(snapshot_dir)
    dbs = {}
    for sub in ("data", "webharbor", "overlay"):
        d = snapshot_dir / sub
        if d.is_dir():
            for db in sorted(d.glob("*.db")):
                dbs[f"{sub}/{db.name}"] = _db_digest(db)
    ws = snapshot_dir / "workspace"
    paths = sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                   if p.is_file() and "/.git/" not in str(p)) if ws.is_dir() else []
    ws_hash = hashlib.sha256("\n".join(paths).encode()).hexdigest()[:16]
    od = snapshot_dir / "overlay"
    opaths = sorted(str(p.relative_to(od)) for p in od.rglob("*")
                    if p.is_file() and "/.git/" not in str(p)) if od.is_dir() else []
    ohash = hashlib.sha256("\n".join(opaths).encode()).hexdigest()[:16]
    return {"dbs": dbs, "workspace_paths": ws_hash, "n_files": len(paths),
            "overlay_paths": ohash, "overlay_n_files": len(opaths)}


def stamp(meta_path: Path, snapshot_dir: Path) -> dict:
    meta = load_jsonc(meta_path) if Path(meta_path).exists() else {}
    meta["world_digest"] = snapshot_digest(snapshot_dir)
    Path(meta_path).write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    return meta["world_digest"]
