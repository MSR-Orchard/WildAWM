from __future__ import annotations

import re
import uuid
from pathlib import Path

from . import dockerfile
from .base import LIVE, STOP, ExecResult, Sandbox, SandboxError
from .docker import DockerSandbox
from .dockerfile import Image
from .modal import ModalSandbox
from .orchard import OrchardSandbox

__all__ = ["ExecResult", "Image", "Sandbox", "SandboxError", "DockerSandbox", "ModalSandbox", "OrchardSandbox", "open_sandbox", "open_dockerfile_sandbox", "endpoint", "close_all"]


def open_sandbox(cfg, image: str, *, name: str | None = None, cpu=None, memory=None, data: dict | None = None, **docker_options) -> Sandbox:
    """Create a sandbox on the configured backend; read-only `data` {remote: local} is bind-mounted or uploaded."""
    if STOP.is_set():
        raise SandboxError("evaluation is stopping")
    options = cfg.sandbox
    cpu = cpu or options.get("cpu")
    memory = memory or options.get("memory")
    base = re.sub(r"[^a-z0-9-]+", "-", f"awm-{cfg.name}-{name or 'task'}".lower())[:40].strip("-")
    name = f"{base}-{uuid.uuid4().hex[:6]}"
    backend = options.get("backend", "docker")
    data = {remote: Path(local).resolve() for remote, local in (data or {}).items()}
    if backend == "docker" and (options.get("docker") or {}).get("bind_data", True):
        docker_options["volumes"] = [*docker_options.get("volumes", ()), *(f"{local}:{remote}:ro" for remote, local in data.items())]
        data = {}
    if backend == "docker":
        sandbox = DockerSandbox(image, name=name, cpu=cpu, memory=memory, **docker_options)
    elif backend == "orchard":
        orchard = options.get("orchard") or {}
        sandbox = OrchardSandbox(image, base_url=orchard.get("base_url", "http://orchard-orchestrator"), api_key=orchard.get("api_key", ""), name=name, cpu=cpu, memory=memory,
                                 create_timeout=orchard.get("create_timeout", 1800), loopback_host=orchard.get("endpoint_host"))
    elif backend == "modal":
        modal = options.get("modal") or {}
        sandbox = ModalSandbox(image, base_url=modal.get("base_url"), api_key=modal.get("api_key"), name=name, cpu=cpu, memory=memory,
                               create_timeout=modal.get("create_timeout", 1800), heartbeat=modal.get("heartbeat", 30),
                               request_timeout=modal.get("request_timeout", 1200), loopback_host=modal.get("endpoint_host"))
    else:
        raise ValueError(f"unknown sandbox backend: {backend} (use docker, orchard or modal)")
    LIVE.add(sandbox)
    try:
        if STOP.is_set():
            raise SandboxError("evaluation is stopping")
        for remote, local in {**(options.get("files") or {}), **data}.items():
            sandbox.upload(local, remote)
        for command in options.get("setup") or []:
            sandbox.exec(command, timeout=600).check("sandbox setup")
    except BaseException:
        sandbox.__exit__()
        raise
    return sandbox


def open_dockerfile_sandbox(cfg, context, *, name: str | None = None, cpu=None, memory=None, timeout: int = 3600) -> tuple[Sandbox, Image]:
    """Sandbox for a Docker build context: built by docker, replayed on the FROM image elsewhere."""
    context = Path(context).resolve()
    base, steps = dockerfile.recipe(context / "Dockerfile")
    if cfg.sandbox.get("backend", "docker") == "docker":
        tag = dockerfile.build_image(context, f"awmeval-{cfg.name}", timeout=timeout)
        image = dockerfile.apply(base, steps)
        image.env = {}  # baked into the built image
        return open_sandbox(cfg, tag, name=name, cpu=cpu, memory=memory), image
    sandbox = open_sandbox(cfg, base, name=name, cpu=cpu, memory=memory)
    try:
        return sandbox, dockerfile.apply(base, steps, sandbox, context, timeout=timeout)
    except BaseException:
        sandbox.__exit__()
        raise


def close_all() -> None:
    """Stop creating sandboxes and remove every live one (used on interrupt)."""
    STOP.set()
    for sandbox in list(LIVE):
        sandbox.__exit__()


def endpoint(sandbox: Sandbox, section) -> str:
    """Base URL of a model section as seen from inside the sandbox."""
    return section.get("sandbox_base_url") or sandbox.url(section.base_url)
