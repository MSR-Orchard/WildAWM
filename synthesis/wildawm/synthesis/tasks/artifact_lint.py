#!/usr/bin/env python3
"""Structural checks for generated task packages; copied into containers, so imports stay optional."""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from wildawm.json_utils import loads_jsonc
except ModuleNotFoundError:
    try:
        import json_repair

        def loads_jsonc(text: str):
            return json_repair.loads(text)
    except ModuleNotFoundError:
        def loads_jsonc(text: str):
            return json.loads(text)

try:
    from wildawm import const_config as _cfg
except ModuleNotFoundError:
    _cfg = None

PUBLIC_TASK_WORD_LIMIT = 300
_REQUIRED_INTERFACES = {"cli", "mcp", "web"}
_WEB_ACTION_TYPES = {"read_only", "browser_write_flow"}
_WRITE_RUN_RE = re.compile(
    r"\b(?:write|create|build|implement|make)\b[^.\n]{0,160}\b(?:script|program|python|node(?:\.js)?|command[- ]line tool)\b|"
    r"\b(?:script|program|python|node(?:\.js)?|command[- ]line tool)\b[^.\n]{0,160}\b(?:run|execute)\b|"
    r"\b(?:run|execute)\b[^.\n]{0,160}\b(?:script|program|python|node(?:\.js)?|command[- ]line tool)\b",
    re.I,
)
_VISUAL_PUBLIC_RE = re.compile(
    r"\b(?:image|photo|picture|visual|visible|appearance|look(?:s|ing)?|shown|see|inspect)\b",
    re.I,
)
_INTERNAL_PATH_RE = re.compile(
    r"(?:/tmp/_xai_snap|/workspace/(?:data|webharbor)|/worlds/_master|"
    r"(?:^|[\s`'\"])/(?:data|webharbor)/[^\s`'\"]+)",
    re.I,
)
_HOME_PATH_RE = re.compile(r"(?:~(?:/|\b)|\$HOME(?:/|\b)|/home/[^\s`'\"]+)", re.I)
_RAW_ID_RE = re.compile(r"\b[a-z][a-z0-9_]*_id\s*(?:==|=|!=|<>|>|<)\s*\d+\b", re.I)
_RAW_KEY_COLUMN_RE = re.compile(
    r"^(?:[a-z][a-z0-9_]*\.)?(?:id|rowid|[a-z][a-z0-9_]*_id)$", re.I)
_RAW_KEY_VALUE_RE = re.compile(
    r"^\s*(?:(?:[a-z][a-z0-9_]*_)?id\s*[:=#]?\s*)?\d+\s*$", re.I)
_PENDING_RE = re.compile(
    r"\b(?:PENDING(?:_VISION_VERIFY)?|TODO|TBD|NEEDS\s+VISION|UNKNOWN|UNSURE)\b",
    re.I,
)
_REL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\.?/?(?:Documents|Downloads|Desktop|Notes|work|repos|data|inputs?)/[^\s`'\",;:()\[\]]+|[A-Za-z0-9_.+-]+\.(?:csv|json|jsonl|md|txt|tsv|yaml|yml|ics|log))",
    re.I,
)
_FORBIDDEN_WORLD_PATCH_IMPORTS = {
    "subprocess", "socket", "urllib", "requests", "httpx", "docker", "git",
}
_FORBIDDEN_PATCH_FRAGMENTS = (
    "worlds/_master", "/worlds/_master", "tasks_xai/", "/home/",
    "git commit", "git push", "docker build", "docker commit", "docker volume",
)


def _image_required() -> bool:
    if _cfg is not None:
        return bool(getattr(_cfg, "XAI_REQUIRE_IMAGE_DECISIVE", True))
    value = os.environ.get("AWM_XAI_REQUIRE_IMAGE_DECISIVE", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _read_meta(task_dir: Path | str) -> dict:
    path = Path(task_dir) / "meta.json"
    if not path.is_file():
        return {}
    try:
        value = loads_jsonc(path.read_text(errors="replace"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _normalise_interface(value: Any) -> str:
    text = str(value or "").strip().lower().split(":", 1)[-1].replace("_", "-")
    aliases = {
        "browser": "web", "website": "web", "web-gui": "web",
        "shell": "cli", "terminal": "cli", "filesystem": "cli",
        "app": "mcp", "apps": "mcp", "mcp-app": "mcp", "mcp-apps": "mcp",
    }
    return aliases.get(text, text)


def _interfaces(value: Any) -> set[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {item for item in (_normalise_interface(v) for v in values) if item}


def _normalise_mcp_domain(value: Any) -> str:
    text = re.sub(r"^mcp[:/\s-]+", "", str(value or "").strip().lower())
    return text.replace(".", "_").replace("-", "_").replace(" ", "_")


def normalize_mcp_domains(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [_normalise_mcp_domain(v) for v in values if str(v or "").strip()]


def mcp_domain_problems(value: Any, *, where: str) -> list[str]:
    domains = normalize_mcp_domains(value)
    if not domains:
        return [f"{where}: mcp_domains must name at least one active MCP app"]
    return [
        f"{where}: invalid MCP app id `{domain}`; use the active snake_case service id"
        for domain in domains
        if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", domain) is None
    ]


def active_mcp_domain_problems(task_dir: Path | str, snapshot_dir: Path | str) -> list[str]:
    meta = _read_meta(task_dir)
    snapshot = Path(snapshot_dir)
    apps_dir = snapshot / "overlay" / "apps"
    data_dir = snapshot / "data"
    generated = {p.name for p in apps_dir.iterdir() if p.is_dir()} if apps_dir.is_dir() else set()
    active = {p.stem for p in data_dir.glob("*.db") if p.stem in generated} if data_dir.is_dir() else set()
    requested = set(normalize_mcp_domains(meta.get("mcp_domains")))
    missing = sorted(requested - active)
    if not missing:
        return []
    return [
        f"meta.json: mcp_domains names services absent from this world: {missing}; active services are {sorted(active)}"
    ]


def flow_interfaces(flow: str, *, mcp_domains=None) -> set[str]:
    found = {
        _normalise_interface(token)
        for token in re.findall(
            r"\b(cli|mcp|web|browser|shell|terminal|filesystem)\b",
            str(flow or ""), re.I,
        )
    }
    if normalize_mcp_domains(mcp_domains) and "mcp" in str(flow or "").lower():
        found.add("mcp")
    return found


_FLOW_SPLIT_RE = re.compile(r"-+>|→|=>|»|\||;|\n")
_FLOW_TOKEN_RE = re.compile(r"\b(cli|mcp|web|browser|shell|terminal|filesystem)\b", re.I)


def flow_sequence(flow: str) -> list[str]:
    sequence: list[str] = []
    for segment in _FLOW_SPLIT_RE.split(str(flow or "")):
        match = _FLOW_TOKEN_RE.search(segment)
        if match:
            sequence.append(_normalise_interface(match.group(1)))
    if len(sequence) >= 2:
        return sequence
    scanned = [
        _normalise_interface(match.group(1))
        for match in _FLOW_TOKEN_RE.finditer(str(flow or ""))
    ]
    collapsed = [
        interface for index, interface in enumerate(scanned)
        if index == 0 or scanned[index - 1] != interface
    ]
    return collapsed if len(collapsed) > len(sequence) else sequence


def flow_shape(flow: str) -> str:
    """Canonical observed interface sequence for reporting only."""
    return ">".join(flow_sequence(flow))


def _image_grounding_problems(grounding: Any, *, where: str) -> list[str]:
    if grounding is None and not _image_required():
        return []
    if not isinstance(grounding, dict):
        return [f"{where}: image_grounding must be a non-null object while image-decisive mode is enabled"]
    aliases = {
        "site": ("site", "url", "domain"),
        "attribute": ("attribute", "visual_attribute", "image_type", "type"),
        "truth": ("truth", "ground_truth", "answer", "target"),
        "distractor": ("distractor", "distractors", "contrast"),
    }
    return [
        f"{where}: image_grounding missing {label}"
        for label, keys in aliases.items()
        if not any(grounding.get(key) for key in keys)
    ]


def _cli_code_step_problems(meta: dict, *, where: str) -> list[str]:
    step = meta.get("cli_code_step")
    if not isinstance(step, dict):
        return [f"{where}: cli_code_step must describe the required script/program"]
    problems: list[str] = []
    if step.get("requires_code") is False:
        problems.append(f"{where}: cli_code_step.requires_code must not be false")
    purpose = str(step.get("purpose") or "").strip()
    downstream = str(step.get("downstream_use") or "").strip()
    inputs = step.get("input_sources")
    if not purpose:
        problems.append(f"{where}: cli_code_step.purpose must describe a real script/program computation")
    if not isinstance(inputs, list) or not any(str(v or "").strip() for v in inputs):
        problems.append(f"{where}: cli_code_step.input_sources must name a real workspace source or MCP export")
    elif any(_INTERNAL_PATH_RE.search(str(value or ""))
             or _HOME_PATH_RE.search(str(value or ""))
             for value in inputs):
        problems.append(f"{where}: cli_code_step.input_sources must use user-visible workspace sources, not internal or home paths")
    if not downstream:
        problems.append(f"{where}: cli_code_step.downstream_use must explain how the result drives later MCP/Web work")
    artifact = step.get("output_artifact")
    if artifact:
        value = str(artifact).strip()
        if value.startswith(("/", "~", "$HOME")) or ".." in Path(value).parts:
            problems.append(f"{where}: cli_code_step.output_artifact must be a clean workspace-relative path when provided")
    return problems


def _web_action_problems(meta: dict, *, where: str) -> list[str]:
    action_type = str(meta.get("web_action_type") or "").strip().lower()
    if action_type not in _WEB_ACTION_TYPES:
        return [f"{where}: web_action_type must be one of {sorted(_WEB_ACTION_TYPES)}"]
    surface = meta.get("web_write_surface")
    if action_type == "read_only":
        return []
    if not isinstance(surface, dict):
        return [f"{where}: browser_write_flow requires web_write_surface"]
    missing = [key for key in ("site", "action", "state_table") if not str(surface.get(key) or "").strip()]
    problems = [f"{where}: web_write_surface missing {key}" for key in missing]
    table = str(surface.get("state_table") or "")
    if "." in table or "/" in table:
        problems.append(f"{where}: web_write_surface.state_table must be a bare live-discovered table name")
    return problems


def _natural_key_contract_problems(record: dict, *, where: str) -> list[str]:
    problems: list[str] = []
    surface = record.get("web_write_surface")
    if isinstance(surface, dict):
        key_column = str(surface.get("natural_key") or "").strip()
        target_key = str(surface.get("target_natural_key") or "").strip()
        if key_column and _RAW_KEY_COLUMN_RE.fullmatch(key_column):
            problems.append(
                f"{where}: web_write_surface.natural_key `{key_column}` is a "
                "fixture/FK id column; use a live user-facing slug, title, URL, "
                "email, name, or another nonnumeric natural key")
        if target_key and _RAW_KEY_VALUE_RE.fullmatch(target_key):
            problems.append(
                f"{where}: web_write_surface.target_natural_key `{target_key}` "
                "is a raw numeric fixture id; use the exact nonnumeric natural-key value")
    grounding = record.get("image_grounding")
    if isinstance(grounding, dict):
        for field in ("asset_key", "natural_key", "slug", "handle", "permalink"):
            value = str(grounding.get(field) or "").strip()
            if value and _RAW_KEY_VALUE_RE.fullmatch(value):
                problems.append(
                    f"{where}: image_grounding.{field} `{value}` is a raw numeric fixture id")
    values = record.get("natural_keys") or []
    if not isinstance(values, list):
        values = [values]
    for value in values:
        text = str(value or "").strip()
        if text and _RAW_KEY_VALUE_RE.fullmatch(text):
            problems.append(
                f"{where}: natural_keys contains raw numeric fixture id `{text}`")
    return problems


def sketch_xai_contract_problems(sketch: dict) -> list[str]:
    problems: list[str] = []
    missing = sorted(_REQUIRED_INTERFACES - _interfaces(sketch.get("interfaces")))
    if missing:
        problems.append(f"sketch missing required interfaces: {', '.join(missing)}")
    flow = sketch.get("interface_flow") or sketch.get("flow") or ""
    if not _REQUIRED_INTERFACES.issubset(flow_interfaces(flow, mcp_domains=sketch.get("mcp_domains"))):
        problems.append("sketch flow must name cli, mcp, and web")
    for field in ("archetype", "sites", "mcp_domains"):
        if not sketch.get(field):
            problems.append(f"sketch missing {field}")
    problems.extend(mcp_domain_problems(sketch.get("mcp_domains"), where="sketch"))
    problems.extend(_image_grounding_problems(sketch.get("image_grounding"), where="sketch"))
    problems.extend(_cli_code_step_problems(sketch, where="sketch"))
    problems.extend(_web_action_problems(sketch, where="sketch"))
    problems.extend(_natural_key_contract_problems(sketch, where="sketch"))
    if _PENDING_RE.search(json.dumps(sketch, ensure_ascii=False)):
        problems.append("sketch contains an unresolved internal marker")
    return problems


def sketch_workspace_input_problems(
    sketch: dict,
    workspace_files: set[str] | None = None,
    *,
    where: str = "sketch",
) -> list[str]:
    """Validate explicit pre-existing path claims without judging their topic."""
    if workspace_files is None:
        return []
    step = sketch.get("cli_code_step")
    if not isinstance(step, dict) or not isinstance(step.get("input_sources"), list):
        return []
    known = {str(p).lstrip("./") for p in workspace_files}
    problems: list[str] = []
    for source in step["input_sources"]:
        text = str(source or "").strip()
        if not text or "MCP" in text.upper() or "export" in text.lower():
            continue
        path = text.lstrip("./")
        if _REL_PATH_RE.fullmatch(text) and path not in known:
            problems.append(f"{where}: cli_code_step.input_sources names `{text}`, which is absent from the clean workspace")
        if _INTERNAL_PATH_RE.search(text):
            problems.append(f"{where}: cli_code_step.input_sources must not expose an internal snapshot or site database path")
    return problems


def sketch_visual_evidence_problems(sketch: dict, *, where: str = "sketch") -> list[str]:
    """Check the visual schema only; the Agent Judge assesses visual semantics."""
    return _image_grounding_problems(sketch.get("image_grounding"), where=where)


def public_task_length_problems(task_dir: Path | str, *, limit: int = PUBLIC_TASK_WORD_LIMIT) -> list[str]:
    path = Path(task_dir) / "task.md"
    if not path.is_file():
        return []
    count = len(path.read_text(errors="replace").split())
    return [] if count <= limit else [f"task.md: public prompt is {count} words; limit is {limit} words"]


def public_task_leakage_problems(task_dir: Path | str) -> list[str]:
    path = Path(task_dir) / "task.md"
    if not path.is_file():
        return []
    problems: list[str] = []
    for line_no, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if _INTERNAL_PATH_RE.search(line):
            problems.append(f"task.md:{line_no}: public prompt exposes an internal snapshot/database path")
        if _HOME_PATH_RE.search(line):
            problems.append(f"task.md:{line_no}: solver-created files must use workspace-captured relative paths")
        if _PENDING_RE.search(line):
            problems.append(f"task.md:{line_no}: public prompt contains an unresolved internal marker")
    return problems


def public_answer_leak_problems(task_dir: Path | str) -> list[str]:
    root = Path(task_dir)
    task_path = root / "task.md"
    if not task_path.is_file():
        return []
    public = " ".join(task_path.read_text(errors="replace").lower().split())
    meta = _read_meta(root)
    grounding = meta.get("image_grounding")
    if not isinstance(grounding, dict):
        return []
    problems: list[str] = []
    for key in ("truth", "ground_truth", "answer", "target"):
        value = " ".join(str(grounding.get(key) or "").lower().split())
        if len(value) >= 12 and value in public:
            problems.append(f"task.md: public prompt copies private image_grounding.{key} verbatim")
    return problems


def public_visual_decision_problems(task_dir: Path | str) -> list[str]:
    if not _image_required():
        return []
    root = Path(task_dir)
    task_path = root / "task.md"
    if not task_path.is_file() or not isinstance(_read_meta(root).get("image_grounding"), dict):
        return []
    if _VISUAL_PUBLIC_RE.search(task_path.read_text(errors="replace")):
        return []
    return ["task.md: image-decisive mode requires a public visual inspection criterion"]


def public_cli_code_requirement_problems(task_dir: Path | str) -> list[str]:
    path = Path(task_dir) / "task.md"
    if not path.is_file():
        return []
    if _WRITE_RUN_RE.search(path.read_text(errors="replace")):
        return []
    return ["task.md: public task must require the solver to write and run a script or program"]


def verifier_sdk_api_problems(task_dir: Path | str) -> list[str]:
    root = Path(task_dir)
    scripts = root / "verifier" / "scripts"
    if not scripts.is_dir():
        return []
    allowed_world_methods = {
        "new_rows", "web_new_rows", "file_new", "file_changed",
        "file_changed_delta", "read",
    }
    guarded_primitives = {"new_rows", "web_new_rows"}
    problems: list[str] = []
    for path in sorted(scripts.glob("*.py")):
        if path.name == "_verify_sdk.py":
            continue
        text = path.read_text(errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(root)}: syntax error: {exc}")
            continue
        parents = {
            child: parent for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id != "w":
                continue
            method = node.func.attr
            if method not in allowed_world_methods:
                problems.append(
                    f"{path.relative_to(root)}:{node.lineno}: unsupported verifier SDK API `w.{method}`")
                continue
            if method not in guarded_primitives:
                continue
            guarded = False
            current: ast.AST | None = node
            while current in parents:
                current = parents[current]
                if isinstance(current, ast.Try):
                    guarded = True
                    break
                if (isinstance(current, ast.Call)
                        and isinstance(current.func, ast.Attribute)
                        and isinstance(current.func.value, ast.Name)
                        and current.func.value.id == "r"
                        and current.func.attr in {"check", "add", "add_error"}):
                    guarded = True
                    break
            if not guarded:
                problems.append(
                    f"{path.relative_to(root)}:{node.lineno}: unguarded `w.{method}` can turn missing/corrupt world state into a script crash; use r.check or catch DBError and r.add_error")
        for line_no, line in enumerate(text.splitlines(), 1):
            if _RAW_ID_RE.search(line):
                problems.append(
                    f"{path.relative_to(root)}:{line_no}: fixture-specific integer identity; resolve the live natural key first")
    return problems


def _claimed_paths(text: str) -> list[str]:
    return sorted({m.group(0).strip("`'\".,;:()[]") for m in _REL_PATH_RE.finditer(text or "")})


def _claimed_existing_paths(text: str) -> list[str]:
    claims: set[str] = set()
    context = re.compile(
        r"\b(?:existing|already|provided|available|read|parse|load|open|from|using|in)\b",
        re.I,
    )
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if context.search(sentence):
            claims.update(_claimed_paths(sentence))
    return sorted(claims)


def workspace_input_contract_problems(task_dir: Path | str) -> list[str]:
    root = Path(task_dir)
    task_path = root / "task.md"
    if not task_path.is_file():
        return []
    claimed = _claimed_existing_paths(task_path.read_text(errors="replace"))
    if not claimed:
        return []
    problems: list[str] = []
    scripts = root / "verifier" / "scripts"
    for path in sorted(scripts.glob("*.py")) if scripts.is_dir() else []:
        if path.name == "_verify_sdk.py":
            continue
        text = path.read_text(errors="replace")
        for claim in claimed:
            if "file_new" in text and Path(claim).name in text:
                problems.append(f"{path.relative_to(root)}: task.md claims `{claim}` exists, but verifier checks it as a new file")
    return problems


def workspace_existing_claim_problems(
    task_dir: Path | str,
    workspace_dir: Path | str | None,
) -> list[str]:
    root = Path(task_dir)
    task_path = root / "task.md"
    if not task_path.is_file() or not workspace_dir:
        return []
    workspace = Path(workspace_dir)
    if not workspace.is_dir():
        return []
    problems: list[str] = []
    for claim in _claimed_existing_paths(task_path.read_text(errors="replace")):
        rel = claim.lstrip("./")
        direct = workspace / rel
        if direct.exists():
            continue
        if "/" not in rel and any(workspace.rglob(Path(rel).name)):
            continue
        problems.append(f"task.md claims pre-existing workspace file `{claim}`, but it is absent from the clean workspace")
    return problems


def _patch_script_safety(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError as exc:
        return [f"world_patch/{path.name}: syntax error: {exc}"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [a.name.split(".", 1)[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [(node.module or "").split(".", 1)[0]]
        else:
            imports = []
        for name in imports:
            if name in _FORBIDDEN_WORLD_PATCH_IMPORTS:
                problems.append(f"world_patch/{path.name}: forbidden import `{name}`")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for fragment in _FORBIDDEN_PATCH_FRAGMENTS:
                if fragment in node.value:
                    problems.append(f"world_patch/{path.name}: forbidden string fragment `{fragment}`")
    return problems


def world_patch_layout_problems(task_dir: Path | str) -> list[str]:
    patch = Path(task_dir) / "world_patch"
    if not patch.exists():
        return []
    required = ("manifest.json", "patch_data.py", "README.md")
    if not any((patch / name).exists() for name in required):
        return []
    missing = [name for name in required if not (patch / name).is_file()]
    if missing:
        return [f"world_patch/: incomplete task-local world patch; missing {', '.join(missing)}"]
    data_script = patch / "patch_data.py"
    problems = _patch_script_safety(data_script)
    data_text = data_script.read_text(errors="replace")
    data_args = ("--instance-dir", "--workspace", "--data-dir", "--webharbor-dir")
    if "parse_known_args" not in data_text:
        missing_args = [arg for arg in data_args if arg not in data_text]
        if missing_args:
            problems.append(
                "world_patch/patch_data.py: define the pipeline arguments "
                f"{', '.join(data_args)} or use parse_known_args(); missing "
                f"{', '.join(missing_args)}")
    runtime_script = patch / "patch_runtime.py"
    if runtime_script.is_file():
        problems.extend(_patch_script_safety(runtime_script))
        runtime_text = runtime_script.read_text(errors="replace")
        runtime_args = (
            "--world-id", "--project", "--instance-dir", "--mcp-container",
            "--webharbor-container", "--worldctl-container",
        )
        if "parse_known_args" not in runtime_text:
            missing_args = [arg for arg in runtime_args if arg not in runtime_text]
            if missing_args:
                problems.append(
                    "world_patch/patch_runtime.py: define the pipeline arguments "
                    f"{', '.join(runtime_args)} or use parse_known_args(); missing "
                    f"{', '.join(missing_args)}")
    return problems


def verifier_state_surface_problems(task_dir: Path | str) -> list[str]:
    """Enforce verifier safety; task-local scripts own live schema semantics."""
    root = Path(task_dir)
    scripts = root / "verifier" / "scripts"
    if not scripts.is_dir():
        return ["verifier/scripts directory is missing"]
    candidates = [p for p in scripts.glob("*.py") if p.name != "_verify_sdk.py"]
    if not candidates:
        return ["verifier/scripts must contain at least one task-local verifier"]
    write_sql = re.compile(
        r"\b(?:INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
        r"DROP\s+TABLE|ALTER\s+TABLE|CREATE\s+TABLE|REPLACE\s+INTO)\b",
        re.I,
    )
    problems: list[str] = []
    web_new_rows_seen = False
    web_new_rows_tables: set[str] = set()
    for path in candidates:
        text = path.read_text(errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            if write_sql.search(line) and not line.lstrip().startswith("#"):
                problems.append(f"{path.relative_to(root)}:{line_no}: verifier contains mutating SQL; verification must be read-only")
            if _INTERNAL_PATH_RE.search(line):
                problems.append(f"{path.relative_to(root)}:{line_no}: verifier hard-codes an internal runtime path")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            tree = None
        forbidden_imports: set[str] = set()
        forbidden_calls: set[str] = set()
        for node in (ast.walk(tree) if tree is not None else []):
            if isinstance(node, ast.Import):
                forbidden_imports.update(
                    alias.name.split(".", 1)[0]
                    for alias in node.names
                    if alias.name.split(".", 1)[0] in _FORBIDDEN_WORLD_PATCH_IMPORTS
                )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "").split(".", 1)[0]
                if module in _FORBIDDEN_WORLD_PATCH_IMPORTS:
                    forbidden_imports.add(module)
            elif isinstance(node, ast.Call):
                call_name = (
                    node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if call_name == "web_new_rows":
                    web_new_rows_seen = True
                    table_node = (
                        node.args[1] if len(node.args) >= 2 else
                        next((kw.value for kw in node.keywords if kw.arg == "table"), None)
                    )
                    if isinstance(table_node, ast.Constant) and isinstance(table_node.value, str):
                        web_new_rows_tables.add(table_node.value)
                if isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval"}:
                    forbidden_calls.add(node.func.id)
                elif (isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name)
                      and node.func.value.id == "os"
                      and node.func.attr in {"system", "popen"}):
                    forbidden_calls.add(f"os.{node.func.attr}")
        if forbidden_imports:
            problems.append(
                f"{path.relative_to(root)}: verifier imports execution/network module(s) "
                f"{sorted(forbidden_imports)}; verification must inspect snapshots without running agent code or network clients")
        if forbidden_calls:
            problems.append(
                f"{path.relative_to(root)}: verifier uses dynamic execution call(s) "
                f"{sorted(forbidden_calls)}; verification must be read-only")
    meta = _read_meta(root)
    if str(meta.get("web_action_type") or "").strip().lower() == "browser_write_flow":
        surface = meta.get("web_write_surface")
        table = str(surface.get("state_table") or "") if isinstance(surface, dict) else ""
        if not web_new_rows_seen:
            problems.append("browser_write_flow verifier must check persisted AFTER-state with web_new_rows")
        elif table and table not in web_new_rows_tables:
            seen = sorted(web_new_rows_tables)
            problems.append(
                f"browser_write_flow verifier must call web_new_rows on declared "
                f"live state table `{table}`; statically resolved tables were {seen}")
    return problems


def _normalise_site(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    text = re.sub(r"^https?://", "", text).split("/", 1)[0]
    return text.removeprefix("www.")


def _site_alignment_problems(meta: dict, *, where: str) -> list[str]:
    sites = [_normalise_site(v) for v in (meta.get("sites") or []) if str(v or "").strip()]
    grounding = meta.get("image_grounding")
    if isinstance(grounding, dict) and sites:
        image_site = _normalise_site(grounding.get("site") or grounding.get("url") or grounding.get("domain"))
        if image_site and image_site not in sites:
            return [f"{where}: image_grounding.site `{image_site}` must match a declared live site"]
    return []


def xai_meta_contract_problems(task_dir: Path | str) -> list[str]:
    root = Path(task_dir)
    meta_path = root / "meta.json"
    if not meta_path.is_file():
        return ["meta.json is missing"]
    try:
        meta = loads_jsonc(meta_path.read_text(errors="replace"))
    except Exception as exc:
        return [f"meta.json is invalid: {exc}"]
    if not isinstance(meta, dict):
        return ["meta.json must be an object"]
    problems: list[str] = []
    missing = sorted(_REQUIRED_INTERFACES - _interfaces(meta.get("interfaces")))
    if missing:
        problems.append(f"meta.json: missing required interfaces: {', '.join(missing)}")
    flow = meta.get("interface_flow") or meta.get("flow") or ""
    if not _REQUIRED_INTERFACES.issubset(flow_interfaces(flow, mcp_domains=meta.get("mcp_domains"))):
        problems.append("meta.json: interface_flow must name cli, mcp, and web")
    problems.extend(mcp_domain_problems(meta.get("mcp_domains"), where="meta.json"))
    problems.extend(_image_grounding_problems(meta.get("image_grounding"), where="meta.json"))
    problems.extend(_cli_code_step_problems(meta, where="meta.json"))
    problems.extend(_web_action_problems(meta, where="meta.json"))
    problems.extend(_natural_key_contract_problems(meta, where="meta.json"))
    problems.extend(_site_alignment_problems(meta, where="meta.json"))
    slug = str(meta.get("slug") or "").strip()
    if not slug:
        problems.append("meta.json: slug is required")
    elif slug.startswith(".") or "/" in slug or "\\" in slug:
        problems.append("meta.json: slug must be a clean directory name")
    elif root.name != slug:
        problems.append(
            f"meta.json: slug `{slug}` must match task directory `{root.name}`")
    return problems


def sketch_author_drift_problems(task_dir: Path | str, sketch: dict) -> list[str]:
    meta = _read_meta(task_dir)
    if not meta:
        return ["meta.json is missing or invalid"]
    problems: list[str] = []
    for field in ("title", "interfaces", "mcp_domains", "sites", "web_action_type"):
        expected = sketch.get(field)
        if expected is None:
            continue
        actual = meta.get(field)
        if field in {"interfaces", "mcp_domains", "sites"}:
            expected = sorted(str(v) for v in (expected or []))
            actual = sorted(str(v) for v in (actual or []))
        if expected != actual:
            problems.append(f"meta.json drifted from reserved sketch field `{field}`")
    reserved_flow = sketch.get("interface_flow") or sketch.get("flow")
    if reserved_flow is not None:
        authored_flow = meta.get("interface_flow") or meta.get("flow")
        if flow_sequence(reserved_flow) != flow_sequence(authored_flow):
            problems.append(
                "meta.json drifted from reserved sketch field `interface_flow`")
    reserved_code = sketch.get("cli_code_step")
    authored_code = meta.get("cli_code_step")
    if isinstance(reserved_code, dict):
        if not isinstance(authored_code, dict):
            problems.append("meta.json removed the reserved `cli_code_step`")
        else:
            if reserved_code.get("requires_code") != authored_code.get("requires_code"):
                problems.append(
                    "meta.json cli_code_step changed reserved `requires_code`")
            reserved_output = reserved_code.get("output_artifact")
            if reserved_output is not None and reserved_output != authored_code.get("output_artifact"):
                problems.append(
                    "meta.json cli_code_step changed reserved `output_artifact`")
            reserved_sources = reserved_code.get("input_sources") or []
            authored_sources = authored_code.get("input_sources") or []
            authored_source_text = " ".join(str(value).lower() for value in authored_sources)
            generic_source_tokens = {
                "active", "actual", "current", "data", "file", "input",
                "live", "real", "recent", "source", "the", "user", "visible",
            }
            for source in reserved_sources:
                source_text = str(source).lower()
                path_tokens = re.findall(r"[a-z0-9_.-]+/[a-z0-9_./-]+", source_text)
                source_tokens = {
                    token for token in re.findall(r"[a-z0-9_]+", source_text)
                    if len(token) >= 3 and token not in generic_source_tokens
                }
                token_coverage = (
                    len(source_tokens & set(re.findall(r"[a-z0-9_]+", authored_source_text)))
                    / len(source_tokens)
                    if source_tokens else 1.0
                )
                if (any(path not in authored_source_text for path in path_tokens)
                        or token_coverage < 0.5):
                    problems.append(
                        f"meta.json cli_code_step removed reserved input source `{source}`")
    reserved_grounding = sketch.get("image_grounding")
    authored_grounding = meta.get("image_grounding")
    if _image_required() and isinstance(reserved_grounding, dict):
        if not isinstance(authored_grounding, dict):
            problems.append("meta.json drifted from reserved sketch field `image_grounding`")
        else:
            reserved_site = _normalise_site(
                reserved_grounding.get("site") or reserved_grounding.get("url")
                or reserved_grounding.get("domain"))
            authored_site = _normalise_site(
                authored_grounding.get("site") or authored_grounding.get("url")
                or authored_grounding.get("domain"))
            if reserved_site and reserved_site != authored_site:
                problems.append(
                    "meta.json image_grounding changed the reserved WebHarbor site")
            for field in (
                    "asset_key", "natural_key", "slug", "handle", "permalink",
                    "image", "image_url", "image_path", "target_url"):
                expected = str(reserved_grounding.get(field) or "").strip()
                if expected and expected != str(authored_grounding.get(field) or "").strip():
                    problems.append(
                        f"meta.json image_grounding changed reserved `{field}`")
    reserved_surface = sketch.get("web_write_surface")
    authored_surface = meta.get("web_write_surface")
    if isinstance(reserved_surface, dict):
        if not isinstance(authored_surface, dict):
            problems.append("meta.json removed the reserved web_write_surface")
        else:
            for field in ("site", "state_table"):
                expected = str(reserved_surface.get(field) or "").strip()
                actual = str(authored_surface.get(field) or "").strip()
                if expected and expected != actual:
                    problems.append(
                        f"meta.json web_write_surface changed reserved `{field}`")
    return problems
