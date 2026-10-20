"""Monotonic wall-clock deadlines shared by model and tool calls."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


class TaskDeadlineExceeded(TimeoutError):
    """The task has no remaining wall-clock budget."""


@dataclass(frozen=True, slots=True)
class TaskDeadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def start(
        cls,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> "TaskDeadline":
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        return cls(expires_at=clock() + timeout, clock=clock)

    def remaining(self) -> float:
        value = self.expires_at - self.clock()
        if value <= 0:
            raise TaskDeadlineExceeded("task deadline exhausted")
        return value

    def clamp(self, limit_seconds: float) -> float:
        limit = float(limit_seconds)
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("limit_seconds must be finite and positive")
        return min(limit, self.remaining())
