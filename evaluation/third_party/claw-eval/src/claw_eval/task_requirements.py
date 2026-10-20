"""Task capability selection and safe official fixture synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import TaskFilterConfig
from .models.task import TaskDefinition


_SERP_SERVICES = frozenset({"web_real", "web_real_injection"})
_OFFICIAL_FIXTURE_REVISION = "ca978fd82edb77d52f26f4ccf3f9684a8df84341"
_OFFICIAL_FIXTURE_SIZE = 2_879_916_129
_OFFICIAL_FIXTURE_SHA256 = "01c349b8eb3ae8bcd741578d6a7f94ba3f76e0aad74bd98233e02e629e65ff5b"


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    source: Path
    destination: str
    grader_only: bool


@dataclass(frozen=True, slots=True)
class SkippedTask:
    task_id: str
    reason: str
    details: list[str]


@dataclass(frozen=True, slots=True)
class TaskSelection:
    eligible: list[TaskDefinition]
    skipped: list[SkippedTask]
    errors: list[SkippedTask]


@dataclass(frozen=True, slots=True)
class FixtureSyncReport:
    archive_path: Path
    archive_revision: str
    archive_bytes: int
    archive_sha256: str
    extracted_files: int
    missing_after_sync: list[str]


class UnsafeArchiveError(RuntimeError):
    """A fixture archive contains a path or member type that cannot be extracted safely."""


class MissingAssetError(FileNotFoundError):
    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = list(paths)
        super().__init__("required task assets are missing")


def requires_serp(task: TaskDefinition) -> bool:
    return task.requires_serp


def _resolve_source(
    task_dir: Path,
    project_root: Path,
    declared_path: str,
) -> Path | None:
    local = task_dir / declared_path
    if local.is_file():
        return local
    project = project_root / declared_path
    return project if project.is_file() else None


def resolve_required_assets(
    task: TaskDefinition,
    task_dir: Path,
    project_root: Path,
) -> list[ResolvedAsset]:
    assets: list[ResolvedAsset] = []
    missing: list[str] = []
    agent_paths = task.sandbox_files or task.environment.fixtures
    phases = (
        (agent_paths, False),
        (task.sandbox_grader_files, True),
    )
    for paths, grader_only in phases:
        for declared_path in paths:
            source = _resolve_source(
                task_dir,
                project_root,
                declared_path,
            )
            if source is None:
                missing.append(declared_path)
                continue
            assets.append(
                ResolvedAsset(
                    source=source,
                    destination=f"/workspace/{declared_path}",
                    grader_only=grader_only,
                )
            )
    for declared_path in task.local_grader_files:
        if _resolve_source(task_dir, project_root, declared_path) is None:
            missing.append(declared_path)
    if missing:
        raise MissingAssetError(list(dict.fromkeys(missing)))
    return assets


def _task_locations(task: TaskDefinition) -> tuple[Path, Path]:
    if task.task_file is None:
        task_dir = Path.cwd()
    else:
        task_dir = Path(task.task_file).parent
    cursor = task_dir.resolve()
    while cursor.parent != cursor:
        if (cursor / "tasks").is_dir():
            return task_dir, cursor
        cursor = cursor.parent
    return task_dir, task_dir.resolve().parent


def select_tasks(
    tasks: Sequence[TaskDefinition],
    task_filter: TaskFilterConfig,
) -> TaskSelection:
    eligible: list[TaskDefinition] = []
    skipped: list[SkippedTask] = []
    errors: list[SkippedTask] = []
    for task in tasks:
        serp_services = [
            service.name
            for service in task.services
            if service.name in _SERP_SERVICES
        ]
        if task_filter.skip_requires_serp and serp_services:
            skipped.append(
                SkippedTask(
                    task_id=task.task_id,
                    reason="missing_serp_api",
                    details=serp_services,
                )
            )
            continue
        if task_filter.require_all_assets:
            task_dir, project_root = _task_locations(task)
            try:
                resolve_required_assets(
                    task,
                    task_dir,
                    project_root,
                )
            except MissingAssetError as error:
                errors.append(
                    SkippedTask(
                        task_id=task.task_id,
                        reason="missing_asset",
                        details=error.paths,
                    )
                )
                continue
        eligible.append(task)
    return TaskSelection(
        eligible=eligible,
        skipped=skipped,
        errors=errors,
    )


def _atomic_write_json(path: Path, value: object) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_task_selection_manifests(
    selection: TaskSelection,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    skipped_path = output_dir / "skipped_tasks.json"
    errors_path = output_dir / "asset_errors.json"
    summary_path = output_dir / "selection_summary.json"
    serialize = lambda item: {
        "task_id": item.task_id,
        "reason": item.reason,
        "details": item.details,
    }
    _atomic_write_json(
        skipped_path,
        [serialize(item) for item in selection.skipped],
    )
    _atomic_write_json(
        errors_path,
        [serialize(item) for item in selection.errors],
    )
    _atomic_write_json(
        summary_path,
        {
            "eligible": len(selection.eligible),
            "skipped": len(selection.skipped),
            "errors": len(selection.errors),
        },
    )
    return {
        "skipped": skipped_path,
        "errors": errors_path,
        "summary": summary_path,
    }


def _safe_member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    path = PurePosixPath(member.name)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if (
        not parts
        or path.is_absolute()
        or ".." in parts
        or "\\" in member.name
    ):
        raise UnsafeArchiveError("fixture archive contains an unsafe path")
    if not member.isdir() and not member.isreg():
        raise UnsafeArchiveError("fixture archive contains a link or special member")
    return parts


def _extract_fixture_archive(
    archive_path: Path,
    destination: Path,
) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    staging = Path(
        tempfile.mkdtemp(
            prefix=".claw-eval-fixtures.",
            dir=destination.parent,
        )
    )
    extracted_files = 0
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            validated: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
            seen: set[tuple[str, ...]] = set()
            for member in archive.getmembers():
                parts = _safe_member_parts(member)
                if parts in seen:
                    raise UnsafeArchiveError(
                        "fixture archive contains a duplicate path"
                    )
                seen.add(parts)
                validated.append((member, parts))
            for member, parts in validated:
                staged_path = staging.joinpath(*parts)
                if member.isdir():
                    staged_path.mkdir(parents=True, exist_ok=True)
                    continue
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UnsafeArchiveError(
                        "fixture archive regular file has no content"
                    )
                with source, open(staged_path, "xb") as output:
                    shutil.copyfileobj(source, output)
                extracted_files += 1

        for staged_path in sorted(
            staging.rglob("*"),
            key=lambda path: (not path.is_dir(), len(path.parts)),
        ):
            relative = staged_path.relative_to(staging)
            target = destination / relative
            if staged_path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.parent.resolve().relative_to(destination_root)
            except ValueError as error:
                raise UnsafeArchiveError(
                    "fixture destination contains an escaping link"
                ) from error
            os.replace(staged_path, target)
        return extracted_files
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def safe_extract_fixture_archive(
    archive: Path,
    destination: Path,
) -> None:
    _extract_fixture_archive(archive, destination)


def _missing_repository_assets(project_root: Path) -> list[str]:
    missing: list[str] = []
    for task_path in sorted((project_root / "tasks").glob("*/task.yaml")):
        task = TaskDefinition.from_yaml(task_path)
        if requires_serp(task):
            continue
        try:
            resolve_required_assets(
                task,
                task_path.parent,
                project_root,
            )
        except MissingAssetError as error:
            missing.extend(
                f"{task.task_id}:{path}"
                for path in error.paths
            )
    return missing


def sync_official_fixtures(
    repo_id: str,
    token: str,
    project_root: Path,
    *,
    downloader: Callable[..., str] | None = None,
) -> FixtureSyncReport:
    if downloader is None:
        from huggingface_hub import hf_hub_download

        downloader = hf_hub_download
    raw_archive = downloader(
        repo_id=repo_id,
        filename="data/fixtures.tar.gz",
        repo_type="dataset",
        revision=_OFFICIAL_FIXTURE_REVISION,
        token=token,
    )
    archive_path = Path(raw_archive)
    archive_size = archive_path.stat().st_size
    if archive_size != _OFFICIAL_FIXTURE_SIZE:
        raise ValueError(f"official fixture archive size mismatch: expected {_OFFICIAL_FIXTURE_SIZE}, got {archive_size}")
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    archive_sha256 = digest.hexdigest()
    if archive_sha256 != _OFFICIAL_FIXTURE_SHA256:
        raise ValueError(f"official fixture archive digest mismatch: expected {_OFFICIAL_FIXTURE_SHA256}, got {archive_sha256}")
    extracted_files = _extract_fixture_archive(
        archive_path,
        project_root / "tasks",
    )
    return FixtureSyncReport(
        archive_path=archive_path,
        archive_revision=_OFFICIAL_FIXTURE_REVISION,
        archive_bytes=archive_size,
        archive_sha256=archive_sha256,
        extracted_files=extracted_files,
        missing_after_sync=_missing_repository_assets(project_root),
    )
