from __future__ import annotations

import json
import logging
import re
import shutil
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .sandbox import STOP, close_all

log = logging.getLogger("awmeval")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def task_dir(out: Path, task_id: str, trial: int, trials: int) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(task_id)).strip("_") or "task"
    return out / "tasks" / (f"{name}--trial-{trial}" if trials > 1 else name)


def latest(rows: list[dict]) -> dict[tuple, dict]:
    return {(str(row["task_id"]), int(row["trial"])): row for row in rows}


def run(bench, tasks: list[dict], out: Path) -> dict:
    """Run every (task, trial) once, retrying infrastructure errors; resumes from results.jsonl."""
    cfg = bench.cfg.run
    trials, retries = int(cfg.get("trials", 1)), int(cfg.get("retries", 3))
    results_path = out / "results.jsonl"
    done = {key for key, row in latest(read_jsonl(results_path)).items() if row.get("status") != "error"}
    jobs = [(task, trial) for task in tasks for trial in range(trials) if (str(task["id"]), trial) not in done]
    lock = threading.Lock()
    counter = {"done": 0}
    log.info("%s: %d trials pending (%d already finished) -> %s", bench.name, len(jobs), len(tasks) * trials - len(jobs), out)

    def work(job) -> None:
        task, trial = job
        path = task_dir(out, task["id"], trial, trials)
        started = time.monotonic()
        row = {"task_id": str(task["id"]), "trial": trial}
        for attempt in range(1, retries + 2):
            if STOP.is_set():
                return
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True, exist_ok=True)
            try:
                result = bench.run_task(task, path)
                status = "skipped" if result.get("skipped") else ("passed" if bench.passed(result) else "failed")
                row.update(result, status=status, score=float(result.get("score") or 0.0), attempts=attempt)
                row.pop("error", None)
                break
            except Exception as error:
                if STOP.is_set():
                    return
                (path / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
                row.update(status="error", score=0.0, attempts=attempt, error=f"{type(error).__name__}: {error}"[:2000])
                log.warning("%s attempt %d/%d failed: %s", task["id"], attempt, retries + 1, row["error"][:300])
        row["seconds"] = round(time.monotonic() - started, 1)
        with lock:
            with results_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            counter["done"] += 1
            log.info("[%d/%d] %s status=%s score=%.3f (%.0fs)", counter["done"], len(jobs), task["id"], row["status"], row["score"], row["seconds"])

    def interrupt(*_args) -> None:
        if not STOP.is_set():
            STOP.set()
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupt)
    pool = ThreadPoolExecutor(max_workers=max(1, int(cfg.get("concurrency", 1))))
    try:
        list(pool.map(work, jobs))
    except KeyboardInterrupt:
        log.warning("interrupted: removing live sandboxes; rerun with the same --name to resume")
        pool.shutdown(wait=False, cancel_futures=True)
        close_all()
        raise SystemExit(130)
    pool.shutdown()
    return finalize(bench, out)


def finalize(bench, out: Path) -> dict:
    rows = list(latest(read_jsonl(out / "results.jsonl")).values())
    summary = {"benchmark": bench.name, **bench.summarize(rows)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
