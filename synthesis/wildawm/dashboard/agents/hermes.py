"""Hermes Agent backend: subprocess-based, driven per turn by the in-container agent adapter."""

from .base import SubprocessAgentBackend


class HermesBackend(SubprocessAgentBackend):
    KIND = "hermes"

    def host_port_base(self) -> int:
        return 9400
