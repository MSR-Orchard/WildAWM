"""Deterministic primitives shared by every world-synthesis stage (no LLM or Docker calls)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

import bcrypt

from wildawm import const_config as cfg
from wildawm.webharbor import lib as wh_lib

WORLD_SUBDIRS = ("data", "workspace", "webharbor", "overlay")


# Name the other valid fix: a read-only day should drop the entry, not force a write.
_DECLARATION_FIX_HINT = (" — if this day's actions did not actually add rows here (it only read or "
                         "updated existing ones, or the rows were already present), the fix is to "
                         "REMOVE this entry from produces.json, not to force a write. Declare what "
                         "the day did; use db_updates for in-place changes.")


def _catalog_hint(site: str, table: str, n_now: int) -> str:
    """Flag a table that did not gain rows but still holds exactly its pristine seed catalog."""
    try:
        seed = wh_lib.cached_seed_path(site)
    except (KeyError, AttributeError):
        return ""
    if not seed.exists() or _row_count(seed, table) != n_now:
        return ""
    return (f" — this table still holds exactly its pristine seed contents ({n_now} rows): it is "
            f"shared {site} catalog, not persona state. Personas reference catalog rows; they never "
            f"add to them. Declare the table your action actually wrote to instead.")


def resolve_site(name: str) -> str:
    """Canonical site id for ``name``, which may be the id or the site's DB filename stem."""
    sites = wh_lib.all_sites()
    if name in sites:
        return name
    stem = str(name)[:-3] if str(name).endswith(".db") else str(name)
    for s in sites:
        if wh_lib.dbfile_for_site(s)[:-3] == stem:
            return s
    return name


def empty_world(dest: Path) -> None:
    for sub in WORLD_SUBDIRS:
        (dest / sub).mkdir(parents=True, exist_ok=True)


def stage_world(prev_snap: Path | None, dest: Path) -> Path:
    """Lay down a writable throwaway world at ``dest`` from ``prev_snap`` or a fresh baseline."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if prev_snap and prev_snap.is_dir():
        for sub in WORLD_SUBDIRS:
            s = prev_snap / sub
            if s.is_dir():
                shutil.copytree(s, dest / sub, symlinks=True)
    else:
        empty_world(dest)
        # Day 0 starts from the full pinned site catalog; delta.sh only adds persona rows.
        wh_lib.ensure_all_webharbor_seeds(dest)
    return dest


# Volatile subtrees never hashed into the logical digest.
_EXCLUDE_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _dump_digest(db: Path) -> str:
    """Content digest of a SQLite DB from its sorted ``.dump`` lines."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            lines = list(con.iterdump())
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return f"err:{e}"
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _is_zipfile(p: Path) -> bool:
    return p.suffix.lower() == ".zip" and zipfile.is_zipfile(p)


def _zip_member_digest(p: Path) -> str:
    """Hash a zip by member name, size, CRC and content, ignoring member mtimes."""
    h = hashlib.sha256()
    with zipfile.ZipFile(p) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            h.update(f"{info.filename}|{info.file_size}|{info.CRC}\n".encode("utf-8"))
            if not info.is_dir():
                with z.open(info) as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
    return h.hexdigest()


def _tree_content_digest(root: Path) -> str:
    """Recursive content hash of a directory, excluding VCS internals and bytecode caches."""
    root = Path(root)
    h = hashlib.sha256()
    if not root.exists():
        h.update(b"<missing>")
        return h.hexdigest()
    entries: list[tuple[str, Path]] = []
    for p in root.rglob("*"):
        rel_parts = p.relative_to(root).parts
        if any(part in _EXCLUDE_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if p.is_dir():
            continue
        if p.suffix in _EXCLUDE_SUFFIXES:
            continue
        entries.append((p.relative_to(root).as_posix(), p))
    for rel, p in sorted(entries, key=lambda x: x[0]):
        if p.is_symlink():
            h.update(f"L {rel} {os.readlink(p)}\n".encode("utf-8"))
            continue
        h.update(f"F {rel} {p.stat().st_size}\n".encode("utf-8"))
        if _is_zipfile(p):
            h.update(b"zip:")
            h.update(_zip_member_digest(p).encode("utf-8") + b"\n")
        else:
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            h.update(b"\n")
    return h.hexdigest()


def world_hash(snapshot_dir: Path | str) -> str:
    """Logical, deterministic digest of a day-N snapshot, including ``overlay/``."""
    snapshot_dir = Path(snapshot_dir)
    h = hashlib.sha256()
    for sub in ("data", "webharbor"):
        d = snapshot_dir / sub
        if not d.is_dir():
            continue
        for db in sorted(d.glob("*.db")):
            h.update(f"db:{sub}/{db.name}\0".encode("utf-8"))
            h.update(_dump_digest(db).encode("utf-8") + b"\0")
    h.update(b"workspace\0")
    h.update(_tree_content_digest(snapshot_dir / "workspace").encode("utf-8") + b"\0")
    h.update(b"overlay\0")
    h.update(_tree_content_digest(snapshot_dir / "overlay").encode("utf-8") + b"\0")
    return h.hexdigest()


# accounts.json: one {site, username, password, password_bcrypt, email} record per site.


def _bcrypt_salt() -> bytes:
    return b"$2b$" + str(cfg.WORLD_SYN_BCRYPT_ROUNDS).encode() + b"$" + cfg.WORLD_SYN_BCRYPT_SALT.encode("ascii")


def hash_password(password: str) -> str:
    """Deterministic bcrypt ($2b$) hash of a password using the fixed project salt."""
    return bcrypt.hashpw(password.encode("utf-8"), _bcrypt_salt()).decode("ascii")


def verify_password(password: str, pwhash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pwhash.encode("ascii"))
    except ValueError:
        return False


def accounts_path(profile_dir: Path | str) -> Path:
    return Path(profile_dir) / "accounts.json"


def load_accounts(profile_dir: Path | str) -> list[dict]:
    p = accounts_path(profile_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def write_accounts(profile_dir: Path | str, accounts: list[dict]) -> Path:
    p = accounts_path(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(accounts, indent=2, ensure_ascii=False) + "\n")
    return p


def account_for(accounts: list[dict], site: str) -> dict | None:
    # Either side may name the site by its DB filename rather than its id.
    want = resolve_site(site)
    for a in accounts:
        if resolve_site(a.get("site") or "") == want:
            return a
    return None


def make_account(site: str, *, username: str, password: str, email: str = "") -> dict:
    """Build a well-formed account record with a deterministic password hash."""
    return {
        "site": site,
        "username": username,
        "password": password,
        "password_bcrypt": hash_password(password),
        "email": email or f"{username}@{site}.local",
    }


# produces.json: db_/web_ writes and updates, web_accounts, files, cross_refs, overlay.

_DEFAULT_FILE_MIN_BYTES = 1
_EMPTY_PLACEHOLDER_FILENAMES = {".gitkeep", ".keep", "__init__.py"}


def _allows_empty_declared_file(rel: str) -> bool:
    """Allow files whose emptiness is the convention, without weakening real artifacts."""
    return Path(rel).name in _EMPTY_PLACEHOLDER_FILENAMES


def workspace_path(ws: Path, rel: str) -> Path:
    """Resolve a declared workspace-relative path, tolerating a redundant `workspace/` prefix."""
    fp = ws / rel
    if not fp.exists() and str(rel).startswith("workspace/"):
        alt = ws / str(rel)[len("workspace/"):]
        if alt.exists():
            return alt
    return fp


def _row_count(db: Path, table: str) -> int:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if cur.fetchone() is None:
                return -1  # table absent
            return int(con.execute(f"SELECT count(*) FROM \"{table}\"").fetchone()[0])
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return -2


def _table_digest(db: Path, table: str) -> str:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if cur.fetchone() is None:
                return "missing"
            cols = [r[1] for r in con.execute(f"PRAGMA table_info(\"{table}\")").fetchall()]
            rows = con.execute(f"SELECT * FROM \"{table}\"").fetchall()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return f"err:{e}"
    h = hashlib.sha256()
    h.update(repr(cols).encode("utf-8") + b"\n")
    for row in sorted(repr(tuple(r)) for r in rows):
        h.update(row.encode("utf-8") + b"\n")
    return h.hexdigest()


def load_produces(produces_path: Path | str) -> dict:
    p = Path(produces_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def check_produces(
    produces: dict,
    snapshot_n: Path | str,
    snapshot_prev: Path | str | None,
    *,
    day_dir: Path | str | None = None,
    file_min_bytes: int = _DEFAULT_FILE_MIN_BYTES,
) -> tuple[bool, list[str]]:
    """Check a produces.json declaration against the day-N snapshot; returns (ok, problems)."""
    snapshot_n = Path(snapshot_n)
    snapshot_prev = Path(snapshot_prev) if snapshot_prev else None
    problems: list[str] = []

    db_writes = produces.get("db_writes") or {}
    if not isinstance(db_writes, dict):
        problems.append("db_writes must be an object {app: [tables]}")
        db_writes = {}
    for app, tables in db_writes.items():
        db = snapshot_n / "data" / f"{app}.db"
        if not db.exists():
            problems.append(f"db_writes: data/{app}.db missing")
            continue
        for table in (tables or []):
            n_now = _row_count(db, table)
            if n_now < 0:
                problems.append(f"db_writes: {app}.{table} table missing")
                continue
            prev_db = snapshot_prev / "data" / f"{app}.db" if snapshot_prev else None
            if prev_db and prev_db.exists():
                n_prev = _row_count(prev_db, table)
                if n_prev >= 0 and n_now <= n_prev:
                    problems.append(
                        f"db_writes: {app}.{table} did not gain rows (prev={n_prev} now={n_now})"
                        + _DECLARATION_FIX_HINT)
            elif n_now <= 0:
                problems.append(f"db_writes: {app}.{table} has no rows (new table expected ≥1)")

    db_updates = produces.get("db_updates") or {}
    if not isinstance(db_updates, dict):
        problems.append("db_updates must be an object {app: [tables]}")
        db_updates = {}
    for app, tables in db_updates.items():
        db = snapshot_n / "data" / f"{app}.db"
        if not db.exists():
            problems.append(f"db_updates: data/{app}.db missing")
            continue
        for table in (tables or []):
            n_now = _row_count(db, table)
            if n_now < 0:
                problems.append(f"db_updates: {app}.{table} table missing")
                continue
            prev_db = snapshot_prev / "data" / f"{app}.db" if snapshot_prev else None
            if prev_db and prev_db.exists():
                n_prev = _row_count(prev_db, table)
                if n_prev < 0:
                    continue
                before = _table_digest(prev_db, table)
                after = _table_digest(db, table)
                if before == after:
                    problems.append(
                        f"db_updates: {app}.{table} did not change content")
            elif n_now <= 0:
                problems.append(f"db_updates: {app}.{table} has no rows (new table expected ≥1)")

    # Site tables live under webharbor/ and some filenames differ from their site ids.
    web_writes = produces.get("web_writes") or {}
    if not isinstance(web_writes, dict):
        problems.append("web_writes must be an object {site: [tables]}")
        web_writes = {}
    for site, tables in web_writes.items():
        try:
            dbfile = wh_lib.dbfile_for_site(resolve_site(site))
        except KeyError:
            problems.append(
                f"web_writes: unknown site {site!r} — use a canonical site id "
                f"(one of: {', '.join(wh_lib.all_sites())})")
            continue
        db = snapshot_n / "webharbor" / dbfile
        if not db.exists():
            problems.append(f"web_writes: webharbor/{dbfile} missing")
            continue
        for table in (tables or []):
            n_now = _row_count(db, table)
            if n_now < 0:
                problems.append(f"web_writes: {site}.{table} table missing")
                continue
            prev_db = snapshot_prev / "webharbor" / dbfile if snapshot_prev else None
            if prev_db and prev_db.exists():
                n_prev = _row_count(prev_db, table)
                if n_prev >= 0 and n_now <= n_prev:
                    problems.append(
                        f"web_writes: {site}.{table} did not gain rows (prev={n_prev} now={n_now})"
                        + _catalog_hint(resolve_site(site), table, n_now) + _DECLARATION_FIX_HINT)
            elif n_now <= 0:
                problems.append(f"web_writes: {site}.{table} has no rows (new table expected ≥1)")

    web_updates = produces.get("web_updates") or {}
    if not isinstance(web_updates, dict):
        problems.append("web_updates must be an object {site: [tables]}")
        web_updates = {}
    for site, tables in web_updates.items():
        try:
            dbfile = wh_lib.dbfile_for_site(resolve_site(site))
        except KeyError:
            problems.append(
                f"web_updates: unknown site {site!r} — use a canonical site id "
                f"(one of: {', '.join(wh_lib.all_sites())})")
            continue
        db = snapshot_n / "webharbor" / dbfile
        if not db.exists():
            problems.append(f"web_updates: webharbor/{dbfile} missing")
            continue
        for table in (tables or []):
            n_now = _row_count(db, table)
            if n_now < 0:
                problems.append(f"web_updates: {site}.{table} table missing")
                continue
            prev_db = snapshot_prev / "webharbor" / dbfile if snapshot_prev else None
            if prev_db and prev_db.exists():
                n_prev = _row_count(prev_db, table)
                if n_prev < 0:
                    continue
                before = _table_digest(prev_db, table)
                after = _table_digest(db, table)
                if before == after:
                    problems.append(f"web_updates: {site}.{table} did not change content")
            elif n_now <= 0:
                problems.append(f"web_updates: {site}.{table} has no rows (new table expected ≥1)")

    ws = snapshot_n / "workspace"
    for rel in (produces.get("files") or []):
        fp = workspace_path(ws, rel)
        if not fp.exists():
            problems.append(f"files: workspace/{rel} missing")
        elif (fp.is_file() and fp.stat().st_size < file_min_bytes
              and not _allows_empty_declared_file(str(rel))):
            problems.append(f"files: workspace/{rel} empty (<{file_min_bytes}B)")

    for xr in (produces.get("cross_refs") or []):
        # Coerce malformed cross_refs so the gate rejects them instead of crashing the stage.
        xr = xr if isinstance(xr, dict) else {}
        table = xr.get("table")
        rel = xr.get("file")
        if not table or not rel:
            problems.append(f"cross_refs: malformed {xr}")
            continue
        app, _, tbl = table.partition(".")
        db = snapshot_n / "data" / f"{app}.db"
        fp = workspace_path(ws, rel)
        if db.exists() and _row_count(db, tbl) >= 0 and not fp.exists():
            problems.append(f"cross_refs: {table} rows reference missing workspace/{rel}")

    overlay = produces.get("overlay") or {}
    if day_dir is not None:
        ov = Path(day_dir) / "overlay"
        for app in (overlay.get("new_apps") or []):
            app_dir = ov / "apps" / app
            if not (app_dir / "run.py").exists() and not (app_dir / "handlers.py").exists():
                problems.append(f"overlay.new_apps: overlay/apps/{app}/ has no run.py/handlers.py")
            if not (app_dir / "models.py").exists():
                problems.append(f"overlay.new_apps: overlay/apps/{app}/models.py missing")
        for patch in (overlay.get("code_patches") or []):
            kind, _, tgt = patch.partition(":")
            if kind == "mcp":
                if not (ov / "apps" / tgt).is_dir():
                    problems.append(f"overlay.code_patches: mcp:{tgt} not in overlay/apps/")
            elif kind == "webharbor":
                if not (ov / "webharbor" / tgt).is_dir():
                    problems.append(f"overlay.code_patches: webharbor:{tgt} not in overlay/webharbor/")
            else:
                problems.append(f"overlay.code_patches: unknown patch kind {patch!r}")

    return (len(problems) == 0, problems)


# Replay lint: no network, wall clock, host paths or master mutation; errs on the strict side.

_PY_FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "requests", "httpx", "docker", "git"}
_PY_FORBIDDEN_STRING_FRAGMENTS = (
    "worlds/_master", "/worlds/_master", "worlds/_masters", "/worlds/_masters",
    "tasks_xai/", "/home/", "git commit", "git push", "git pull", "git clone",
    "git fetch", "docker build", "docker commit", "docker volume",
)

# Frozen GIT_*_DATE env is allowed; a wall-clock `date` is not.
_SHELL_FORBIDDEN_PATTERNS = [
    (re.compile(r"(^|[\s;|&])date\b(?![A-Za-z_])"), "bare `date` command (wall-clock) — use frozen literals"),
    (re.compile(r"\$\(\s*date\b"), "$(date ...) (wall-clock)"),
    (re.compile(r"`\s*date\b"), "backtick `date` (wall-clock)"),
    (re.compile(r"\bcurl\b"), "live network (curl)"),
    (re.compile(r"\bwget\b"), "live network (wget)"),
    (re.compile(r"\bnc\b"), "live network (nc)"),
    (re.compile(r"\bssh\b"), "live network (ssh)"),
    (re.compile(r"\bscp\b"), "live network (scp)"),
    (re.compile(r"\bgit\s+clone\b"), "git clone (live network)"),
    (re.compile(r"\bgit\s+pull\b"), "git pull (live network)"),
    (re.compile(r"\bgit\s+push\b"), "git push (live network)"),
    (re.compile(r"\bgit\s+fetch\b"), "git fetch (live network)"),
    # Say what to do instead, or the author retries `git commit` with dates set.
    (re.compile(r"\bgit\s+commit\b"), "git commit — the replay must not invoke git. Build the history "
                                      "as git objects instead: write zlib-compressed blob/tree/commit "
                                      "objects under .git/objects and point .git/refs/heads/<branch> at "
                                      "the commit sha, timestamping from $GIT_AUTHOR_DATE/$GIT_COMMITTER_DATE"),
    (re.compile(r"/home/"), "/home/ host path"),
    (re.compile(r"worlds/_master"), "worlds/_master master path"),
    (re.compile(r"tasks_xai/"), "tasks_xai/ path"),
]


def _python_safety_errors(script: Path) -> list[str]:
    if not script.exists():
        return []
    try:
        tree = ast.parse(script.read_text(errors="replace"))
    except SyntaxError as e:
        return [f"{script.name}: syntax error: {e}"]
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in _PY_FORBIDDEN_IMPORTS:
                    errors.append(f"{script.name}: forbidden import `{alias.name}`")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".", 1)[0] in _PY_FORBIDDEN_IMPORTS:
                errors.append(f"{script.name}: forbidden import `{node.module}`")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for frag in _PY_FORBIDDEN_STRING_FRAGMENTS:
                if frag in node.value:
                    errors.append(f"{script.name}: forbidden string fragment `{frag}`")
    return errors


def _shell_safety_errors(text: str, name: str) -> list[str]:
    executable_text = "\n".join(
        "" if line.lstrip().startswith("#") else line
        for line in text.splitlines()
    )
    errors: list[str] = []
    for pat, why in _SHELL_FORBIDDEN_PATTERNS:
        if pat.search(executable_text):
            errors.append(f"{name}: {why}")
    return errors


def lint_delta(day_dir: Path | str) -> list[str]:
    """Lint a day's delta.sh and Python sub-scripts for non-hermetic replay; returns problems."""
    day_dir = Path(day_dir)
    problems: list[str] = []
    delta = day_dir / "delta.sh"
    if not delta.exists():
        return [f"{day_dir.name}: delta.sh missing"]
    problems += _shell_safety_errors(delta.read_text(errors="replace"), "delta.sh")
    for sp in sorted(day_dir.glob("*.py")):
        problems += _python_safety_errors(sp)
    return problems


# days.json maps {day: "YYYY-MM-DD"} for the frozen clock and day list.


def days_path(master_dir: Path | str) -> Path:
    return Path(master_dir) / "days.json"


def read_days(master_dir: Path | str) -> dict[int, str]:
    p = days_path(master_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except ValueError:
        return {}
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return out


def write_days(master_dir: Path | str, days: dict[int, str]) -> Path:
    p = days_path(master_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    serial = {str(int(k)): str(v) for k, v in sorted(days.items())}
    p.write_text(json.dumps(serial, indent=2, ensure_ascii=False) + "\n")
    return p


def profile_slug_seed(persona_text: str) -> str:
    """Deterministic 6-char slug prefix from the sha256 of the persona text."""
    return hashlib.sha256(persona_text.encode("utf-8")).hexdigest()[:6]


def normalize_name(raw: str) -> str:
    """Filesystem-safe profile name token: lowercase alnum + dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return s or "persona"
