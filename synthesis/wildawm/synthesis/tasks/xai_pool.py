#!/usr/bin/env python3
"""Workspace-scoped reservation pool: SQLite claims let parallel workers reserve slots atomically."""
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from wildawm import const_config as cfg
from wildawm.core.paths import PROJECT_ROOT
from wildawm.json_utils import loads_jsonc
from wildawm.synthesis.tasks import artifact_lint as alint
from wildawm.synthesis.tasks import tasks_index as tx

REQUIRED_INTERFACES = {"cli", "mcp", "web"}
# Long lease: a day's later slot locks wait unheartbeaten while earlier slots run.
DEFAULT_LEASE_SECONDS = 48 * 60 * 60

_INIT_LOCK = threading.RLock()
_WRITE_LOCK = threading.RLock()
_INITED_DBS: set[str] = set()


def workspace_id() -> str:
    return str(PROJECT_ROOT.resolve())


def db_path() -> Path:
    return Path(cfg.XAI_SYNTHESIS_POOL_DB)


def _now() -> float:
    return time.time()


def _utc(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _db_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def _is_locked_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database is busy" in msg


def _retry_sqlite_locked(label: str, fn):
    attempts = max(1, int(getattr(cfg, "DB_LOCK_RETRIES", 8)))
    base_s = max(0.001, int(getattr(cfg, "DB_LOCK_RETRY_BASE_MS", 100)) / 1000.0)
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not _is_locked_error(e) or i == attempts - 1:
                raise
            last = e
            time.sleep(min(2.0, base_s * (2 ** i)))
    if last:
        raise last
    return fn()


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=cfg.DB_CONNECT_TIMEOUT_S, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {cfg.DB_BUSY_TIMEOUT_MS}")
    key = _db_key(p)
    if key not in _INITED_DBS:
        with _INIT_LOCK:
            if key not in _INITED_DBS:
                _retry_sqlite_locked("init", lambda: _init_db(con))
                _INITED_DBS.add(key)
    return con


def _init_db(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS xai_synthesis_locks (
            lock_id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            owner TEXT NOT NULL,
            profile TEXT NOT NULL,
            day INTEGER NOT NULL,
            slug TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'locked',
            title TEXT NOT NULL,
            area TEXT,
            archetype TEXT,
            summary TEXT,
            difficulty TEXT,
            interfaces_json TEXT NOT NULL,
            interface_flow TEXT,
            sites_json TEXT NOT NULL,
            mcp_domains_json TEXT NOT NULL,
            image_grounding_json TEXT,
            anchors_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            lease_expires_at REAL NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_xai_synthesis_locks_workspace_expiry "
        "ON xai_synthesis_locks(workspace, lease_expires_at)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_xai_synthesis_locks_workspace_slot "
        "ON xai_synthesis_locks(workspace, profile, day, slug)"
    )


def _write_transaction(label: str, db: Path | None, fn):
    def attempt():
        with _WRITE_LOCK:
            with _connect(db) as con:
                began = False
                try:
                    con.execute("BEGIN IMMEDIATE")
                    began = True
                    result = fn(con)
                    con.execute("COMMIT")
                    return result
                except BaseException:
                    if began:
                        try:
                            con.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                    raise

    return _retry_sqlite_locked(label, attempt)


def _read_query(label: str, db: Path | None, fn):
    def attempt():
        with _connect(db) as con:
            return fn(con)

    return _retry_sqlite_locked(label, attempt)


def _json_loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return loads_jsonc(text)
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _listify(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_interface(value: str) -> str:
    v = str(value or "").strip().lower().split(":", 1)[-1].replace("_", "-")
    if v in {"mcp-app", "mcp-apps", "mcpapps", "app", "apps"}:
        return "mcp"
    if v in {"browser", "web-gui", "webgui", "website", "image"}:
        return "web"
    if v in {"shell", "terminal", "filesystem", "file"}:
        return "cli"
    return v


def normalize_interfaces(value: Any) -> list[str]:
    return sorted({i for i in (_normalize_interface(v) for v in _listify(value)) if i})


def _normalize_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _normalize_site(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    canonical = text.replace("-", "_")
    if "://" not in text and "/" not in text and "." not in text:
        return canonical
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(r"[^a-z0-9.-]+", "", host)
    host = re.sub(r"\.+", ".", host).strip(".")
    return host


_TRUTH_KEYS = ("truth", "ground_truth", "answer", "target")
_ASSET_FIELDS = (
    "asset_key", "natural_key", "slug", "handle", "permalink", "image",
    "image_url", "image_path", "hero_image", "matched_hero_image", "url",
    "path",
)
_ASSET_PATH_RE = re.compile(
    r"/(?:static|media|images?)/[A-Za-z0-9_./+%=-]*?\.(?:jpg|jpeg|png|webp|gif|avif|svg)",
    re.I,
)
_EXPLICIT_KEY_RE = re.compile(
    r"\b(?:asset[_ -]?key|natural[_ -]?key|slug|handle|permalink)\s*[:=]\s*([^,;\s]+)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s,;]+", re.I)


def _primary_site(record: dict) -> str:
    sites = _listify(record.get("sites"))
    if sites:
        return _normalize_site(sites[0])
    grounding = record.get("image_grounding")
    if isinstance(grounding, dict):
        return _normalize_site(grounding.get("site"))
    return ""


def _visual_asset_keys(record: dict) -> list[str]:
    """Return explicit image or natural-key identities without a domain ontology."""
    grounding = record.get("image_grounding")
    if not isinstance(grounding, dict):
        grounding = {}
    site = _primary_site(record)
    if not site:
        return []
    values: list[str] = []
    values.extend(str(grounding.get(key) or "") for key in _ASSET_FIELDS)
    values.extend(str(record.get(key) or "") for key in _ASSET_FIELDS)
    values.extend(str(grounding.get(key) or "") for key in _TRUTH_KEYS)
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        for path in _ASSET_PATH_RE.findall(value):
            keys.add(f"{site}:image:{path.lower()}")
    for field in ("asset_key", "natural_key", "slug", "handle", "permalink"):
        value = str(grounding.get(field) or record.get(field) or "").strip().lower()
        if value:
            keys.add(f"{site}:key:{field}:{value}")
    explicit = _listify(record.get("natural_keys"))
    surface = record.get("web_write_surface")
    if isinstance(surface, dict):
        explicit.extend(_listify(surface.get("target_natural_key")))
    anchors = " ".join(str(value or "") for value in _listify(record.get("anchors")))
    explicit.extend(match.group(1) for match in _EXPLICIT_KEY_RE.finditer(anchors))
    explicit.extend(_URL_RE.findall(anchors))
    for value in explicit:
        normalized = str(value or "").strip().lower().rstrip(".)]}")
        if normalized:
            keys.add(f"{site}:key:{normalized}")
    return sorted(keys)


def _slot_key(record: dict) -> tuple[str, int | None, str] | None:
    slug = str(record.get("slug") or "").strip()
    if not slug:
        return None
    profile = str(record.get("profile") or "").strip()
    try:
        day = int(record.get("day")) if record.get("day") is not None else None
    except (TypeError, ValueError):
        day = None
    return (profile, day, slug)


def compact_record(candidate: dict, *, profile: str | None = None, day: int | None = None,
                   slug: str | None = None, owner: str | None = None,
                   lock_id: str | None = None, status: str = "locked",
                   lease_expires_at: float | None = None) -> dict:
    image_grounding = candidate.get("image_grounding")
    if isinstance(image_grounding, dict):
        image_grounding = dict(image_grounding)
        if image_grounding.get("site"):
            image_grounding["site"] = _normalize_site(image_grounding.get("site"))
    sites = _listify(candidate.get("sites"))
    if not sites and isinstance(image_grounding, dict) and image_grounding.get("site"):
        sites = [image_grounding.get("site")]
    sites = [_normalize_site(site) for site in sites if _normalize_site(site)]
    flow = candidate.get("interface_flow") or candidate.get("flow") or ""
    anchors = candidate.get("anchors") or candidate.get("grounded_anchors") or []
    return {
        "status": status,
        "lock_id": lock_id,
        "owner": owner,
        "profile": candidate.get("profile", profile),
        "day": candidate.get("day", day),
        "slug": candidate.get("slug", slug),
        "title": candidate.get("title") or slug or "untitled",
        "area": candidate.get("area") or "?",
        "archetype": candidate.get("archetype") or "?",
        "summary": candidate.get("summary") or "",
        "difficulty": (candidate.get("difficulty") or "").strip().lower(),
        "difficulty_provisional": bool(candidate.get("difficulty_provisional", False)),
        "proposed_difficulty": str(candidate.get("proposed_difficulty") or "").strip().lower(),
        "quota_slot_difficulty": str(candidate.get("quota_slot_difficulty") or "").strip().lower(),
        "interfaces": normalize_interfaces(candidate.get("interfaces")),
        "interface_flow": flow,
        "sites": sites,
        "mcp_domains": _listify(candidate.get("mcp_domains")),
        "image_grounding": image_grounding,
        "cli_code_step": candidate.get("cli_code_step"),
        "web_action_type": candidate.get("web_action_type"),
        "web_write_surface": candidate.get("web_write_surface"),
        "why_hard": candidate.get("why_hard"),
        "anchors": anchors if isinstance(anchors, list) else [anchors],
        "natural_keys": _listify(candidate.get("natural_keys")),
        "created_at": candidate.get("created_at"),
        "updated_at": candidate.get("updated_at"),
        "lease_expires_at": lease_expires_at,
        "lease_expires_at_iso": _utc(lease_expires_at),
    }


def _row_to_record(row: sqlite3.Row) -> dict:
    payload = _json_loads(row["payload_json"], {})
    rec = compact_record(
        payload,
        profile=row["profile"],
        day=row["day"],
        slug=row["slug"],
        owner=row["owner"],
        lock_id=row["lock_id"],
        status=row["status"],
        lease_expires_at=row["lease_expires_at"],
    )
    rec.update({
        "title": row["title"],
        "area": row["area"],
        "archetype": row["archetype"],
        "summary": row["summary"],
        "difficulty": row["difficulty"],
        "interfaces": _json_loads(row["interfaces_json"], []),
        "interface_flow": row["interface_flow"],
        "sites": _json_loads(row["sites_json"], []),
        "mcp_domains": _json_loads(row["mcp_domains_json"], []),
        "image_grounding": _json_loads(row["image_grounding_json"], None),
        "anchors": _json_loads(row["anchors_json"], []),
        "created_at": row["created_at"],
        "created_at_iso": _utc(row["created_at"]),
        "updated_at": row["updated_at"],
        "updated_at_iso": _utc(row["updated_at"]),
    })
    return rec


def _accepted_records() -> list[dict]:
    return tx.gather_global_priors()


def _purge_expired(con: sqlite3.Connection, *, workspace: str, now: float) -> int:
    cur = con.execute(
        "DELETE FROM xai_synthesis_locks WHERE workspace = ? AND lease_expires_at <= ?",
        (workspace, now),
    )
    return int(cur.rowcount or 0)


def _locked_records(con: sqlite3.Connection, *, workspace: str, now: float) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM xai_synthesis_locks WHERE workspace = ? AND lease_expires_at > ? "
        "ORDER BY created_at",
        (workspace, now),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def list_pool(*, db: Path | None = None, purge_expired: bool = True) -> dict:
    now = _now()
    ws = workspace_id()
    if purge_expired:
        def op(con):
            expired = _purge_expired(con, workspace=ws, now=now)
            locked = _locked_records(con, workspace=ws, now=now)
            return expired, locked

        expired, locked = _write_transaction("list_pool", db, op)
    else:
        expired = 0
        locked = _read_query(
            "list_pool_read",
            db,
            lambda con: _locked_records(con, workspace=ws, now=now),
        )
    accepted = _accepted_records()
    return {
        "workspace": ws,
        "db_path": str(Path(db or db_path())),
        "accepted": accepted,
        "locked": locked,
        "records": accepted + locked,
        "counts": {
            "accepted": len(accepted),
            "locked": len(locked),
            "expired_reaped": expired,
        },
    }


def _candidate_contract_reasons(record: dict) -> list[str]:
    reasons = list(alint.sketch_xai_contract_problems(record))
    missing = sorted(REQUIRED_INTERFACES - set(record.get("interfaces") or []))
    if missing:
        reasons.append(f"missing required interfaces: {', '.join(missing)}")
    reasons.extend(alint.mcp_domain_problems(
        record.get("mcp_domains"), where="candidate"))
    flow = str(record.get("interface_flow") or "")
    if not REQUIRED_INTERFACES.issubset(
            alint.flow_interfaces(flow, mcp_domains=record.get("mcp_domains"))):
        reasons.append("interface_flow must name cli, mcp, and web")
    return reasons


def _exact_duplicate(candidate: dict, pool: list[dict]) -> str | None:
    """Detect exact task and explicit asset reuse without semantic buckets."""
    title = _normalize_text(candidate.get("title"))
    summary = _normalize_text(candidate.get("summary"))
    assets = set(_visual_asset_keys(candidate))
    for record in pool:
        prior_title = _normalize_text(record.get("title"))
        prior_summary = _normalize_text(record.get("summary"))
        if title and title == prior_title:
            return f"same title as {record.get('status', 'pool')} task {record.get('title')!r}"
        if title and summary and (title, summary) == (prior_title, prior_summary):
            return f"exact task duplicate of {record.get('status', 'pool')} task {record.get('title')!r}"
        overlap = assets & set(_visual_asset_keys(record))
        if overlap:
            return (
                f"same explicit image/natural-key asset {sorted(overlap)[0]!r} as "
                f"{record.get('status', 'pool')} task {record.get('title')!r}"
            )
    return None


def reserve_many(candidates: list[dict], k: int, *, owner: str, profile: str,
                 day: int, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 db: Path | None = None) -> dict:
    ws = workspace_id()
    accepted = _accepted_records()
    k = max(0, int(k))
    normalized = [
        compact_record(c, profile=profile, day=day, slug=c.get("slug"))
        for c in candidates
    ]

    def op(con):
        now = _now()
        rejected: list[dict] = []
        locked: list[dict] = []
        expired = _purge_expired(con, workspace=ws, now=now)
        existing_locks = _locked_records(con, workspace=ws, now=now)
        pool = accepted + existing_locks
        eligible: list[tuple[int, dict]] = []
        for idx, rec in enumerate(normalized):
            reasons = _candidate_contract_reasons(rec)
            duplicate = _exact_duplicate(rec, pool)
            if duplicate:
                reasons.append(duplicate)
            if rec.get("difficulty") not in {"medium", "hard"}:
                reasons.append("difficulty must be medium or hard")
            if reasons:
                rejected.append({"index": idx, "title": rec.get("title"), "reasons": reasons})
                continue
            eligible.append((idx, rec))
        selected_pool = list(pool)
        claimed_slots = {k for k in (_slot_key(r) for r in existing_locks) if k}
        for idx, rec in eligible:
            if len(locked) >= k:
                rejected.append({
                    "index": idx,
                    "title": rec.get("title"),
                    "reasons": ["not reserved: reserve_many k reached"],
                })
                continue
            duplicate = _exact_duplicate(rec, selected_pool)
            if duplicate:
                rejected.append({"index": idx, "title": rec.get("title"),
                                 "reasons": [duplicate]})
                continue
            slot_key = _slot_key(rec)
            if slot_key and slot_key in claimed_slots:
                rejected.append({
                    "index": idx,
                    "title": rec.get("title"),
                    "reasons": [f"slot already reserved in this workspace: {slot_key[0]}/day{slot_key[1]}/{slot_key[2]}"],
                })
                continue
            lock_id = uuid.uuid4().hex
            slug = rec.get("slug") or f"xai-{lock_id[:8]}"
            lease_expires = now + max(60, int(lease_seconds))
            rec = {**rec, "lock_id": lock_id, "owner": owner, "slug": slug,
                   "status": "locked", "lease_expires_at": lease_expires,
                   "created_at": now, "updated_at": now}
            payload = {k2: v for k2, v in rec.items()
                       if k2 not in {"created_at", "updated_at", "lease_expires_at", "lease_expires_at_iso"}}
            con.execute(
                """
                INSERT INTO xai_synthesis_locks (
                    lock_id, workspace, owner, profile, day, slug, status, title,
                    area, archetype, summary, difficulty, interfaces_json,
                    interface_flow, sites_json, mcp_domains_json,
                    image_grounding_json, anchors_json, payload_json,
                    created_at, updated_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lock_id, ws, owner, profile, int(day), slug, "locked",
                    rec.get("title") or slug, rec.get("area"), rec.get("archetype"),
                    rec.get("summary"), rec.get("difficulty"),
                    _json_dumps(rec.get("interfaces") or []),
                    rec.get("interface_flow"),
                    _json_dumps(rec.get("sites") or []),
                    _json_dumps(rec.get("mcp_domains") or []),
                    _json_dumps(rec.get("image_grounding")),
                    _json_dumps(rec.get("anchors") or []),
                    _json_dumps(payload),
                    now, now, lease_expires,
                ),
            )
            locked.append(rec)
            selected_pool.append(rec)
            if slot_key:
                claimed_slots.add(slot_key)
        return {
            "workspace": ws,
            "locked": locked,
            "rejected": sorted(rejected, key=lambda r: r["index"]),
            "counts": {
                "accepted": len(accepted),
                "locked_existing": len(existing_locks),
                "locked_new": len(locked),
                "expired_reaped": expired,
            },
        }

    return _write_transaction("reserve_many", db, op)


def heartbeat(lock_id: str, *, owner: str | None = None,
              lease_seconds: int = DEFAULT_LEASE_SECONDS,
              db: Path | None = None) -> dict:
    ws = workspace_id()

    def op(con):
        now = _now()
        lease_expires = now + max(60, int(lease_seconds))
        row = con.execute(
            "SELECT * FROM xai_synthesis_locks WHERE workspace = ? AND lock_id = ?",
            (ws, lock_id),
        ).fetchone()
        if row is None or row["lease_expires_at"] <= now:
            return {"ok": False, "reason": "lock missing or expired", "lock_id": lock_id}
        if owner and row["owner"] != owner:
            return {"ok": False, "reason": "owner mismatch", "lock_id": lock_id}
        con.execute(
            "UPDATE xai_synthesis_locks SET updated_at = ?, lease_expires_at = ? "
            "WHERE workspace = ? AND lock_id = ?",
            (now, lease_expires, ws, lock_id),
        )
        return {"ok": True, "lock_id": lock_id, "lease_expires_at": lease_expires,
                "lease_expires_at_iso": _utc(lease_expires)}

    return _write_transaction("heartbeat", db, op)


def release(lock_id: str, *, owner: str | None = None, reason: str = "",
            db: Path | None = None) -> dict:
    ws = workspace_id()

    def op(con):
        row = con.execute(
            "SELECT owner FROM xai_synthesis_locks WHERE workspace = ? AND lock_id = ?",
            (ws, lock_id),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "lock missing", "lock_id": lock_id}
        if owner and row["owner"] != owner:
            return {"ok": False, "reason": "owner mismatch", "lock_id": lock_id}
        con.execute(
            "DELETE FROM xai_synthesis_locks WHERE workspace = ? AND lock_id = ?",
            (ws, lock_id),
        )
        return {"ok": True, "lock_id": lock_id, "reason": reason}

    return _write_transaction("release", db, op)


def finalize(lock_id: str, *, owner: str | None = None, verdict: str = "",
             db: Path | None = None) -> dict:
    # Accepted tasks are rescanned from disk; finalization just removes the pending claim.
    return release(lock_id, owner=owner, reason=f"finalize:{verdict}", db=db)


def _http_retryable(code: int, detail: str) -> bool:
    if code in {408, 409, 423, 429, 500, 502, 503, 504}:
        return True
    if code == 400 and "locked" in detail.lower():
        return True
    return False


def _request(method: str, path: str, body: dict | None = None, *,
             timeout: int = cfg.HTTP_MEDIUM_TIMEOUT_S,
             retries: int = 3) -> dict:
    url = cfg.SYNTHESIS_DASHBOARD.rstrip("/") + path
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    attempts = max(1, int(retries))
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if i < attempts - 1 and _http_retryable(e.code, detail):
                time.sleep(min(2.0, 0.25 * (2 ** i)))
                continue
            raise RuntimeError(f"{method} {path} failed with HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            if i < attempts - 1:
                time.sleep(min(2.0, 0.25 * (2 ** i)))
                continue
            raise RuntimeError(f"{method} {path} failed: {e}") from e
    raise RuntimeError(f"{method} {path} failed after {attempts} attempts")


def reserve_many_remote(candidates: list[dict], k: int, *, owner: str,
                        profile: str, day: int,
                        lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    return _request(
        "POST",
        "/api/xai-synthesis-pool/reserve-many",
        {
            "candidates": candidates,
            "k": k,
            "owner": owner,
            "profile": profile,
            "day": day,
            "lease_seconds": lease_seconds,
        },
        timeout=cfg.HTTP_MEDIUM_TIMEOUT_S,
    )


def heartbeat_remote(lock_id: str, *, owner: str,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    return _request(
        "POST",
        f"/api/xai-synthesis-pool/{lock_id}/heartbeat",
        {"owner": owner, "lease_seconds": lease_seconds},
        timeout=cfg.HTTP_SHORT_TIMEOUT_S,
    )


def finalize_remote(lock_id: str, *, owner: str, verdict: str) -> dict:
    return _request(
        "POST",
        f"/api/xai-synthesis-pool/{lock_id}/finalize",
        {"owner": owner, "verdict": verdict},
        timeout=cfg.HTTP_SHORT_TIMEOUT_S,
    )


def release_remote(lock_id: str, *, owner: str, reason: str = "") -> dict:
    return _request(
        "DELETE",
        f"/api/xai-synthesis-pool/{lock_id}",
        {"owner": owner, "reason": reason},
        timeout=cfg.HTTP_SHORT_TIMEOUT_S,
    )


@contextmanager
def heartbeat_loop(lock_id: str | None, *, owner: str,
                   interval_s: int = 60,
                   lease_seconds: int = DEFAULT_LEASE_SECONDS):
    if not lock_id:
        yield
        return

    stop = threading.Event()
    errors: list[str] = []

    def _run() -> None:
        while not stop.wait(interval_s):
            try:
                heartbeat_remote(lock_id, owner=owner, lease_seconds=lease_seconds)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

    thread = threading.Thread(target=_run, name=f"xai-lock-heartbeat-{lock_id[:8]}", daemon=True)
    thread.start()
    try:
        heartbeat_remote(lock_id, owner=owner, lease_seconds=lease_seconds)
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
