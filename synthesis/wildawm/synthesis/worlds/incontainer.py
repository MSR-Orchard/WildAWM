#!/usr/bin/env python3
"""Creator container lifecycle: boot (or reuse) it with the worktree mounted at its host path."""
import os
import subprocess
from pathlib import Path

from wildawm.core.paths import PROJECT_ROOT
from wildawm.const_config import DOCKER_COMMAND_TIMEOUT_S
from wildawm.webharbor import lib as wh

ROOT = PROJECT_ROOT


def _run(cmd, **kw):
    kw.setdefault("timeout", DOCKER_COMMAND_TIMEOUT_S)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _container_name(world_path: Path) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in Path(world_path).resolve().name)
    name = f"wildawm-claude-{safe}"
    # Per-process suffix so concurrent runs each get their own creator container.
    suffix = os.environ.get("AWM_CREATOR_CONTAINER_SUFFIX", "").strip()
    if suffix:
        suffix = "".join(c if c.isalnum() or c in "-_" else "-" for c in suffix)
        name = f"{name}-{suffix}"
    return name


def ensure_container(world_path: Path | str, *, fresh: bool = False,
                     extra_mounts: list[str] | None = None) -> str:
    """Boot (or reuse) the creator container with the worktree mounted."""
    name = _container_name(world_path)
    if fresh and _running(name):
        _run(["docker", "rm", "-f", name])
    if not _running(name):
        _run(["docker", "rm", "-f", name])
        run_args = ["docker", "run", "-d", "--name", name,
                    "--label", "wildawm-claude=1"]
        run_args += ["-v", f"{ROOT}:{ROOT}"]
        if extra_mounts:
            run_args += list(extra_mounts)
        run_args += ["--entrypoint", "sleep", wh.CREATOR_IMAGE, "infinity"]
        r = _run(run_args)
        if r.returncode != 0:
            raise RuntimeError(f"creator container run failed: {r.stderr.strip()}")
    return name


def _running(name: str) -> bool:
    r = _run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return r.returncode == 0 and r.stdout.strip() == "true"


def teardown(world_path: Path | str) -> None:
    _run(["docker", "rm", "-f", _container_name(world_path)])
