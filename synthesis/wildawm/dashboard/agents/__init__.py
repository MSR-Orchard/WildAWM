"""Agent backends for the dashboard, one per harness, all on the shared agent image."""

from .base import AgentBackend, BackendRuntimeError
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .hermes import HermesBackend
from .nanobot import NanobotBackend
from .opencode import OpencodeBackend
from .openclaw import OpenClawBackend

REGISTRY: dict[str, AgentBackend] = {
    NanobotBackend.KIND: NanobotBackend(),
    OpencodeBackend.KIND: OpencodeBackend(),
    ClaudeCodeBackend.KIND: ClaudeCodeBackend(),
    CodexBackend.KIND: CodexBackend(),
    HermesBackend.KIND: HermesBackend(),
    OpenClawBackend.KIND: OpenClawBackend(),
}

DEFAULT_KIND = NanobotBackend.KIND


def get(kind: str | None) -> AgentBackend:
    """Resolve a backend by kind, falling back to the default for None."""
    if not kind:
        return REGISTRY[DEFAULT_KIND]
    if kind not in REGISTRY:
        raise BackendRuntimeError(
            f"unknown agent kind {kind!r}; known: {sorted(REGISTRY)}",
        )
    return REGISTRY[kind]


__all__ = ["AgentBackend", "BackendRuntimeError", "REGISTRY", "DEFAULT_KIND", "get"]
