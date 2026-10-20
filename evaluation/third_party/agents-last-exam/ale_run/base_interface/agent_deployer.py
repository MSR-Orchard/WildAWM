"""BaseAgentDeployer — the minimal contract every ALE agent implements.

A deployer is just code: a few Python methods that the framework places
into an executor (vm / local / docker). The framework calls
``install`` → ``launch`` → ``parse_artifacts`` for each unit.

Lives in ``base_interface/`` rather than ``agents/`` so concrete agent
subclasses can import without dragging in the rest of the agents
package, and so this contract is the single point of definition every
other layer of the framework agrees on.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from .trajectory import Trajectory, TrajectoryBuilder

if TYPE_CHECKING:
    # BaseExecutor and BaseAgentDeployer reference each other in their
    # public signatures. Same-package TYPE_CHECKING keeps the cycle from
    # surfacing at runtime; type-checkers see both directions.
    from .executor import BaseExecutor


# =============================================================================
# Config
# =============================================================================
#
# There is intentionally NO shared ``BaseAgentConfig`` base class. Each
# agent's config is a standalone ``@dataclass`` in
# ``ale_run.agents.<agent>.config`` that declares ONLY the knobs that
# agent's deployer actually consumes (plus ``model`` and a ``name``
# ClassVar). A single shared base used to conflate orchestration concerns
# (the episode wall-budget) with agent behavior, and the defaults it
# injected silently diverged from each agent's per-config source of truth;
# distributing the fields by ownership keeps every config self-describing.
#
# The wall-clock episode budget (formerly ``timeout_s`` on this base) is
# orchestration-owned: it is derived from the task at run time
# (``orchestration/lifecycle.py`` reads ``task_meta["timeout_s"]``) and
# enforced by the executor, which wraps ``launch()`` in
# ``asyncio.wait_for(timeout=timeout_s)`` and kills the in-substrate
# process on expiry. Deployers therefore do NOT self-poll a config
# ``timeout_s``; on ``CancelledError`` they reap their child and re-raise.


# =============================================================================
# Run + episode results
# =============================================================================

@dataclass
class AgentRunResult:
    """Outcome of :meth:`BaseAgentDeployer.launch` — handed to
    :meth:`BaseAgentDeployer.parse_artifacts` along with the gathered work_dir.

    Pure data; serializable across executor boundaries.
    """

    status: str                          # "completed" | "timeout" | "failed"
    transcript_path: str | None = None
    stderr_path: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    error: str | None = None


@dataclass
class EpisodeResult:
    """The framework lifecycle's final assembly."""

    reward: float | None
    status: str = "completed"
    error: str | None = None
    instruction: str | None = None
    trajectory: Trajectory | None = None
    duration_s: float | None = None
    task_path: str | None = None
    variant_index: int | None = None

    eval_status: str = "not_executed"
    eval_duration_s: float | None = None
    eval_error: dict[str, Any] | None = None


# =============================================================================
# Deployer ABC
# =============================================================================

class BaseAgentDeployer(abc.ABC):
    """Minimal deployer contract.

    Subclasses MUST set :attr:`supported_executors` (declares which
    substrates this agent can run on: any subset of ``{"sandbox","local","docker"}``)
    AND :attr:`default_executor` (the one used when yaml omits the field).
    The framework validates yaml ``executor`` against ``supported_executors``.
    """

    default_executor: ClassVar[str] = ""
    """The executor type used when yaml's ``agent.executor`` is omitted.
    Empty = error at resolve time. Concrete deployer subclass declares."""

    supported_executors: ClassVar[frozenset[str]] = frozenset()
    """Subclass overrides — strings match yaml ``executor: <type>`` values
    (and :attr:`BaseExecutor.type` class attribute on the concrete impl).
    Empty set is a programmer error caught at ``resolve_agent`` time."""

    hot_artifacts: ClassVar[tuple[str, ...]] = ()
    """Files (relative to :attr:`BaseExecutor.work_dir`) the framework
    should tail while the agent runs. Read by the IncrementalPuller on
    vm-runtime: each path is fetched in deltas every ~15 s so a SIGTERM
    mid-agent doesn't lose the transcript. Empty tuple (the default)
    disables incremental sync — the final one-shot gather still runs."""

    def __init__(self, executor: BaseExecutor):
        self.executor = executor
        self.config = executor.config        # convenience alias

    # ---- abstract methods ----

    @abc.abstractmethod
    async def install(self) -> None:
        """Stage prereqs for this run. Use ``self.executor`` for all
        substrate I/O; the substrate itself (VM, container, host
        process) is the framework's concern — the agent code is
        identical anywhere."""

    @abc.abstractmethod
    async def launch(self, prompt: str) -> AgentRunResult:
        """Spawn the agent and wait for it to finish.

        Always return an :class:`AgentRunResult` (errors → ``status="failed"``
        with ``error=...``). Raise only if even *starting* failed (the
        framework will catch and treat as failed-run too)."""

    @classmethod
    @abc.abstractmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: Any,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        """Read on-disk artifacts in ``work_dir``, populate ``builder``
        with :class:`Step` entries.

        Pure function — always runs on the framework host after the
        framework has gathered the executor's work_dir locally. Doesn't
        need an executor instance; static across all executor kinds for
        a given agent. Partial / missing logs are valid; emit a single
        ``source="system"`` step explaining the gap and return cleanly."""

    # ---- optional metadata ----

    @property
    def version(self) -> str | None:
        """CLI / SDK version string surfaced in run.json + trajectory.agent.version.
        Override if the agent has a meaningful version pin."""
        return None
