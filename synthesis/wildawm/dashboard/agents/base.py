"""Abstract interface for a stateless agent backend wrapping one harness."""

from pathlib import Path


class BackendRuntimeError(RuntimeError):
    """Raised for backend-internal failures (compose, HTTP, parsing)."""


class AgentBackend:
    """Stateless adapter for one agent harness."""

    KIND: str = ""

    # Compose env var for the published host port; None for backends without an HTTP server.
    PORT_ENV: str | None = None

    def publishes_port(self) -> bool:
        """True if this backend serves HTTP on a published host port."""
        return self.PORT_ENV is not None

    COMPOSE_FILE = "docker-compose.unified.yml"

    def container_name(self, world_id: str) -> str:
        # Per-kind container so several harnesses can coexist on one world.
        return f"wildawm-agent-{self.KIND}-{world_id}"

    def service_name(self) -> str:
        """Compose service name for this backend's agent container."""
        return f"agent-{self.KIND}"

    def project_name(self, world_id: str) -> str:
        return f"wildawm-{world_id}"

    def compose_file(self) -> str:
        return self.COMPOSE_FILE

    def internal_port(self) -> int:
        """Port the agent listens on inside its container (0 for subprocess-based agents)."""
        raise NotImplementedError

    def host_port_base(self) -> int:
        """Lowest host port this backend will allocate from."""
        raise NotImplementedError

    def prepare(self, world_id: str, project_root: Path, state_dir: Path,
                model: dict) -> None:
        """Generate per-kind config into state_dir/agent-config/ from the resolved model spec."""
        raise NotImplementedError


class SubprocessAgentBackend(AgentBackend):
    """Base for subprocess harnesses driven per turn by the in-container agent adapter."""

    def internal_port(self) -> int:
        return 0

    def prepare(self, world_id: str, project_root: Path, state_dir: Path,
                model: dict) -> None:
        # The entrypoint configures the harness from the container env; just ensure the config dir.
        (state_dir / "agent-config").mkdir(parents=True, exist_ok=True)
