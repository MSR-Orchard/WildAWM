from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class OpencodeConfig:
    name: ClassVar[str] = "opencode"

    model: str = ""
    api_key: str = field(default="", metadata={"secret": True})
    vision_api_key: str = field(default="", metadata={"secret": True})
    # Built by the awmeval host from its shared OpenCode harness; keys are filled in from the secret fields above.
    opencode: dict[str, Any] = field(default_factory=dict)
    runner: dict[str, Any] = field(default_factory=dict)
    vision: dict[str, Any] = field(default_factory=dict)
    mcp_timeout_ms: int = 1_860_000
    assets_dir: str = "/tmp/awmeval"

    opencode_executable: str = "/usr/local/bin/opencode"
    opencode_config_home: str = "/opt/ale-opencode-config"
    browser_use_executable: str = "/opt/opencode-web/bin/browser-use"
    playwright_mcp_cli: str = "/opt/playwright-mcp/node_modules/@playwright/mcp/cli.js"
    browser_python: str = "/opt/opencode-web/bin/python"

    def __post_init__(self) -> None:
        for name in ("model", "api_key", "vision_api_key", "opencode", "runner", "vision"):
            if not getattr(self, name):
                raise ValueError(f"OpencodeConfig.{name} is required")
        if self.mcp_timeout_ms <= 0:
            raise ValueError("OpencodeConfig.mcp_timeout_ms must be positive")
