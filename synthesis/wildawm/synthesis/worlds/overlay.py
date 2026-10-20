"""Per-world code overlay: new or patched MCP apps and WebHarbor site patches."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.webharbor import control as wh_control

APPS_SUBDIR = "apps"
WEBHARBOR_SUBDIR = "webharbor"
CAPABILITY_RULES_FILE = "capability_rules.json"

# Overlay apps are eval-time servers: only clearly dangerous imports are forbidden.
_OVERLAY_FORBIDDEN_IMPORTS = {"subprocess", "docker", "git"}
_OVERLAY_FORBIDDEN_STRINGS = (
    "worlds/_master", "/worlds/_master", "tasks_xai/", "/home/",
    "git push", "git commit", "docker build", "docker commit", "docker volume",
)


def overlay_dir(root: Path | str) -> Path:
    return Path(root) / "overlay"


def has_overlay(root: Path | str) -> bool:
    od = overlay_dir(root)
    return od.is_dir() and any(od.iterdir())


def list_overlay_apps(od: Path | str) -> list[str]:
    apps = overlay_dir(od) / APPS_SUBDIR
    if not apps.is_dir():
        return []
    return sorted(d.name for d in apps.iterdir() if d.is_dir() and _looks_like_app(d))


def list_overlay_webharbor_sites(od: Path | str) -> list[str]:
    wh = overlay_dir(od) / WEBHARBOR_SUBDIR
    if not wh.is_dir():
        return []
    return sorted(d.name for d in wh.iterdir() if d.is_dir())


def _looks_like_app(d: Path) -> bool:
    return (d / "run.py").exists() or (d / "handlers.py").exists() or (d / "models.py").exists()


def merge_overlay(src_overlay: Path | str, dest_root: Path | str) -> Path | None:
    """Merge an overlay dir into ``<dest_root>/overlay`` (last write wins); None if absent."""
    src_overlay = Path(src_overlay)
    if not src_overlay.is_dir():
        return None
    dest_overlay = overlay_dir(dest_root)
    dest_overlay.mkdir(parents=True, exist_ok=True)
    _copytree_merge(src_overlay, dest_overlay)
    return dest_overlay


def _copytree_merge(src: Path, dest: Path) -> None:
    """Merge ``src`` into an existing ``dest``, preserving symlinks."""
    shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)


def accumulate_overlay(
    prev_overlay: Path | str | None,
    day_overlay: Path | str | None,
    dest_overlay: Path | str,
) -> Path:
    """Cumulative overlay for day N: earlier days plus day N, day N winning on conflict."""
    dest_overlay = Path(dest_overlay)
    if dest_overlay.exists():
        shutil.rmtree(dest_overlay)
    dest_overlay.mkdir(parents=True)
    if prev_overlay and Path(prev_overlay).is_dir():
        _copytree_merge(Path(prev_overlay), dest_overlay)
    if day_overlay and Path(day_overlay).is_dir():
        _copytree_merge(Path(day_overlay), dest_overlay)
    return dest_overlay


def _docker_cp_file(host_src: Path, container: str, container_dest: str) -> None:
    parent = str(Path(container_dest).parent)
    subprocess.run(
        ["docker", "exec", container, "mkdir", "-p", parent],
        capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S, check=False,
    )
    subprocess.run(
        ["docker", "cp", str(host_src), f"{container}:{container_dest}"],
        capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S, check=True,
    )


def apply_runtime(
    world_dir: Path | str,
    *,
    webharbor_container: str | None = None,
    control_port: int = 8101,
) -> dict:
    """Copy overlay WebHarbor patches into the live container and restart patched sites."""
    od = overlay_dir(world_dir)
    out: dict = {"applied": [], "restarted": [], "errors": []}
    if not webharbor_container or not od.is_dir():
        return out
    for site in list_overlay_webharbor_sites(world_dir):
        site_src = od / WEBHARBOR_SUBDIR / site
        try:
            for f in sorted(site_src.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(site_src).as_posix()
                    _docker_cp_file(f, webharbor_container, f"/opt/WebSyn/{site}/{rel}")
            restarted = wh_control.restart_site(webharbor_container, site)
            out["applied"].append(site)
            if restarted:
                out["restarted"].append(site)
            else:
                out["errors"].append(f"{site}: restart did not become ready (patched code may be invalid)")
        except Exception as e:
            out["errors"].append(f"{site}: {e}")
    return out


def capability_rules_path(od: Path | str) -> Path:
    return overlay_dir(od) / CAPABILITY_RULES_FILE


def load_capability_rules(od: Path | str) -> list[dict]:
    """Read ``overlay/capability_rules.json`` as stamp rows; empty if absent."""
    p = capability_rules_path(od)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return []
    if isinstance(data, dict) and isinstance(data.get("stamped"), list):
        return [r for r in data["stamped"] if isinstance(r, dict)]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def overlay_code_safety_errors(od: Path | str) -> list[str]:
    """AST-lint overlay app code for dangerous imports and forbidden path/operation strings."""
    od = Path(od)
    errors: list[str] = []
    apps = od / APPS_SUBDIR
    if not apps.is_dir():
        return errors
    for sp in sorted(apps.rglob("*.py")):
        try:
            tree = ast.parse(sp.read_text(errors="replace"))
        except SyntaxError as e:
            errors.append(f"{sp.name}: syntax error: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in _OVERLAY_FORBIDDEN_IMPORTS:
                        errors.append(f"{sp.name}: forbidden import `{alias.name}`")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".", 1)[0] in _OVERLAY_FORBIDDEN_IMPORTS:
                    errors.append(f"{sp.name}: forbidden import `{node.module}`")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for frag in _OVERLAY_FORBIDDEN_STRINGS:
                    if frag in node.value:
                        errors.append(f"{sp.name}: forbidden string fragment `{frag}`")
    return errors
