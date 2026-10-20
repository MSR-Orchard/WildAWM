"""Revisioned shared MCP application library; world overlays pin a concrete revision."""
from __future__ import annotations

import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis.worlds import runtime as rt
from wildawm.synthesis.worlds import skills

SCHEMA_VERSION = 2
SELECTION_SCHEMA_VERSION = 1
APP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
REQUIRED_FILES = ("models.py", "schemas.py", "handlers.py", "run.py", "capability_rules.json", "README.md")
POLICIES = {"shared", "local"}
CONSTITUTION = Path(__file__).resolve().parent / "constitutions" / "app_creator.md"
DECISION_FILE = "_revision_decision.json"
REVISION_MARKER = "_app_revision.json"


@dataclass(frozen=True)
class AppRequirement:
    app_id: str
    activities: tuple[str, ...]
    skills: tuple[str, ...]
    policy: str = "shared"

    @property
    def path(self) -> Path:
        return app_dir(self.app_id)


@dataclass(frozen=True)
class AppSelection:
    app_id: str
    revision: int
    content_sha256: str
    policy: str

    @property
    def path(self) -> Path:
        return package_for_revision(self.app_id, self.revision)


@dataclass(frozen=True)
class PreparedApp:
    app_id: str
    revision: int
    content_sha256: str
    policy: str
    change: str
    previous_revision: int | None


def library_root() -> Path:
    return cfg.WS_APPS_DIR


def app_dir(app_id: str) -> Path:
    return library_root() / app_id


def meta_path(app_id: str) -> Path:
    return app_dir(app_id) / "_app_meta.json"


def selection_manifest_path(manifest_path: Path) -> Path:
    path = Path(manifest_path)
    name = path.name.replace(".skills.json", ".apps.json")
    return path.with_name(name)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _excluded_package_path(relative: Path) -> bool:
    return (
        not relative.parts
        or relative.parts[0] in {"history", "__pycache__"}
        or "__pycache__" in relative.parts
        or relative.name in {"_app_meta.json", DECISION_FILE, REVISION_MARKER}
        or relative.suffix == ".pyc"
    )


def _package_files(root: Path):
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if not _excluded_package_path(relative):
            yield path, relative


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path, relative in _package_files(root):
        digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _copy_package(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    for path, relative in _package_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    (destination / "__init__.py").touch(exist_ok=True)


@contextlib.contextmanager
def _library_lock():
    root = library_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".library.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_meta(app_id: str) -> dict:
    try:
        loaded = json.loads(meta_path(app_id).read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def current_revision(app_id: str) -> int:
    return int(load_meta(app_id).get("revision", 1) or 1)


def package_for_revision(app_id: str, revision: int) -> Path:
    root = app_dir(app_id)
    if revision == current_revision(app_id):
        return root
    return root / "history" / f"revision_{revision:04d}"


def _migration_files(path: Path) -> list[Path]:
    migrations = Path(path) / "migrations"
    if not migrations.is_dir():
        return []
    return sorted(p for p in migrations.glob("revision_*_to_*.py") if p.is_file())


def app_problems(path: Path, app_id: str, *, forbidden_terms: tuple[str, ...] = ()) -> list[str]:
    path = Path(path)
    problems: list[str] = []
    if not APP_ID_RE.fullmatch(app_id):
        problems.append(f"invalid app id {app_id!r}")
    for filename in REQUIRED_FILES:
        file_path = path / filename
        if not file_path.is_file() or file_path.stat().st_size == 0:
            problems.append(f"missing or empty {filename}")
    python_files = [path / filename for filename in ("models.py", "schemas.py", "handlers.py", "run.py")]
    python_files += _migration_files(path)
    for file_path in python_files:
        if not file_path.is_file():
            continue
        filename = file_path.relative_to(path).as_posix()
        text = file_path.read_text(errors="replace")
        try:
            tree = ast.parse(text, filename=str(file_path))
        except SyntaxError as exc:
            problems.append(f"{filename} syntax error: {exc}")
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or ""]
                else:
                    imports = []
                for imported in imports:
                    if imported.split(".", 1)[0] in {"subprocess", "docker", "git"}:
                        problems.append(f"{filename} contains forbidden import {imported!r}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for fragment in ("/home/", "tasks_xai/", "worlds/_master", "git push", "git commit", "docker build"):
                        if fragment in node.value:
                            problems.append(f"{filename} contains forbidden string fragment {fragment!r}")
        if f"wildawm.apps.{app_id}" in text:
            problems.append(f"{filename} imports a pre-existing {app_id} implementation")
    for filename in REQUIRED_FILES:
        file_path = path / filename
        if not file_path.is_file():
            continue
        lowered = file_path.read_text(errors="replace").lower()
        for term in forbidden_terms:
            term = str(term or "").strip()
            if len(term) >= 4 and term.lower() in lowered:
                problems.append(f"{filename} contains persona-specific term {term!r}")
        if re.search(r"(?:^|\s)(?:/home/|worlds/_masters/|worlds/_scripts/)", lowered):
            problems.append(f"{filename} contains a concrete runtime or synthesized-world path")
    run_text = (path / "run.py").read_text(errors="replace") if (path / "run.py").is_file() else ""
    if not re.search(r"\bdef\s+create_app\s*\(", run_text):
        problems.append("run.py must define create_app(db_path)")
    for migration in _migration_files(path):
        migration_text = migration.read_text(errors="replace")
        if not re.search(r"\bdef\s+migrate\s*\(", migration_text):
            problems.append(f"{migration.name} must define migrate(db_path)")
        if "__main__" not in migration_text or "sys.argv" not in migration_text:
            problems.append(f"{migration.name} must provide a command-line entry point")
    try:
        capability = json.loads((path / "capability_rules.json").read_text())
        stamped = capability.get("stamped") if isinstance(capability, dict) else None
        if not isinstance(stamped, list):
            problems.append("capability_rules.json must be an object with a stamped list")
        else:
            required = {"table", "column", "value", "source", "needle", "severity"}
            for index, row in enumerate(stamped):
                if not isinstance(row, dict) or not required.issubset(row):
                    problems.append(f"capability_rules.json stamped[{index}] must contain {sorted(required)}")
    except (OSError, ValueError) as exc:
        problems.append(f"capability_rules.json is invalid: {exc}")
    return problems


def _operation_ids(path: Path) -> list[str]:
    values: set[str] = set()
    for filename in ("handlers.py", "run.py"):
        file_path = Path(path) / filename
        if not file_path.is_file():
            continue
        text = file_path.read_text(errors="replace")
        values.update(re.findall(r"operation_id\s*=\s*[\"']([^\"']+)[\"']", text))
    return sorted(values)


def list_apps() -> list[dict]:
    root = library_root()
    if not root.is_dir():
        return []
    rows = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if app_problems(directory, directory.name):
            continue
        meta = load_meta(directory.name)
        rows.append({
            "id": directory.name,
            "revision": int(meta.get("revision", 1) or 1),
            "description": str(meta.get("description") or ""),
            "activities": list(meta.get("activities") or []),
            "operation_ids": list(meta.get("operation_ids") or _operation_ids(directory)),
            "uses": list(meta.get("uses") or []),
            "path": str(directory),
        })
    return rows


def format_library_index() -> str:
    rows = list_apps()
    if not rows:
        return "(empty: no MCP service implementation exists yet; request only services required by this persona's activities)"
    lines = []
    for row in rows:
        operations = ", ".join(row["operation_ids"][:12]) or "operation contract unavailable"
        activities = "; ".join(row["activities"][-3:]) or row["description"] or "reusable MCP service"
        lines.append(f"- {row['id']} (revision {row['revision']}; operations: {operations}): {activities}")
    return "\n".join(lines)


def plan_app_requirements(manifest_path: Path) -> list[AppRequirement]:
    data = skills.load_plan_manifest(manifest_path)
    activities: dict[str, list[str]] = {}
    skill_ids: dict[str, list[str]] = {}
    policies: dict[str, set[str]] = {}
    for event in data["events"]:
        policy = str(event.get("mcp_app_policy") or "shared").strip().lower()
        for raw_app in event.get("mcp_apps", []):
            app_id = str(raw_app).strip()
            if not app_id:
                continue
            activities.setdefault(app_id, []).append(str(event["activity"]).strip())
            skill_ids.setdefault(app_id, []).append(str(event["skill"]).strip())
            policies.setdefault(app_id, set()).add(policy)
    requirements = []
    for app_id in sorted(activities):
        app_policies = policies.get(app_id) or {"shared"}
        if len(app_policies) != 1:
            raise RuntimeError(f"day plan assigns conflicting MCP policies to {app_id}: {sorted(app_policies)}")
        policy = next(iter(app_policies))
        if policy not in POLICIES:
            raise RuntimeError(f"invalid MCP app policy for {app_id}: {policy}")
        requirements.append(AppRequirement(
            app_id,
            tuple(dict.fromkeys(activities[app_id])),
            tuple(dict.fromkeys(skill_ids[app_id])),
            policy,
        ))
    return requirements


def missing_plan_apps(manifest_path: Path) -> list[AppRequirement]:
    return [req for req in plan_app_requirements(manifest_path) if app_problems(req.path, req.app_id)]


def _profile_terms(profile_id: str) -> tuple[str, ...]:
    terms = [profile_id]
    persona = cfg.WS_PROFILES_DIR / profile_id / "persona.md"
    if persona.is_file():
        text = persona.read_text(errors="replace")
        match = re.search(r"^\s*[-*]\s*\*\*Name:\*\*\s*(.+?)\s*$", text, re.M)
        if not match:
            match = re.search(r"^#\s*Persona Bible\s*[—:-]\s*(.+?)\s*$", text, re.M)
        if match:
            terms.append(match.group(1).strip())
    return tuple(terms)


def _stage_dir(app_id: str, profile_id: str, day: int, purpose: str) -> Path:
    safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile_id)
    return library_root() / ".staging" / f"{app_id}-{safe_profile}-d{day}-{purpose}-{os.getpid()}"


def _done_check_app(path: Path, app_id: str, forbidden_terms: tuple[str, ...]):
    def _check() -> tuple[bool, str]:
        problems = app_problems(path, app_id, forbidden_terms=forbidden_terms)
        return (not problems, "application package is complete" if not problems else "; ".join(problems[:6]))
    return _check


def _creation_prompt(requirement: AppRequirement, output: Path) -> str:
    activities = "\n".join(f"- {activity}" for activity in requirement.activities)
    skill_ids = ", ".join(requirement.skills)
    return f"""# Create reusable MCP service: {requirement.app_id}

Write the complete service package to `{output}`.

Activities that first require this service:
{activities}

Matched reusable event skills: {skill_ids}

This is revision 1 of a global shared implementation. Later days and personas may reuse it or create backward-compatible revisions from it. Generalize the data model and API around the activity class. Do not include any persona identity, account value, date, fixed row id, or synthesized-world path.

No MCP service exists at the beginning of synthesis. Do not import or copy `wildawm.apps.{requirement.app_id}`. You may read `wildawm/apps/server_utils.py` for shared infrastructure and the app_creator constitution for the package contract.

Write every required file, run the specified compile/import checks, then reply DONE."""


def _revision_prompt(requirement: AppRequirement, stage: Path, revision: int, meta: dict) -> str:
    previous = revision
    target = revision + 1
    prior_activities = "\n".join(f"- {value}" for value in meta.get("activities", [])) or "(none recorded)"
    requested = "\n".join(f"- {value}" for value in requirement.activities)
    migration = f"migrations/revision_{previous:04d}_to_{target:04d}.py"
    return f"""# Review and possibly evolve shared MCP service: {requirement.app_id}

The complete current revision {previous} package has been copied to `{stage}`. Review it against the new reusable activities below.

Activities already supported by metadata:
{prior_activities}

New activities to support:
{requested}

Matched reusable event skills: {', '.join(requirement.skills)}

Choose one action:

1. `reuse`: the current schema and operations already support every new activity. Do not modify package code.
2. `revise`: modify this staged package into backward-compatible revision {target}. Preserve every existing operation ID and existing field meaning. Add the smallest generic capabilities required by the new activities.

If `models.py` changes or persistent schema changes, write an idempotent migration at `{stage / migration}`. It must define `migrate(db_path)`, accept a database path through `sys.argv[1]` when executed directly, preserve existing rows, work when called twice, and make a revision-{previous} database structurally equivalent to a fresh revision-{target} database. Do not rely on `Base.metadata.create_all` to alter existing tables.

Always write `{stage / DECISION_FILE}` with this exact JSON shape:

```json
{{
  "action": "reuse or revise",
  "reason": "short persona-neutral reason",
  "schema_changed": false,
  "migration_file": null,
  "capabilities_added": ["generic capability"]
}}
```

For a schema revision, set `schema_changed` to true and `migration_file` to `{migration}`. For reuse, keep package code byte-identical, use `schema_changed=false`, `migration_file=null`, and an empty `capabilities_added` list.

The result is global shared code. Do not include persona identities, account values, dates, fixed row ids, or world paths. Follow the app_creator constitution and reply DONE only after the decision and all required validation artifacts exist."""


def _smoke_generated_app(_container: str, staged: Path, app_id: str) -> None:
    script = r'''import shutil, sys, tempfile
from pathlib import Path
from wildawm.cli.gateway import create_gateway
source = Path(sys.argv[1])
app_id = sys.argv[2]
with tempfile.TemporaryDirectory(prefix="awm-app-smoke-") as tmp:
    root = Path(tmp)
    apps = root / "apps"
    data = root / "data"
    data.mkdir()
    shutil.copytree(source, apps / app_id)
    gateway = create_gateway(str(data), app_ids=[app_id], extra_apps_dir=str(apps))
    if gateway.state.loaded_apps != [app_id] or gateway.state.load_errors:
        raise SystemExit(f"gateway load failed: loaded={gateway.state.loaded_apps} errors={gateway.state.load_errors}")
    mounted = [route for route in gateway.routes if getattr(route, "path", "") == f"/{app_id}"]
    if not mounted:
        raise SystemExit("generated app was not mounted")
    service_routes = [route for route in mounted[0].app.routes if getattr(route, "operation_id", None)]
    if not service_routes:
        raise SystemExit("generated app exposes no operation-id routes")
'''
    mounted = f"{Path(staged).resolve()}:/tmp/generated-app:ro"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i", "-v", mounted,
            "--entrypoint", "python3", rt.GATEWAY_IMAGE,
            "-", "/tmp/generated-app", app_id,
        ],
        input=script,
        text=True,
        capture_output=True,
        timeout=cfg.DOCKER_COMMAND_TIMEOUT_S,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"generated app {app_id} failed runtime smoke: {detail[:1200]}")


def _load_decision(stage: Path) -> dict:
    try:
        loaded = json.loads((Path(stage) / DECISION_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid app revision decision: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("app revision decision must be a JSON object")
    return loaded


def _revision_problems(stage: Path, previous: Path, requirement: AppRequirement, revision: int, forbidden_terms: tuple[str, ...]) -> list[str]:
    problems = app_problems(stage, requirement.app_id, forbidden_terms=forbidden_terms)
    try:
        decision = _load_decision(stage)
    except RuntimeError as exc:
        return problems + [str(exc)]
    action = str(decision.get("action") or "")
    if action not in {"reuse", "revise"}:
        problems.append("revision decision action must be reuse or revise")
    reason = str(decision.get("reason") or "").strip()
    if not reason:
        problems.append("revision decision reason is required")
    lowered = json.dumps(decision, ensure_ascii=False).lower()
    for term in forbidden_terms:
        if len(str(term)) >= 4 and str(term).lower() in lowered:
            problems.append(f"revision decision contains persona-specific term {term!r}")
    previous_hash = _sha256_tree(previous)
    staged_hash = _sha256_tree(stage)
    models_changed = (previous / "models.py").read_bytes() != (stage / "models.py").read_bytes()
    schema_changed = bool(decision.get("schema_changed"))
    migration_value = decision.get("migration_file")
    if action == "reuse":
        if staged_hash != previous_hash:
            problems.append("reuse decision modified package code")
        if schema_changed or migration_value:
            problems.append("reuse decision cannot declare a schema migration")
    if action == "revise":
        if staged_hash == previous_hash:
            problems.append("revise decision did not modify package code")
        old_operations = set(_operation_ids(previous))
        new_operations = set(_operation_ids(stage))
        missing = sorted(old_operations - new_operations)
        if missing:
            problems.append(f"revision removes existing operation IDs: {missing}")
        if models_changed and not schema_changed:
            problems.append("models.py changed but schema_changed is false")
        if schema_changed:
            expected = f"migrations/revision_{revision:04d}_to_{revision + 1:04d}.py"
            if migration_value != expected:
                problems.append(f"schema revision must declare migration_file {expected}")
            elif not (stage / expected).is_file():
                problems.append(f"schema migration file missing: {expected}")
    capabilities = decision.get("capabilities_added")
    if not isinstance(capabilities, list):
        problems.append("capabilities_added must be a list")
    return problems


def _done_check_revision(stage: Path, previous: Path, requirement: AppRequirement, revision: int, forbidden_terms: tuple[str, ...]):
    def _check() -> tuple[bool, str]:
        problems = _revision_problems(stage, previous, requirement, revision, forbidden_terms)
        return (not problems, "revision review is complete" if not problems else "; ".join(problems[:7]))
    return _check


def _smoke_revision(previous: Path, revised: Path, app_id: str, revision: int, decision: dict) -> None:
    migration = str(decision.get("migration_file") or "")
    script = r'''import importlib.util, json, sqlite3, sys, tempfile
from pathlib import Path
from wildawm.cli.gateway import _load_overlay_app
app_id, migration_rel = sys.argv[1], sys.argv[2]

def shape(db):
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: [r[1:6] for r in con.execute(f"PRAGMA table_info('{table}')")] for table in tables}
    finally:
        con.close()

with tempfile.TemporaryDirectory(prefix="awm-app-revision-") as tmp:
    root = Path(tmp)
    migrated = root / "migrated.db"
    fresh = root / "fresh.db"
    old_app = _load_overlay_app(app_id, "/tmp/previous", str(migrated))
    old_ops = {getattr(route, "operation_id", None) for route in old_app.routes} - {None}
    if migration_rel:
        migration_path = Path("/tmp/revised") / app_id / migration_rel
        spec = importlib.util.spec_from_file_location("awm_generated_migration", migration_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(str(migrated))
        module.migrate(str(migrated))
    new_on_old = _load_overlay_app(app_id, "/tmp/revised", str(migrated))
    new_fresh = _load_overlay_app(app_id, "/tmp/revised", str(fresh))
    new_ops = {getattr(route, "operation_id", None) for route in new_on_old.routes} - {None}
    if not old_ops.issubset(new_ops):
        raise SystemExit(f"operation compatibility failed: missing={sorted(old_ops-new_ops)}")
    if shape(migrated) != shape(fresh):
        raise SystemExit("migrated schema differs from fresh revised schema: " + json.dumps({"migrated": shape(migrated), "fresh": shape(fresh)}, default=str)[:2000])
'''
    mounts = [
        "-v", f"{Path(previous).resolve()}:/tmp/previous/{app_id}:ro",
        "-v", f"{Path(revised).resolve()}:/tmp/revised/{app_id}:ro",
    ]
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", *mounts, "--entrypoint", "python3", rt.GATEWAY_IMAGE, "-", app_id, migration],
        input=script,
        text=True,
        capture_output=True,
        timeout=cfg.DOCKER_COMMAND_TIMEOUT_S,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"generated app {app_id} revision {revision + 1} failed compatibility smoke: {detail[:2000]}")


def _publish_new(requirement: AppRequirement, staged: Path, profile_id: str, day: int) -> Path:
    problems = app_problems(staged, requirement.app_id, forbidden_terms=_profile_terms(profile_id))
    if problems:
        raise RuntimeError(f"generated app {requirement.app_id} is invalid: {problems}")
    (staged / "__init__.py").touch(exist_ok=True)
    final = app_dir(requirement.app_id)
    with _library_lock():
        if final.is_dir() and not app_problems(final, requirement.app_id):
            return final
        publish = library_root() / f".{requirement.app_id}.{os.getpid()}.tmp"
        _copy_package(staged, publish)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "id": requirement.app_id,
            "revision": 1,
            "description": requirement.activities[0] if requirement.activities else "",
            "activities": list(requirement.activities),
            "operation_ids": _operation_ids(publish),
            "created_by": {"profile": profile_id, "day": day},
            "last_updated_by": {"profile": profile_id, "day": day},
            "uses": [],
            "content_sha256": _sha256_tree(publish),
            "revision_history": [],
        }
        (publish / "_app_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(publish, final)
    return final


def _publish_revision(requirement: AppRequirement, stage: Path, profile_id: str, day: int, expected_revision: int, expected_hash: str, decision: dict) -> bool:
    final = app_dir(requirement.app_id)
    with _library_lock():
        meta = load_meta(requirement.app_id)
        if int(meta.get("revision", 1) or 1) != expected_revision or _sha256_tree(final) != expected_hash:
            return False
        next_revision = expected_revision + 1
        publish = library_root() / f".{requirement.app_id}.r{next_revision}.{os.getpid()}.tmp"
        shutil.rmtree(publish, ignore_errors=True)
        shutil.copytree(final, publish, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        history_target = publish / "history" / f"revision_{expected_revision:04d}"
        _copy_package(final, history_target)
        for child in list(publish.iterdir()):
            if child.name in {"history", "_app_meta.json"}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for path, relative in _package_files(stage):
            target = publish / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (publish / "__init__.py").touch(exist_ok=True)
        activities = list(meta.get("activities") or [])
        for activity in requirement.activities:
            if activity not in activities:
                activities.append(activity)
        revision_history = list(meta.get("revision_history") or [])
        revision_history.append({
            "revision": expected_revision,
            "content_sha256": expected_hash,
            "superseded_by": next_revision,
            "schema_changed_in_next": bool(decision.get("schema_changed")),
            "migration_file_to_next": decision.get("migration_file"),
        })
        meta.update({
            "schema_version": SCHEMA_VERSION,
            "id": requirement.app_id,
            "revision": next_revision,
            "activities": activities,
            "operation_ids": _operation_ids(publish),
            "last_updated_by": {"profile": profile_id, "day": day},
            "last_revision_reason": str(decision.get("reason") or ""),
            "last_capabilities_added": list(decision.get("capabilities_added") or []),
            "content_sha256": _sha256_tree(publish),
            "revision_history": revision_history,
        })
        meta.setdefault("created_by", {"profile": profile_id, "day": day})
        meta.setdefault("uses", [])
        _atomic_write(publish / "_app_meta.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        backup = library_root() / f".{requirement.app_id}.old.{os.getpid()}"
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(final, backup)
        os.replace(publish, final)
        shutil.rmtree(backup, ignore_errors=True)
    return True


def _generate_missing(requirement: AppRequirement, profile_id: str, day: int, container: str) -> Path:
    stage = _stage_dir(requirement.app_id, profile_id, day, "create")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    rec = rt.run_stage_agent(
        _creation_prompt(requirement, stage),
        role="app_creator",
        container=container,
        cwd=str(stage),
        constitution=CONSTITUTION,
        timeout=cfg.WORLD_SYN_IMPLEMENT_TIMEOUT_S,
        max_turns=cfg.WORLD_SYN_MAX_LLM_TURNS,
        done_check=_done_check_app(stage, requirement.app_id, _profile_terms(profile_id)),
    )
    if app_problems(stage, requirement.app_id, forbidden_terms=_profile_terms(profile_id)):
        raise RuntimeError(f"app creator failed for {requirement.app_id}: {rec.status}\n{rec.result_text[:500]}")
    _smoke_generated_app(container, stage, requirement.app_id)
    try:
        return _publish_new(requirement, stage, profile_id, day)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _review_existing(requirement: AppRequirement, profile_id: str, day: int, container: str) -> Path:
    for _attempt in range(3):
        previous = app_dir(requirement.app_id)
        meta = load_meta(requirement.app_id)
        revision = int(meta.get("revision", 1) or 1)
        expected_hash = _sha256_tree(previous)
        stage = _stage_dir(requirement.app_id, profile_id, day, f"review-r{revision}")
        _copy_package(previous, stage)
        rec = rt.run_stage_agent(
            _revision_prompt(requirement, stage, revision, meta),
            role="app_creator",
            container=container,
            cwd=str(stage),
            constitution=CONSTITUTION,
            timeout=cfg.WORLD_SYN_IMPLEMENT_TIMEOUT_S,
            max_turns=cfg.WORLD_SYN_MAX_LLM_TURNS,
            done_check=_done_check_revision(stage, previous, requirement, revision, _profile_terms(profile_id)),
        )
        problems = _revision_problems(stage, previous, requirement, revision, _profile_terms(profile_id))
        if problems:
            raise RuntimeError(f"app revision review failed for {requirement.app_id}: {rec.status}; {problems}")
        decision = _load_decision(stage)
        try:
            if decision["action"] == "reuse":
                return previous
            _smoke_revision(previous, stage, requirement.app_id, revision, decision)
            if _publish_revision(requirement, stage, profile_id, day, revision, expected_hash, decision):
                return app_dir(requirement.app_id)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    raise RuntimeError(f"concurrent revisions prevented publishing {requirement.app_id} after 3 attempts")


def _record_requirement(requirement: AppRequirement) -> None:
    with _library_lock():
        path = meta_path(requirement.app_id)
        if not path.is_file():
            return
        meta = load_meta(requirement.app_id)
        key = "activities" if requirement.policy == "shared" else "local_requirements"
        activities = list(meta.get(key) or [])
        for activity in requirement.activities:
            if activity not in activities:
                activities.append(activity)
        meta[key] = activities
        _atomic_write(path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


def _requirements_sha256(manifest_path: Path) -> str:
    rows = [
        {
            "app_id": requirement.app_id,
            "activities": list(requirement.activities),
            "skills": list(requirement.skills),
            "policy": requirement.policy,
        }
        for requirement in plan_app_requirements(manifest_path)
    ]
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _write_selections(manifest_path: Path, selections: list[AppSelection]) -> None:
    payload = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "requirements_sha256": _requirements_sha256(manifest_path),
        "apps": {
            selection.app_id: {
                "revision": selection.revision,
                "content_sha256": selection.content_sha256,
                "policy": selection.policy,
            }
            for selection in selections
        },
    }
    _atomic_write(selection_manifest_path(manifest_path), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def load_plan_app_selections(manifest_path: Path) -> dict[str, AppSelection]:
    path = selection_manifest_path(manifest_path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid app selection manifest {path}: {exc}") from exc
    if data.get("schema_version") != SELECTION_SCHEMA_VERSION or not isinstance(data.get("apps"), dict):
        raise RuntimeError(f"invalid app selection manifest schema: {path}")
    expected_requirements = data.get("requirements_sha256")
    if expected_requirements and expected_requirements != _requirements_sha256(manifest_path):
        raise RuntimeError(f"app selection manifest requirements changed: {path}")
    selections: dict[str, AppSelection] = {}
    for app_id, row in data["apps"].items():
        if not isinstance(row, dict):
            raise RuntimeError(f"invalid app selection for {app_id}")
        selection = AppSelection(
            app_id=str(app_id),
            revision=int(row.get("revision", 0) or 0),
            content_sha256=str(row.get("content_sha256") or ""),
            policy=str(row.get("policy") or "shared"),
        )
        if selection.policy not in POLICIES or selection.revision < 1:
            raise RuntimeError(f"invalid app selection for {app_id}: {row}")
        package = selection.path
        if app_problems(package, app_id):
            raise RuntimeError(f"selected app revision is invalid: {app_id} revision {selection.revision}")
        if _sha256_tree(package) != selection.content_sha256:
            raise RuntimeError(f"selected app revision hash mismatch: {app_id} revision {selection.revision}")
        selections[app_id] = selection
    return selections


def ensure_plan_apps(profile_id: str, day: int, manifest_path: Path, *, container: str | None = None) -> list[Path]:
    requirements = plan_app_requirements(manifest_path)
    try:
        existing = load_plan_app_selections(manifest_path)
    except RuntimeError:
        existing = {}
    requirement_by_id = {requirement.app_id: requirement for requirement in requirements}
    if set(existing) == set(requirement_by_id) and all(
        selection.policy == requirement_by_id[app_id].policy
        for app_id, selection in existing.items()
    ):
        # Day-level selections are immutable; later revisions must not repin an accepted world.
        return [existing[app_id].path for app_id in sorted(existing)]
    owned_container = container is None
    paths: list[Path] = []
    selections: list[AppSelection] = []
    try:
        for requirement in requirements:
            path = requirement.path
            if app_problems(path, requirement.app_id):
                if container is None:
                    container = rt.ensure_creator(cfg.PROJECT_ROOT)
                path = _generate_missing(requirement, profile_id, day, container)
            meta = load_meta(requirement.app_id)
            supported = set(str(value) for value in meta.get("activities", []))
            has_unreviewed = any(activity not in supported for activity in requirement.activities)
            if requirement.policy == "shared" and has_unreviewed:
                if container is None:
                    container = rt.ensure_creator(cfg.PROJECT_ROOT)
                path = _review_existing(requirement, profile_id, day, container)
            problems = app_problems(path, requirement.app_id, forbidden_terms=_profile_terms(profile_id))
            if problems:
                raise RuntimeError(f"shared app {requirement.app_id} is invalid: {problems}")
            _record_requirement(requirement)
            meta = load_meta(requirement.app_id)
            revision = int(meta.get("revision", 1) or 1)
            selected_path = package_for_revision(requirement.app_id, revision)
            selections.append(AppSelection(requirement.app_id, revision, _sha256_tree(selected_path), requirement.policy))
            paths.append(selected_path)
        _write_selections(manifest_path, selections)
    finally:
        if owned_container and container is not None:
            rt.teardown_creator(cfg.PROJECT_ROOT)
    return paths


def validate_plan_apps(profile_id: str, manifest_path: Path) -> list[Path]:
    selections = load_plan_app_selections(manifest_path)
    requirements = {requirement.app_id: requirement for requirement in plan_app_requirements(manifest_path)}
    if set(selections) != set(requirements):
        raise RuntimeError(f"app selection mismatch: selected={sorted(selections)} required={sorted(requirements)}")
    paths = []
    for app_id, selection in selections.items():
        problems = app_problems(selection.path, app_id, forbidden_terms=_profile_terms(profile_id))
        if problems:
            raise RuntimeError(f"shared app {app_id} revision {selection.revision} is invalid: {problems}")
        paths.append(selection.path)
    return paths


def active_world_apps(snapshot: Path | None) -> set[str]:
    if snapshot is None:
        return set()
    apps = Path(snapshot) / "overlay" / "apps"
    if not apps.is_dir():
        return set()
    return {path.name for path in apps.iterdir() if path.is_dir()}


def _world_app_revision(snapshot: Path | None, app_id: str) -> int | None:
    if snapshot is None:
        return None
    package = Path(snapshot) / "overlay" / "apps" / app_id
    if not package.is_dir():
        return None
    marker = package / REVISION_MARKER
    if marker.is_file():
        try:
            return int(json.loads(marker.read_text()).get("revision"))
        except (TypeError, ValueError, OSError):
            pass
    return 1


def _replace_capability_rules(day_dir: Path, prev_snapshot: Path | None, replacements: dict[str, Path]) -> None:
    stamped: list[dict] = []
    previous = Path(prev_snapshot) / "overlay" / "capability_rules.json" if prev_snapshot else None
    if previous and previous.is_file():
        try:
            stamped = [row for row in json.loads(previous.read_text()).get("stamped", []) if isinstance(row, dict)]
        except (OSError, ValueError):
            stamped = []
    replaced = set(replacements)
    stamped = [row for row in stamped if str(row.get("app") or "") not in replaced]
    for app_id, package in replacements.items():
        rules = json.loads((package / "capability_rules.json").read_text()).get("stamped", [])
        for row in rules:
            if isinstance(row, dict):
                copy = dict(row)
                copy.setdefault("app", app_id)
                stamped.append(copy)
    overlay = Path(day_dir) / "overlay"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "capability_rules.json").write_text(json.dumps({"stamped": stamped}, indent=2, ensure_ascii=False) + "\n")


def _required_migrations(package: Path, previous_revision: int | None, selected_revision: int) -> list[str]:
    if previous_revision is None or previous_revision >= selected_revision:
        return []
    required = []
    for migration in _migration_files(package):
        match = re.fullmatch(r"revision_(\d{4})_to_(\d{4})\.py", migration.name)
        if not match:
            continue
        source_revision, target_revision = int(match.group(1)), int(match.group(2))
        if previous_revision <= source_revision and target_revision <= selected_revision:
            required.append(migration.relative_to(package).as_posix())
    return sorted(required)


def prepare_day_apps(profile_id: str, day: int, manifest_path: Path, day_dir: Path, prev_snapshot: Path | None) -> list[PreparedApp]:
    selections = load_plan_app_selections(manifest_path)
    prepared: list[PreparedApp] = []
    replacements: dict[str, Path] = {}
    for app_id, selection in selections.items():
        previous_revision = _world_app_revision(prev_snapshot, app_id)
        if previous_revision == selection.revision:
            continue
        change = "new" if previous_revision is None else "upgrade"
        target = Path(day_dir) / "overlay" / "apps" / app_id
        _copy_package(selection.path, target)
        marker = {
            "schema_version": 1,
            "id": app_id,
            "revision": selection.revision,
            "content_sha256": selection.content_sha256,
            "policy": selection.policy,
            "migration_files": _required_migrations(selection.path, previous_revision, selection.revision),
            "local_patch": False,
        }
        (target / REVISION_MARKER).write_text(json.dumps(marker, indent=2, ensure_ascii=False) + "\n")
        replacements[app_id] = target
        prepared.append(PreparedApp(app_id, selection.revision, selection.content_sha256, selection.policy, change, previous_revision))
    if replacements:
        _replace_capability_rules(Path(day_dir), prev_snapshot, replacements)
    return prepared


def finalize_day_apps(profile_id: str, day: int, manifest_path: Path, day_dir: Path, prev_snapshot: Path | None, prepared: list[PreparedApp]) -> list[str]:
    day_dir = Path(day_dir)
    produces_path = day_dir / "produces.json"
    produces = json.loads(produces_path.read_text())
    declaration = produces.setdefault("overlay", {})
    new_apps = set(declaration.get("new_apps") or [])
    code_patches = set(declaration.get("code_patches") or [])
    prepared_by_id = {item.app_id: item for item in prepared}
    for item in prepared:
        target = day_dir / "overlay" / "apps" / item.app_id
        problems = app_problems(target, item.app_id, forbidden_terms=_profile_terms(profile_id))
        if problems:
            raise RuntimeError(f"prepared app {item.app_id} revision {item.revision} is invalid: {problems}")
        marker_path = target / REVISION_MARKER
        marker = json.loads(marker_path.read_text())
        if item.change == "upgrade":
            delta_text = (day_dir / "delta.sh").read_text(errors="replace")
            missing_migrations = [name for name in marker.get("migration_files", []) if name not in delta_text]
            if missing_migrations:
                raise RuntimeError(f"delta.sh does not invoke required migrations for {item.app_id}: {missing_migrations}")
        changed = _sha256_tree(target) != item.content_sha256
        if changed and item.policy == "shared":
            raise RuntimeError(f"day implementation modified shared-policy app {item.app_id}; publish reusable capability changes through app_creator or mark the plan local")
        marker["local_patch"] = changed
        marker_path.write_text(json.dumps(marker, indent=2, ensure_ascii=False) + "\n")
        if item.change == "new":
            new_apps.add(item.app_id)
        else:
            code_patches.add(f"mcp:{item.app_id}")
    apps_out = day_dir / "overlay" / "apps"
    if apps_out.is_dir():
        active = active_world_apps(prev_snapshot)
        for package in apps_out.iterdir():
            if package.is_dir() and package.name in active and package.name not in prepared_by_id:
                code_patches.add(f"mcp:{package.name}")
                selection = load_plan_app_selections(manifest_path).get(package.name)
                if selection is not None:
                    marker = {
                        "schema_version": 1,
                        "id": package.name,
                        "revision": selection.revision,
                        "content_sha256": selection.content_sha256,
                        "policy": selection.policy,
                        "migration_files": [],
                        "local_patch": True,
                    }
                    (package / REVISION_MARKER).write_text(json.dumps(marker, indent=2, ensure_ascii=False) + "\n")
    declaration["new_apps"] = sorted(new_apps)
    declaration["code_patches"] = sorted(code_patches)
    produces_path.write_text(json.dumps(produces, indent=2, ensure_ascii=False) + "\n")
    return sorted(item.app_id for item in prepared if item.change == "new")


def stage_new_apps(profile_id: str, day: int, manifest_path: Path, day_dir: Path, prev_snapshot: Path | None) -> list[str]:
    prepared = prepare_day_apps(profile_id, day, manifest_path, day_dir, prev_snapshot)
    return finalize_day_apps(profile_id, day, manifest_path, day_dir, prev_snapshot, prepared)


def record_usage(profile_id: str, day: int, manifest_path: Path) -> None:
    usage_by_app = load_plan_app_selections(manifest_path)
    for app_id, selection in usage_by_app.items():
        usage = {"profile": profile_id, "day": day, "revision": selection.revision}
        with _library_lock():
            path = meta_path(app_id)
            if not path.is_file():
                raise RuntimeError(f"cannot record usage for missing shared app {app_id}")
            meta = load_meta(app_id)
            uses = list(meta.get("uses") or [])
            if usage not in uses:
                uses.append(usage)
            meta["uses"] = uses
            _atomic_write(path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
