"""Claude Code backend: subprocess-based, driven per turn by the in-container agent adapter."""

import json
from pathlib import Path

from wildawm.const_config import AGENT_INTERNAL_PORT, LLM_PROXY_PORT

from .base import AgentBackend


class ClaudeCodeBackend(AgentBackend):
    KIND = "claude-code"

    def internal_port(self) -> int:
        # No HTTP serve mode; a dummy port keeps the compose port mapping valid.
        return AGENT_INTERNAL_PORT

    def host_port_base(self) -> int:
        return 9100

    def prepare(self, world_id: str, project_root: Path, state_dir: Path,
                model: dict) -> None:
        # claude-code-router config for the resolved model; turns run through the in-container proxy.
        config_dir = state_dir / "agent-config"
        config_dir.mkdir(parents=True, exist_ok=True)

        base_url = model["base_url"]
        api_key = model["api_key"]
        model_name = model["api_model"]

        # Append /chat/completions unless base_url already ends with it.
        base_url_normalized = base_url.rstrip("/")
        if base_url_normalized.endswith("/chat/completions"):
            api_base_url = base_url_normalized
        else:
            api_base_url = base_url_normalized + "/chat/completions"

        # `cleancache` strips cache_control; `tooluse` is omitted: it breaks strict chat templates.
        cfg = {
            "Providers": [
                {
                    "name": "wildawm",
                    "api_base_url": api_base_url,
                    "api_key": api_key,
                    "models": [model_name],
                    "transformer": {
                        "use": ["cleancache"],
                    },
                },
            ],
            "Router": {
                "default": f"wildawm,{model_name}",
            },
            "HOST": "0.0.0.0",
            "PORT": LLM_PROXY_PORT,
        }
        (config_dir / "claude-code-router.json").write_text(json.dumps(cfg, indent=2))
