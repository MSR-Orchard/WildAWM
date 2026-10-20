"""Stage task data when the orchestrator and CUA sandbox share one filesystem."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ...base_interface import SandboxHandle, TaskDataSpec
from . import task_subdir

_PREFIX = "co-located:"


def _paths(sandbox: SandboxHandle, task_data: TaskDataSpec, source: str) -> tuple[Path, Path]:
    if not sandbox.metadata.get("co_located_filesystem"):
        raise RuntimeError("co-located task data requires a co-located sandbox filesystem")
    if not source.startswith(_PREFIX):
        raise ValueError("co-located task data source has an invalid prefix")
    root = Path(source.removeprefix(_PREFIX)).resolve()
    private = (root / task_data.source_relpath).resolve()
    try:
        private.relative_to(root)
    except ValueError as error:
        raise RuntimeError("co-located task data path escapes its private root") from error
    return private, Path(task_subdir(sandbox, task_data))


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for child in path.rglob("*"):
        os.chown(child, uid, gid, follow_symlinks=False)


def _copy_visible(source: Path, target: Path, uid: int, gid: int, *, executable: bool = False) -> None:
    if not source.is_dir():
        raise RuntimeError(f"co-located task data directory is missing: {source}")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, symlinks=False)
    _chown_tree(target, uid, gid)
    if executable:
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | 0o111)


async def stage_input(sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str) -> dict[str, Any]:
    private, visible = _paths(sandbox, task_data, source)
    uid = int(sandbox.metadata.get("desktop_uid", 1000))
    gid = int(sandbox.metadata.get("desktop_gid", uid))
    visible.mkdir(parents=True, exist_ok=True)
    _copy_visible(private / "input", visible / "input", uid, gid)
    staged = ["input"]
    if (private / "software").is_dir():
        _copy_visible(private / "software", visible / "software", uid, gid, executable=True)
        staged.append("software")
    shutil.rmtree(visible / "reference", ignore_errors=True)
    shutil.rmtree(visible / "output", ignore_errors=True)
    (visible / "output").mkdir(mode=0o700)
    os.chown(visible / "output", uid, gid)
    return {"staged": staged, "source": "co-located"}


async def stage_reference(sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str) -> dict[str, Any]:
    private, visible = _paths(sandbox, task_data, source)
    uid = int(sandbox.metadata.get("desktop_uid", 1000))
    gid = int(sandbox.metadata.get("desktop_gid", uid))
    os.chown(visible, uid, gid)
    visible.chmod(0o700)
    reference = private / "reference"
    target = visible / "reference"
    shutil.rmtree(target, ignore_errors=True)
    if not reference.is_dir():
        return {"skipped": True, "reason": "no_reference"}
    _copy_visible(reference, target, uid, gid)
    return {"staged": ["reference"], "source": "co-located"}
