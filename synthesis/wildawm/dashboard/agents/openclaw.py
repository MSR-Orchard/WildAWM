"""OpenClaw backend: subprocess-based, driven per turn by the in-container agent adapter."""

from .base import SubprocessAgentBackend


class OpenClawBackend(SubprocessAgentBackend):
    KIND = "openclaw"

    def host_port_base(self) -> int:
        return 9500
