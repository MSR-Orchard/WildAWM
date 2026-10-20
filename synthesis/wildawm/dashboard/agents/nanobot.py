"""Nanobot backend: prepare() writes the per-world nanobot-config.json."""

import json
from pathlib import Path

from wildawm import const_config as runtime_cfg
from wildawm.const_config import (
    AGENT_INTERNAL_PORT,
    BROWSER_MCP_PORT,
    LOCAL_SCRIPT_TIMEOUT_S,
    LLM_PROXY_PORT,
    MCP_GATEWAY_URL,
)

from .base import AgentBackend

_MCP_BASE = MCP_GATEWAY_URL.rstrip("/")


def _world_mcp_servers(world_id: str) -> dict[str, str]:
    instance = runtime_cfg.INSTANCES_DIR / world_id
    data_dir = instance / "data"
    apps_dir = instance / "overlay" / "apps"
    generated = {path.name for path in apps_dir.iterdir() if path.is_dir()} if apps_dir.is_dir() else set()
    app_ids = sorted(path.stem for path in data_dir.glob("*.db") if path.stem in generated) if data_dir.is_dir() else []
    servers = {
        "wildawm": f"{_MCP_BASE}/mcp",
        "browser": f"http://localhost:{BROWSER_MCP_PORT}/mcp",
    }
    servers.update({app_id: f"{_MCP_BASE}/{app_id}/mcp" for app_id in app_ids})
    return servers


class NanobotBackend(AgentBackend):
    KIND = "nanobot"
    # No published host port: worldctl reaches the in-container adapter by service-name DNS.
    PORT_ENV = None

    def internal_port(self) -> int:
        return AGENT_INTERNAL_PORT

    def host_port_base(self) -> int:
        return AGENT_INTERNAL_PORT

    def prepare(self, world_id: str, project_root: Path, state_dir: Path,
                model: dict) -> None:
        config_dir = state_dir / "agent-config"
        config_dir.mkdir(parents=True, exist_ok=True)

        api_key = model["api_key"]
        model_name = model["api_model"]

        # Route all traffic through the in-container proxy via nanobot's generic `custom` provider.
        cfg = {
            "providers": {
                "custom": {
                    "apiBase": f"http://localhost:{LLM_PROXY_PORT}/v1",
                    "apiKey": api_key or "not-used",
                    "defaultModel": model_name,
                },
            },
            "agents": {
                "defaults": {
                    "provider": "custom",
                    "model": model_name,
                    **({"context_window_tokens": int(model["context_window"])}
                       if model.get("context_window") else {}),
                    **({"reasoning_effort": model["reasoning_effort"]}
                       if model.get("reasoning_effort") else {}),
                },
            },
            "tools": {
                "mcpServers": {
                    name: {"url": url}
                    for name, url in _world_mcp_servers(world_id).items()
                },
                # The container is the sandbox boundary, so the exec tool allows every command.
                "exec": {
                    "enable": True,
                    "timeout": LOCAL_SCRIPT_TIMEOUT_S,
                    "allow_patterns": [".*"],
                },
                # WebHarbor sites resolve to private Docker IPs; allow RFC1918 past the SSRF guard.
                "ssrf_whitelist": [
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "127.0.0.0/8",
                ],
            },
            "channels": {"websocket": {"enabled": False}},
            "api": {"host": "0.0.0.0", "port": AGENT_INTERNAL_PORT},
        }
        (config_dir / "nanobot-config.json").write_text(json.dumps(cfg, indent=2))
