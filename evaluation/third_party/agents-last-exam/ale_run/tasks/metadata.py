"""Export per-task sandbox resources and task-data paths for the awmeval host runner."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from ..environments.providers.gcloud import _parse_gce_machine_type
from .loader import TaskLoader


def export_metadata(source: Path, task_ids: list[str]) -> list[dict[str, object]]:
    rows = []
    for task_id in task_ids:
        task = TaskLoader(str(source / "tasks" / task_id)).load(0)
        task_data = task.get("task_data")
        shape = _parse_gce_machine_type(task.get("machine_type"))
        rows.append(
            {
                "task_id": task_id,
                "machine_type": task.get("machine_type"),
                "vcpus": task.get("vcpus") or (shape.vcpus if shape else None),
                "memory_gb": task.get("memory_gb") or (shape.memory_gb if shape else None),
                "task_data": dataclasses.asdict(task_data) if dataclasses.is_dataclass(task_data) else None,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    task_ids = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    if not isinstance(task_ids, list) or any(not isinstance(value, str) for value in task_ids):
        raise ValueError("tasks payload must be a list of task IDs")
    rows = export_metadata(Path(args.source).resolve(), task_ids)
    Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
