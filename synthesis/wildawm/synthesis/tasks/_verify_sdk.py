#!/usr/bin/env python3
"""Verification SDK imported by generated verifier scripts (stdlib only; ships beside them)."""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# site -> served sqlite filename (mirrors webharbor_lib.DB_FILE_BY_SITE)
_WEB_DBFILE = {
    "allrecipes": "allrecipes.db", "amazon": "amazon_store.db",
    "apple": "apple_store.db", "arxiv": "arxiv.db", "bbc_news": "bbc_news.db",
    "booking": "booking.db", "github": "github_mirror.db",
    "google_flights": "google_flights.db", "google_map": "gmaps.db",
    "google_search": "google_search.db", "huggingface": "hf.db",
    "wolfram_alpha": "wolfram_alpha.db", "cambridge_dictionary": "cambridge.db",
    "coursera": "coursera.db", "espn": "espn.db",
}


def parse_common_args(desc: str = "XAI verify check") -> argparse.Namespace:
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--workspace", required=True)
    p.add_argument("--data-dir", required=True, help="AFTER state: instance data/")
    p.add_argument("--baseline-dir", required=True,
                   help="clean day-N snapshot root (workspace/ data/ webharbor/)")
    p.add_argument("--webharbor-dir", default=None,
                   help="AFTER state: served-webharbor snapshot dir")
    p.add_argument("--baseline-webharbor-dir", default=None,
                   help="baseline webharbor dir (default <baseline-dir>/webharbor)")
    args, _unknown = p.parse_known_args()
    return args


def norm(s: str) -> str:
    """Aggressive normalization for matching free text."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _quote_ident(value: str) -> str:
    """Quote a schema-provided SQLite identifier."""
    return '"' + str(value).replace('"', '""') + '"'


def mentions(text: str, terms, mode: str = "all") -> bool:
    """Case/punctuation-insensitive containment; a nested list in ``terms`` means any-of synonyms."""
    t = norm(text)

    def hit(term) -> bool:
        if isinstance(term, (list, tuple)):
            return any(hit(x) for x in term)
        return norm(str(term)) in t

    items = terms if isinstance(terms, (list, tuple)) else [terms]
    return all(hit(x) for x in items) if mode == "all" else any(hit(x) for x in items)


def same_date(value: str, ymd: str) -> bool:
    """True iff a stored datetime string falls on YYYY-MM-DD ('T'/' ' separator or bare date)."""
    return (value or "").strip().replace("T", " ").startswith(ymd)


class DBError(Exception):
    """World/infra problem (missing or corrupt DB) — routes to world_imperfect."""


class _Side:
    """One world state (after OR baseline): filesystem + MCP DBs + web DBs."""

    def __init__(self, workspace, data_dir, webharbor_dir=None):
        self.workspace = Path(workspace) if workspace else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.webharbor_dir = Path(webharbor_dir) if webharbor_dir else None

    def _db(self, path) -> Path:
        if path is None:
            raise DBError("db path unset")
        p = Path(path)
        if not p.exists():
            raise DBError(f"missing db: {p}")
        if p.stat().st_size == 0:
            raise DBError(f"empty db file: {p}")
        return p

    def q(self, app: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._run(self._db(self.data_dir / f"{app}.db"), sql, params)

    def web(self, site: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.webharbor_dir:
            raise DBError(f"no webharbor dir for site {site!r}")
        dbfile = _WEB_DBFILE.get(site, f"{site}.db")
        return self._run(self._db(Path(self.webharbor_dir) / dbfile), sql, params)

    def _run(self, db_path: Path, sql: str, params: tuple) -> list[sqlite3.Row]:
        try:
            # read-write open so committed WAL rows are visible; we never write.
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise DBError(f"{db_path.name}: {e}") from e

    def files(self, under: str = ".") -> dict[str, Path]:
        """relpath -> abspath for all files under workspace/<under>."""
        out: dict[str, Path] = {}
        root = (self.workspace / under) if self.workspace else None
        if not root or not root.exists():
            return out
        for dp, _dn, fns in os.walk(root):
            if "/.git" in dp or dp.endswith("/.git"):
                continue
            for fn in fns:
                p = Path(dp) / fn
                out[str(p.relative_to(self.workspace))] = p
        return out

    def columns(self, app: str, table: str) -> set[str]:
        table_sql = _quote_ident(table)
        return {r["name"] for r in self.q(app, f"PRAGMA table_info({table_sql})")}

    def read(self, relpath) -> str:
        try:
            return (self.workspace / relpath).read_text(encoding="utf-8",
                                                        errors="replace")
        except OSError:
            return ""


class World:
    """AFTER + BASELINE pair; every primitive diffs against the baseline."""

    def __init__(self, args: argparse.Namespace | None = None, *,
                 workspace=None, data_dir=None, webharbor_dir=None,
                 baseline_dir=None, baseline_webharbor_dir=None):
        if args is not None:
            workspace = args.workspace
            data_dir = args.data_dir
            webharbor_dir = args.webharbor_dir
            baseline_dir = args.baseline_dir
            baseline_webharbor_dir = args.baseline_webharbor_dir
        base = Path(baseline_dir)
        bwh = baseline_webharbor_dir or (base / "webharbor")
        self.after = _Side(workspace, data_dir, webharbor_dir)
        self.baseline = _Side(base / "workspace", base / "data", bwh)

    def _pk(self, table_info_rows) -> tuple[str, ...]:
        rows = list(table_info_rows)
        if not rows:
            raise DBError("table not found (empty PRAGMA table_info)")
        primary = tuple(
            row["name"] for row in sorted(rows, key=lambda row: row["pk"])
            if row["pk"]
        )
        if primary:
            return primary
        columns = {row["name"] for row in rows}
        return ("id",) if "id" in columns else ("rowid",)

    @staticmethod
    def _pk_sql(pk: tuple[str, ...]) -> tuple[str, tuple[str, ...], str]:
        aliases = ("_pk",) if len(pk) == 1 else tuple(
            f"_pk_{index}" for index in range(len(pk)))
        select = ", ".join(
            f"{_quote_ident(column)} AS {_quote_ident(alias)}"
            for column, alias in zip(pk, aliases))
        order = ", ".join(_quote_ident(column) for column in pk)
        return select, aliases, order

    def new_rows(self, app: str, table: str, where: str = "1=1",
                 params: tuple = ()) -> list[sqlite3.Row]:
        """Rows of <app>.db:<table> in AFTER but not BASELINE, matched by primary key (or id/rowid)."""
        table_sql = _quote_ident(table)
        info = self.after.q(app, f"PRAGMA table_info({table_sql})")
        primary_key = self._pk(info)
        select_pk, aliases, order = self._pk_sql(primary_key)
        baseline_info = self.baseline.q(app, f"PRAGMA table_info({table_sql})")
        if baseline_info:
            if self._pk(baseline_info) != primary_key:
                raise DBError(f"{app}.{table}: primary key changed from baseline")
            base_keys = {
                tuple(row[alias] for alias in aliases)
                for row in self.baseline.q(
                    app, f"SELECT {select_pk} FROM {table_sql}")
            }
        else:
            base_keys = set()       # table absent in baseline → all rows new
        rows = self.after.q(
            app,
            f"SELECT *, {select_pk} FROM {table_sql} "
            f"WHERE {where} ORDER BY {order}",
            params,
        )
        return [row for row in rows
                if tuple(row[alias] for alias in aliases) not in base_keys]

    def web_new_rows(self, site: str, table: str, where: str = "1=1",
                     params: tuple = ()) -> list[sqlite3.Row]:
        table_sql = _quote_ident(table)
        info = self.after.web(site, f"PRAGMA table_info({table_sql})")
        primary_key = self._pk(info)
        select_pk, aliases, order = self._pk_sql(primary_key)
        baseline_info = self.baseline.web(site, f"PRAGMA table_info({table_sql})")
        if baseline_info:
            if self._pk(baseline_info) != primary_key:
                raise DBError(f"{site}.{table}: primary key changed from baseline")
            base_keys = {
                tuple(row[alias] for alias in aliases)
                for row in self.baseline.web(
                    site, f"SELECT {select_pk} FROM {table_sql}")
            }
        else:
            base_keys = set()
        rows = self.after.web(
            site,
            f"SELECT *, {select_pk} FROM {table_sql} "
            f"WHERE {where} ORDER BY {order}",
            params,
        )
        return [row for row in rows
                if tuple(row[alias] for alias in aliases) not in base_keys]

    def file_new(self, *, under: str = ".", suffix: str | None = None,
                 name_contains: str | None = None,
                 content_mentions=None) -> list[str]:
        """Relpaths of files that exist AFTER but not in the baseline."""
        base = set(self.baseline.files(under))
        out = []
        for rel, _p in sorted(self.after.files(under).items()):
            if rel in base:
                continue
            fn = Path(rel).name.lower()
            if suffix and not fn.endswith(suffix.lower()):
                continue
            if name_contains and name_contains.lower() not in fn:
                continue
            if content_mentions and not mentions(self.after.read(rel),
                                                 content_mentions):
                continue
            out.append(rel)
        return out

    def file_changed(self, relpath: str, *, now_mentions=None) -> bool:
        """True iff the file differs from baseline (and, if given, now contains the mention terms)."""
        before, after = self.baseline.read(relpath), self.after.read(relpath)
        if before == after:
            return False
        return mentions(after, now_mentions) if now_mentions else True

    def file_changed_delta(self, relpath: str, *, delta_mentions=None,
                           now_mentions=None) -> bool:
        """Like file_changed, but ``delta_mentions`` must appear in the newly added/changed text."""
        before, after = self.baseline.read(relpath), self.after.read(relpath)
        if before == after:
            return False
        if now_mentions and not mentions(after, now_mentions):
            return False
        if not delta_mentions:
            return True
        if after.startswith(before):
            delta = after[len(before):]
        else:
            delta = "\n".join(
                line[2:] for line in difflib.ndiff(before.splitlines(), after.splitlines())
                if line.startswith("+ ")
            )
        return mentions(delta, delta_mentions)

    def read(self, relpath: str) -> str:
        return self.after.read(relpath)


def _row_evidence(r: sqlite3.Row, *keys, width: int = 380) -> str:
    names = [k for k in (keys or r.keys())
             if k in r.keys() and not str(k).startswith("_pk")]
    d = {k: r[k] for k in names}
    return json.dumps(d, ensure_ascii=False, default=str)[:width]


def row_summary(rows, *keys, limit: int = 3) -> str:
    """Render up to ``limit`` rows as compact evidence with real values (the judge sees only this)."""
    if isinstance(rows, bool):
        return "condition holds" if rows else "condition does NOT hold"
    if not rows:
        return "ABSENT (no matching new row/file)"
    if rows and isinstance(rows[0], str):       # file relpaths
        return f"{len(rows)} new file(s): " + ", ".join(rows[:6])
    parts = [_row_evidence(r, *keys) for r in rows[:limit]]
    more = f" …+{len(rows) - limit} more" if len(rows) > limit else ""
    return f"{len(rows)} new row(s): " + " | ".join(parts) + more


class Result:
    """Accumulates checks and renders the blind evidence + RESULT_JSON (3-class verdict)."""

    def __init__(self, task: str):
        self.task = task
        self.checks: dict[str, bool] = {}
        self.details: dict[str, str] = {}
        self.errors: dict[str, str] = {}

    def add(self, check_id: str, ok: bool, evidence: str) -> bool:
        """Record one check; ``evidence`` must carry the real extracted values, not a bare ok/fail."""
        self.checks[check_id] = bool(ok)
        self.details[check_id] = str(evidence)
        print(f"[ev] {check_id}: {evidence}")
        return bool(ok)

    def check(self, check_id: str, fn, *evidence_keys) -> bool:
        """Run ``fn`` (a 0-arg primitive call): DBError is a world error, a truthy result passes."""
        try:
            rows = fn()
        except DBError as e:
            return self.add_error(check_id, str(e))
        ok = bool(rows)
        self.add(check_id, ok, row_summary(rows, *evidence_keys))
        return ok

    def add_error(self, check_id: str, msg: str) -> bool:
        """Record a world/infra problem (missing db, corrupt file); yields world_imperfect."""
        self.errors[check_id] = str(msg)
        print(f"[ev] {check_id}: WORLD-ERROR {msg}")
        return False

    def verdict(self) -> str:
        if self.errors:
            return "world_imperfect"
        if not self.checks:
            return "failed"
        return "completed" if all(self.checks.values()) else "failed"

    def finish(self):
        v = self.verdict()
        passed = sum(1 for x in self.checks.values() if x)
        print(f"\nVERDICT: {v} ({passed}/{len(self.checks)} checks"
              + (f", {len(self.errors)} world errors" if self.errors else "") + ")")
        print("RESULT_JSON=" + json.dumps(
            {"task": self.task, "checks": self.checks, "details": self.details,
             "errors": self.errors, "verdict": v}, ensure_ascii=False))
        sys.exit(0 if v == "completed" else 1)
