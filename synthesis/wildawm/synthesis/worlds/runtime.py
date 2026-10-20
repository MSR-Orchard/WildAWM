"""Shared runtime helpers: hermetic delta.sh replay, live-world boot and the creator container."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.worlds import incontainer
from wildawm.synthesis.worlds import overlay as ov
from wildawm.webharbor import control as wh_control
from wildawm.webharbor import lib as wh_lib

# Overlay-aware gateway image: `docker build -f docker/Dockerfile.mcp -t wildawm-mcp:worlds .`
GATEWAY_IMAGE = "wildawm-mcp:worlds"
GATEWAY_INTERNAL_PORT = cfg.MCP_INTERNAL_PORT


@dataclass
class LiveWorld:
    world_dir: Path
    webharbor_container: str
    mcp_container: str | None = None
    mcp_host_port: int | None = None
    extra_containers: list[str] = field(default_factory=list)


def run_delta_sh(
    day_dir: Path | str,
    world_dir: Path | str,
    *,
    frozen_date: str,
    timeout: int = max(cfg.LOCAL_SCRIPT_TIMEOUT_S * 5, 900),
    image: str = wh_lib.CREATOR_IMAGE,
):
    """Replay a day's ``delta.sh`` over ``world_dir`` offline with a frozen clock."""
    day_dir = Path(day_dir).resolve()
    world_dir = Path(world_dir).resolve()
    delta = day_dir / "delta.sh"
    if not delta.exists():
        raise FileNotFoundError(f"delta.sh missing: {delta}")
    env_ts = f"{frozen_date}T10:00:00"
    import os
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    cmd = [
        "docker", "run", "--rm", "--network", "none",
        "--user", uid_gid,
        "-v", f"{world_dir}:/world",
        "-v", f"{day_dir}:/scripts:ro",
        "-w", "/world",
        "-e", f"GIT_AUTHOR_DATE={env_ts}",
        "-e", f"GIT_COMMITTER_DATE={env_ts}",
        "-e", f"AWM_FROZEN_DATE={frozen_date}",
        # The image entrypoint boots the sites; replay only needs bash, and runs as the host uid.
        "--entrypoint", "/bin/bash",
        image,
        "/scripts/delta.sh",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _free_host_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _gateway_container_name(world_dir: Path) -> str:
    import re
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", str(Path(world_dir).resolve()))[-40:]
    return f"wildawm-mcp-gate-{safe}"


def _webharbor_gate_container_name(world_dir: Path) -> str:
    """Per-world webharbor sidecar name, derived from the full path so parallel gates cannot collide."""
    import re
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", str(Path(world_dir).resolve()))[-40:]
    return f"wildawm-creator-{safe}"


def _wait_gateway_health(host_port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{host_port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


def boot_live_world(
    world_dir: Path | str,
    *,
    fresh: bool = True,
    with_mcp: bool = True,
    load_sites: list[str] | None = None,
) -> LiveWorld:
    """Boot a webharbor sidecar and overlay-aware MCP gateway, then apply overlay site patches."""
    world_dir = Path(world_dir).resolve()
    # Name the sidecar from the full path: gate worlds share the basename "A" and would collide.
    wh_name = wh_control.boot(world_dir, fresh=fresh, load_sites=load_sites,
                              name=_webharbor_gate_container_name(world_dir))
    mcp_name = None
    mcp_host_port = None
    if with_mcp:
        mcp_name = _gateway_container_name(world_dir)
        last_error = ""
        for _attempt in range(3):
            mcp_host_port = _free_host_port()
            subprocess.run(["docker", "rm", "-f", mcp_name],
                           capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
            run_cmd = [
                "docker", "run", "-d", "--name", mcp_name,
                "--label", "wildawm-ws-gate=1",
                "-v", f"{world_dir / 'data'}:/data",
                "-v", f"{world_dir / 'overlay'}:/overlay:ro",
                "-p", f"{mcp_host_port}:{GATEWAY_INTERNAL_PORT}",
                GATEWAY_IMAGE,
                "--world_dir", "/data", "--extra_apps_dir", "/overlay/apps",
            ]
            r = subprocess.run(run_cmd, capture_output=True, text=True,
                              timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
            if r.returncode == 0:
                break
            last_error = r.stderr.strip()
            # Remove a half-created container so the retry can reuse the deterministic name.
            subprocess.run(["docker", "rm", "-f", mcp_name],
                           capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        else:
            wh_control.teardown(wh_name)
            raise RuntimeError(f"gateway run failed after 3 port attempts: {last_error}")
        if not _wait_gateway_health(mcp_host_port):
            wh_control.teardown(wh_name)
            subprocess.run(["docker", "rm", "-f", mcp_name], capture_output=True,
                           timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
            raise RuntimeError(f"gateway {mcp_name} did not become healthy")
    if ov.has_overlay(world_dir):
        ov.apply_runtime(world_dir, webharbor_container=wh_name)
    return LiveWorld(world_dir=world_dir, webharbor_container=wh_name,
                     mcp_container=mcp_name, mcp_host_port=mcp_host_port)


def teardown_live_world(lw: LiveWorld) -> None:
    if lw.webharbor_container:
        try:
            wh_control.teardown(lw.webharbor_container)
        except Exception:
            pass
    if lw.mcp_container:
        subprocess.run(["docker", "rm", "-f", lw.mcp_container],
                       capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    for c in lw.extra_containers:
        subprocess.run(["docker", "rm", "-f", c],
                       capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)


def gateway_apps(host_port: int) -> dict:
    """Introspect a booted gate gateway: {available, loaded, errors}."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{host_port}/apps", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _external_bind_mount(path: Path | str) -> list[str]:
    """Mount an external runtime path at its absolute path inside the creator."""
    real = Path(path).resolve()
    project_root = Path(cfg.PROJECT_ROOT).resolve()
    if real == project_root or project_root in real.parents:
        return []
    return ["-v", f"{real}:{real}"]


def _world_stage_extra_mounts() -> list[str]:
    """Extra host paths that world-stage creator agents may chdir/read/write."""
    mounts: list[str] = []
    seen: set[str] = set()
    for path in (cfg.WORLDS_DIR, cfg.SCRATCH):
        # Create mount sources as the invoking user; Docker would create them owned by root.
        real = Path(path).resolve()
        real.mkdir(parents=True, exist_ok=True)
        if not os.access(real, os.W_OK | os.X_OK):
            raise PermissionError(f"creator bind-mount source is not writable: {real}")
        extra = _external_bind_mount(real)
        if not extra:
            continue
        src = extra[1].split(":", 1)[0]
        if src in seen:
            continue
        seen.add(src)
        mounts += extra
    return mounts


def ensure_creator(worktree: Path | str, *, fresh: bool = False) -> str:
    """Boot (or reuse) the creator container with the worktree mounted."""
    return incontainer.ensure_container(
        worktree, fresh=fresh, extra_mounts=_world_stage_extra_mounts())


def teardown_creator(worktree: Path | str) -> None:
    # Opt-in: keep one creator container per run instead of churning the docker daemon per stage.
    if os.environ.get("AWM_KEEP_CREATOR"):
        return
    incontainer.teardown(worktree)


def run_stage_agent(
    prompt: str,
    *,
    role: str,
    container: str,
    cwd: str,
    constitution: Path | str | None,
    timeout: int,
    max_turns: int | None = None,
    done_check=None,
    vision: bool = True,
    env_extra: dict | None = None,
    retries: int = 2,
):
    """Run a world-stage role inside the creator container via :func:`harness.run_agent`."""
    return harness.run_agent(
        prompt, role=role, timeout=timeout, container=container, cwd=cwd,
        workdir=cwd,
        system_prompt_file=str(constitution) if constitution else None,
        max_turns=max_turns, done_check=done_check, vision=vision,
        retries=retries, env_extra=env_extra,
    )


def run_host_judge(prompt: str, *, role: str, timeout: int | None = None,
                   retries: int = 2) -> str:
    """One host-side chat call for text-verdict roles; returns the assistant text."""
    spec = cfg.model_spec(cfg.OPENCODE_LLM_REF) if not _role_model_ref(role) else cfg.model_spec(_role_model_ref(role))
    rec = cr.http_chat(spec, [{"role": "user", "content": prompt}],
                       role=role, timeout=timeout or cfg.LLM_SHORT_TIMEOUT_S, retries=retries)
    return rec.result_text or ""


def _role_model_ref(role: str) -> str:
    import os
    return os.environ.get(f"AWM_SYNTH_{role.upper()}_MODEL", "")
