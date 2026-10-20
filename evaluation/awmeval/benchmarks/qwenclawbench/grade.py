"""Grade one QwenClawBench trial in a host child process (JSON request on stdin)."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path


def main() -> None:
    request = json.loads(sys.stdin.read())
    sys.path[0] = request["scripts"]  # graders run with the vendored scripts first on sys.path, as upstream
    from lib_grading import grade_task
    from lib_tasks import TaskLoader

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    task_file = Path(request["task_file"])
    task = TaskLoader(task_file.parent).load_task(task_file)
    transcript = json.loads(Path(request["transcript"]).read_text(encoding="utf-8"))
    judge = request["judge"]
    grade = grade_task(task=task, execution_result={"transcript": transcript, "workspace": request["workspace"]}, judge_model=judge["model"],
                       judge_base_url=judge["base_url"], judge_api_key=judge["api_key"], judge_timeout_seconds=judge["timeout"],
                       judge_max_output_tokens=judge["max_output_tokens"], judge_reasoning_effort=judge["reasoning_effort"], judge_retries=judge["retries"])
    Path(request["output"]).write_text(json.dumps(grade.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
