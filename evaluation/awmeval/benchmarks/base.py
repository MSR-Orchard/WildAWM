from __future__ import annotations

from pathlib import Path

from .. import metrics


class Benchmark:
    """One evaluation benchmark: task list, per-task episode, and scoring."""

    name = ""
    metric = "mean_score"
    pass_threshold = 1.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.options = cfg.benchmark

    def prepare(self) -> None:
        """Download or check benchmark data (optional)."""

    def tasks(self) -> list[dict]:
        raise NotImplementedError

    def run_task(self, task: dict, out: Path) -> dict:
        raise NotImplementedError

    def passed(self, result: dict) -> bool:
        return float(result.get("score") or 0.0) >= self.pass_threshold

    def summarize(self, results: list[dict]) -> dict:
        return metrics.summarize(results, self.metric, self.pass_threshold)
