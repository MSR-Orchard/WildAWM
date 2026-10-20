"""MCP gateway: serves every WildAWM app from one process, plus a unified /mcp endpoint."""

from dataclasses import dataclass
import hashlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from simpleArgParser import parse_args
from wildawm.const_config import MCP_INTERNAL_PORT

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

@dataclass
class GatewayArgs:
    world_dir: str
    apps: str = ""
    host: str = "0.0.0.0"
    port: int = MCP_INTERNAL_PORT
    # Directory of generated per-world packages: ``<extra_apps_dir>/<app>/``. Each package is served over ``world_dir/<app>.db``. Empty/absent means this world has no MCP implementation.
    extra_apps_dir: str = ""


def parse_gateway_args(argv: list[str] | None = None) -> GatewayArgs:
    if argv is None:
        argv = sys.argv[1:]
    # Accept dashed spellings of the path flags too.
    dashed = {"--world-dir": "--world_dir", "--extra-apps-dir": "--extra_apps_dir"}
    argv = [dashed.get(arg, arg) for arg in argv]
    return parse_args(GatewayArgs, pass_in=argv, disable_cmd=True)


def _overlay_app_ids(extra_apps_dir: str) -> list[str]:
    if not extra_apps_dir:
        return []
    base = Path(extra_apps_dir)
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and ((d / "run.py").exists() or (d / "handlers.py").exists()):
            out.append(d.name)
    return out


def discover_apps(extra_apps_dir: str = "") -> list[str]:
    """Return generated implementations available in the supplied world overlay."""
    return _overlay_app_ids(extra_apps_dir)


def discover_world_apps(world_dir: str, extra_apps_dir: str = "") -> list[str]:
    """Return services that have both a world database and a generated overlay package."""
    root = Path(world_dir)
    generated = set(_overlay_app_ids(extra_apps_dir))
    return sorted(path.stem for path in root.glob("*.db") if path.stem in generated)


def _load_overlay_app(app_id: str, extra_apps_dir: str, db_path: str):
    """Import an overlay app package (run.py, else handlers.py) and call its create_app(db_path)."""
    app_dir = Path(extra_apps_dir) / app_id
    identity = hashlib.sha256(str(app_dir.resolve()).encode()).hexdigest()[:12]
    root_name = "wildawm_generated_apps"
    if root_name not in sys.modules:
        root_package = types.ModuleType(root_name)
        root_package.__path__ = []
        sys.modules[root_name] = root_package
    package_name = f"{root_name}.{app_id}_{identity}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(app_dir)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    for fname, modstem in (("run.py", "run"), ("handlers.py", "handlers")):
        f = app_dir / fname
        if not f.exists():
            continue
        module_name = f"{package_name}.{modstem}"
        spec = importlib.util.spec_from_file_location(module_name, f)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        factory = getattr(mod, "create_app", None)
        if factory is not None:
            return factory(db_path)
    raise RuntimeError(f"overlay app {app_id}: no create_app in run.py/handlers.py")


def create_gateway(world_dir: str, app_ids: list[str] | None = None,
                   *, extra_apps_dir: str = "") -> FastAPI:
    if app_ids is None:
        app_ids = discover_world_apps(world_dir, extra_apps_dir)

    gateway = FastAPI(
        title="WildAWM MCP Gateway",
        description="Unified gateway serving all WildAWM simulated app endpoints.",
        version="0.1.0",
    )

    loaded = []
    errors = []

    for app_id in app_ids:
        db_path = os.path.join(world_dir, f"{app_id}.db")
        try:
            if extra_apps_dir and (Path(extra_apps_dir) / app_id).is_dir():
                sub_app = _load_overlay_app(app_id, extra_apps_dir, db_path)
            else:
                errors.append(f"{app_id}: not in --extra-apps-dir")
                continue
            sub_mcp = FastApiMCP(sub_app)
            sub_mcp.mount_http()
            gateway.mount(f"/{app_id}", sub_app)
            loaded.append(app_id)
        except Exception as e:
            errors.append(f"{app_id}: {e}")

    gateway.state.loaded_apps = loaded
    gateway.state.load_errors = errors
    gateway.state.world_dir = world_dir

    @gateway.get("/health", operation_id="gateway_health", tags=["gateway"])
    async def health():
        return {
            "status": "ok",
            "loaded_apps": loaded,
            "errors": errors,
            "world_dir": world_dir,
            "extra_apps_dir": extra_apps_dir,
        }

    @gateway.get("/apps", operation_id="gateway_list_apps", tags=["gateway"])
    async def list_apps():
        return {
            "available": list(app_ids),
            "loaded": loaded,
            "errors": errors,
        }

    return gateway


def main(argv: list[str] | None = None):
    args = parse_gateway_args(argv)

    app_ids = [a.strip() for a in args.apps.split(",") if a.strip()] if args.apps else None
    os.makedirs(args.world_dir, exist_ok=True)

    gateway = create_gateway(args.world_dir, app_ids, extra_apps_dir=args.extra_apps_dir)

    mcp = FastApiMCP(gateway)
    mcp.mount_http()

    print(f"WildAWM MCP Gateway starting on {args.host}:{args.port}")
    print(f"  World dir: {args.world_dir}")
    print(f"  Loaded apps: {gateway.state.loaded_apps}")
    if gateway.state.load_errors:
        print(f"  Errors: {gateway.state.load_errors}")
    print(f"  MCP endpoint: http://{args.host}:{args.port}/mcp")
    print(f"  Health check: http://{args.host}:{args.port}/health")

    uvicorn.run(gateway, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
