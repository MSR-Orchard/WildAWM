#!/usr/bin/env python3
"""Discover per-world MCP and WebHarbor capabilities from live artifacts."""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simpleArgParser import parse_args

try:
    from wildawm.webharbor import lib as wh_lib
except ModuleNotFoundError:
    wh_lib = None

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
OUT_MD = PROMPTS_DIR / "capability_contract.md"


@dataclass(frozen=True)
class Stamp:
    app: str
    table: str
    column: str
    value: str
    source: str
    needle: str
    severity: str


# Global stamp rules (none by default); each world adds its own via overlay/capability_rules.json.
STAMPED: list[Stamp] = []


@dataclass
class CapabilityContractArgs:
    cmd: str = "generate"


def webharbor_registry() -> list[dict]:
    return wh_lib.site_registry() if wh_lib is not None else []


@dataclass
class Violation:
    severity: str
    where: str
    column: str
    line: str
    why: str


def _snapshot_path(snapshot: Path | None = None) -> Path:
    return Path(snapshot) if snapshot is not None else Path(
        os.environ.get("AWM_XAI_SNAPSHOT_DIR", "/tmp/_xai_snap"))


def _load_overlay_stamps(snapshot: Path | None = None) -> list[Stamp]:
    active = _snapshot_path(snapshot)
    candidates = [active / "overlay" / "capability_rules.json"]
    env_path = os.environ.get("AWM_CAPABILITY_OVERLAY_RULES")
    if snapshot is None and env_path:
        candidates.insert(0, Path(env_path))
    out: list[Stamp] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows = data.get("stamped") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            out.append(Stamp(
                app=str(row.get("app") or ""),
                table=str(row.get("table") or ""),
                column=str(row.get("column") or ""),
                value=str(row.get("value") or ""),
                source=str(row.get("source") or ""),
                needle=str(row.get("needle") or ""),
                severity=str(row.get("severity") or "warn"),
            ))
        break
    return out


def active_stamps(snapshot: Path | None = None) -> list[Stamp]:
    rows = list(STAMPED)
    seen = {(row.app, row.table, row.column) for row in rows}
    for row in _load_overlay_stamps(snapshot):
        key = (row.app, row.table, row.column)
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return rows


def _active_app_dirs(snapshot: Path | None = None) -> dict[str, Path]:
    active = _snapshot_path(snapshot)
    apps = active / "overlay" / "apps"
    data = active / "data"
    if not apps.is_dir() or not data.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(apps.iterdir())
        if path.is_dir() and (data / f"{path.name}.db").is_file()
    }


def _sqlite_schema(directory: Path, *, only: set[str] | None = None) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    if not directory.is_dir():
        return result
    for db_path in sorted(directory.glob("*.db")):
        if only is not None and db_path.stem not in only:
            continue
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                table_names = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                tables: dict[str, list[str]] = {}
                for table in table_names:
                    quoted = str(table).replace('"', '""')
                    columns: list[str] = []
                    for row in connection.execute(f'PRAGMA table_info("{quoted}")'):
                        flags = []
                        if row[5]:
                            flags.append("PRIMARY KEY")
                        if row[3]:
                            flags.append("NOT NULL")
                        suffix = f" ({', '.join(flags)})" if flags else ""
                        columns.append(f"{row[1]} {row[2] or 'ANY'}{suffix}")
                    foreign_keys = list(connection.execute(
                        f'PRAGMA foreign_key_list("{quoted}")'))
                    columns.extend(
                        f"FOREIGN KEY {row[3]} -> {row[2]}.{row[4]}"
                        for row in foreign_keys
                    )
                    tables[str(table)] = columns
                result[db_path.stem] = tables
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"failed to inspect SQLite schema {db_path}: {exc}") from exc
    return result


def derive_db_schema(snapshot: Path | None = None) -> dict[str, dict[str, list[str]]]:
    active = _snapshot_path(snapshot)
    return _sqlite_schema(active / "data", only=set(_active_app_dirs(active)))


def derive_web_schema(snapshot: Path | None = None) -> dict[str, dict[str, list[str]]]:
    return _sqlite_schema(_snapshot_path(snapshot) / "webharbor")


def _annotation_names(annotation: ast.AST | None) -> set[str]:
    names: set[str] = set()
    if annotation is None:
        return names
    for node in ast.walk(annotation):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "schemas":
                names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _schema_models(app_dir: Path) -> dict[str, list[str]]:
    path = app_dir / "schemas.py"
    if not path.is_file():
        return {}
    tree = ast.parse(path.read_text())
    models: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = [
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        ]
        if fields:
            models[node.name] = fields
    return models


def derive_operations(snapshot: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Extract routes and request schemas from generated FastAPI decorators."""
    result: dict[str, list[dict[str, Any]]] = {}
    for app, app_dir in _active_app_dirs(snapshot).items():
        path = app_dir / "handlers.py"
        models = _schema_models(app_dir)
        operations: list[dict[str, Any]] = []
        if not path.is_file():
            result[app] = operations
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr.lower()
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                route = ""
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    route = str(decorator.args[0].value)
                operation_id = node.name
                for keyword in decorator.keywords:
                    if keyword.arg == "operation_id" and isinstance(keyword.value, ast.Constant):
                        operation_id = str(keyword.value.value)
                request_models: list[str] = []
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    for name in _annotation_names(arg.annotation):
                        if name in models and name not in request_models:
                            request_models.append(name)
                operations.append({
                    "method": method.upper(),
                    "path": route,
                    "operation_id": operation_id,
                    "request_models": request_models,
                    "request_fields": {
                        name: models[name] for name in request_models
                    },
                })
        result[app] = operations
    return result


def derive_settable(snapshot: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """Return fields from request models actually attached to write routes."""
    result: dict[str, dict[str, list[str]]] = {}
    for app, operations in derive_operations(snapshot).items():
        models: dict[str, list[str]] = {}
        for operation in operations:
            if operation["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            models.update(operation["request_fields"])
        result[app] = models
    return result


def verify(snapshot: Path | None = None) -> list[str]:
    problems: list[str] = []
    active = _snapshot_path(snapshot)
    schemas = derive_db_schema(active)
    for stamp in active_stamps(active):
        source = Path(stamp.source)
        if not source.is_absolute():
            source = active / "overlay" / "apps" / stamp.app / source
        if not source.is_file() or stamp.needle not in source.read_text(errors="replace"):
            problems.append(
                f"{stamp.app}.{stamp.table}.{stamp.column}: capability-rule source evidence is missing")
        table_columns = schemas.get(stamp.app, {}).get(stamp.table, [])
        names = {column.split(" ", 1)[0] for column in table_columns}
        if names and stamp.column not in names and stamp.column != "rowid":
            problems.append(f"{stamp.app}.{stamp.table}.{stamp.column}: column missing from snapshot DB")
    return problems


def lint_text(text: str, *, where: str = "<text>") -> list[Violation]:
    """Reject equality gates on fields declared tool-stamped by this world."""
    violations: list[Violation] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for stamp in active_stamps():
            pattern = re.compile(
                rf"(?:\b{re.escape(stamp.column)}\s*(?:=|==|!=|<>|\s+LIKE\b|\s+IN\b)|"
                rf"\[\s*['\"]{re.escape(stamp.column)}['\"]\s*\]\s*(?:==|!=|\.startswith|\s+in\b))",
                re.I,
            )
            if pattern.search(line):
                violations.append(Violation(
                    stamp.severity,
                    f"{where}:{line_number}",
                    f"{stamp.app}.{stamp.table}.{stamp.column}",
                    line.strip()[:160],
                    f"{stamp.column} is tool-stamped: {stamp.value}",
                ))
    return violations


def lint_task_dir(task_dir: Path) -> list[Violation]:
    root = Path(task_dir)
    targets = list((root / "verifier" / "scripts").glob("*.py")) + [
        root / "verifier" / "rubrics.md",
        root / "verifier" / "verify.skill.md",
        root / "golden.py",
    ]
    violations: list[Violation] = []
    for path in targets:
        if not path.is_file() or path.name == "_verify_sdk.py":
            continue
        violations.extend(lint_text(
            path.read_text(errors="replace"), where=str(path.relative_to(root))))
    return violations


def generate_md(snapshot: Path | None = None) -> str:
    active = _snapshot_path(snapshot)
    lines = [
        "# Live world capability and schema contract",
        "",
        "This contract is generated from the selected world. It contains no fixed application or website ontology.",
        "",
        "## Rules",
        "",
        "- Use only listed live MCP operations and request fields.",
        "- Derive task-local verifier queries from exact current tables and columns.",
        "- Resolve identities and targets through current natural keys and foreign-key relationships.",
        "- Do not gate on tool-stamped fields.",
        "- Discover Web UI behavior live. The Web schemas below describe observable persistence; they do not prove that a specific UI action writes a table.",
        "",
        "## Tool-stamped fields",
        "",
    ]
    stamps = active_stamps(active)
    if stamps:
        lines.extend(["| app.table.column | value | severity |", "|---|---|---|"])
        lines.extend(
            f"| `{s.app}.{s.table}.{s.column}` | {s.value} | {s.severity} |"
            for s in stamps
        )
    else:
        lines.append("(none declared)")
    lines.extend(["", "## Active MCP operations", ""])
    operations = derive_operations(active)
    for app, rows in operations.items():
        lines.append(f"### {app}")
        if not rows:
            lines.append("- No decorated operations discovered.")
        for row in rows:
            fields = "; ".join(
                f"{model}({', '.join(values)})"
                for model, values in row["request_fields"].items()
            ) or "no request body"
            lines.append(
                f"- `{row['method']} {row['path']}` operation `{row['operation_id']}`: {fields}")
        lines.append("")
    lines.extend(["## Active MCP database schemas", ""])
    for app, tables in derive_db_schema(active).items():
        lines.append(f"### {app}")
        for table, columns in tables.items():
            lines.append(f"- `{table}`: " + ", ".join(f"`{c}`" for c in columns))
        lines.append("")
    lines.extend(["## WebHarbor canonical site registry", ""])
    registry = webharbor_registry()
    for row in registry:
        lines.append(
            f"- site id `{row['id']}`: database `{row['dbfile']}`; "
            f"domains {row.get('domains') or []}")
    lines.extend(["", "## WebHarbor persistence schemas", ""])
    registry_by_stem = {
        Path(row["dbfile"]).stem: row for row in registry
    }
    for database_stem, tables in derive_web_schema(active).items():
        row = registry_by_stem.get(database_stem)
        heading = (
            f"{row['id']} (database {row['dbfile']})"
            if row else database_stem
        )
        lines.append(f"### {heading}")
        for table, columns in tables.items():
            lines.append(f"- `{table}`: " + ", ".join(f"`{c}`" for c in columns))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    command = parse_args(CapabilityContractArgs).cmd
    if command == "verify":
        problems = verify()
        print("\n".join(problems) if problems else "contract OK")
        return 1 if problems else 0
    markdown = generate_md()
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(markdown)
    print(f"wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
