from __future__ import annotations

from collections import defaultdict
from math import comb


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k (Chen et al., 2021)."""
    return 1.0 if n - c < k else 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Probability that k sampled rollouts all succeed."""
    return comb(c, k) / comb(n, k)


def mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 6) if values else None


def summarize(results: list[dict], metric: str = "mean_score", threshold: float = 1.0) -> dict:
    scored = [row for row in results if row.get("status") in ("passed", "failed")]
    summary = {
        "trials": len(results),
        "scored": len(scored),
        "errors": sum(row.get("status") == "error" for row in results),
        "skipped": sum(row.get("status") == "skipped" for row in results),
        "mean_score": mean(float(row["score"]) for row in scored),
        "pass_rate": mean(float(row["score"]) >= threshold for row in scored),
        "metric": metric,
    }
    summary["value"] = summary[metric]
    return summary


def pass_k_summary(results: list[dict], ks: list[int] | None = None) -> dict:
    """pass@k and pass^k over tasks with n rollouts each (errors excluded)."""
    groups = defaultdict(list)
    for row in results:
        if row.get("status") in ("passed", "failed"):
            groups[row["task_id"]].append(row["status"] == "passed")
    if not groups:
        return {}
    n = min(len(group) for group in groups.values())
    summary = {"tasks": len(groups), "rollouts_per_task": n}
    for k in ks or range(1, n + 1):
        if k <= n:
            summary[f"pass@{k}"] = mean(pass_at_k(len(group), sum(group), k) for group in groups.values())
            summary[f"pass^{k}"] = mean(pass_hat_k(len(group), sum(group), k) for group in groups.values())
    summary["tasks_solved"] = sum(any(group) for group in groups.values())
    return summary
