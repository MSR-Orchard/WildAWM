"""Opencode backend: prepare() writes the per-world opencode.json."""

import json
import os
import shutil
from pathlib import Path

from wildawm_common.opencode_config import SCHEMA_URL, model_ref, provider_block

from wildawm import const_config as runtime_cfg
from wildawm.const_config import (
    BROWSER_MCP_PORT,
    LLM_PROXY_PORT,
    MCP_GATEWAY_URL,
    OPENCODE_INTERNAL_PORT,
)
from wildawm.synthesis.harness import config as occ
from wildawm.synthesis.tasks.vision_shim import DESCRIBE_IMAGE_MCP

from .base import AgentBackend, BackendRuntimeError

_MCP_BASE = MCP_GATEWAY_URL.rstrip("/")
_BASE_MCP_SERVERS = {
    "wildawm": f"{_MCP_BASE}/mcp",
    "browser": f"http://localhost:{BROWSER_MCP_PORT}/mcp",
}


def _world_mcp_servers(world_id: str) -> dict[str, str]:
    """MCP servers for the services materialized in this world."""
    instance = runtime_cfg.INSTANCES_DIR / world_id
    data_dir = instance / "data"
    apps_dir = instance / "overlay" / "apps"
    generated = {path.name for path in apps_dir.iterdir() if path.is_dir()} if apps_dir.is_dir() else set()
    app_ids = sorted(path.stem for path in data_dir.glob("*.db") if path.stem in generated) if data_dir.is_dir() else []
    servers = dict(_BASE_MCP_SERVERS)
    servers.update({app_id: f"{_MCP_BASE}/{app_id}/mcp" for app_id in app_ids})
    return servers


_VISION_NOTE = """
Your current model cannot see image payloads directly. When visual evidence is
needed, save the image or screenshot to a local file under /workspace or /tmp,
then call the `vision_describe_image` tool with that absolute path and a focused
question. Treat the returned text as the image contents. Use text, database,
curl, and browser DOM evidence whenever possible.
"""


class OpencodeBackend(AgentBackend):
    KIND = "opencode"
    # No published host port: worldctl reaches the in-container adapter by service-name DNS.
    PORT_ENV = None

    def internal_port(self) -> int:
        return OPENCODE_INTERNAL_PORT

    def host_port_base(self) -> int:
        return OPENCODE_INTERNAL_PORT + 100

    def prepare(self, world_id: str, project_root: Path, state_dir: Path,
                model: dict) -> None:
        config_dir = state_dir / "agent-config"
        config_dir.mkdir(parents=True, exist_ok=True)

        api_key = model["api_key"]
        model_name = model["api_model"]
        if not model["base_url"] or not api_key:
            raise BackendRuntimeError(
                f"model {model_name!r} missing base_url / api_key")
        mcp_helper_source = project_root / "docker" / "mcp_call.py"
        if not mcp_helper_source.is_file():
            raise BackendRuntimeError(
                f"required MCP shell helper is missing: {mcp_helper_source}")
        try:
            shutil.copyfile(mcp_helper_source, config_dir / "mcp_call.py")
        except OSError as error:
            raise BackendRuntimeError(
                f"required MCP shell helper cannot be copied: "
                f"{mcp_helper_source}") from error
        has_vision_helper = DESCRIBE_IMAGE_MCP.exists()

        # Route through the in-container sanitizing proxy, which strips unsupported reasoning params.
        proxy_base = f"http://localhost:{LLM_PROXY_PORT}/v1"
        mcp = {
            name: {"type": "remote", "url": url, "enabled": True}
            for name, url in _world_mcp_servers(world_id).items()
        }
        instructions = ["/workspace/WORLD.md"]
        tools = {"webfetch": False, "websearch": False}
        if has_vision_helper:
            shutil.copyfile(DESCRIBE_IMAGE_MCP, config_dir / "describe_image_mcp.py")
            (config_dir / "vision_note.md").write_text(_VISION_NOTE)
            instructions.append("/agent-config/vision_note.md")
            vision_spec = runtime_cfg.opencode_vision_spec()
            mcp.update(occ.vision_mcp_block(
                python_cmd="python3",
                script_path="/agent-config/describe_image_mcp.py",
                base_url=runtime_cfg.container_host_url(vision_spec["base_url"]),
                model=vision_spec["api_model"],
                api_key=vision_spec.get("api_key", ""),
                extra_env=runtime_cfg.vision_reasoning_env()))
            tools["browser_browser_take_screenshot"] = False
        else:
            for stale in ("describe_image_mcp.py", "vision_note.md"):
                (config_dir / stale).unlink(missing_ok=True)

        # The primary model stays text-only; image input goes through the separate vision MCP helper.
        cfg = {
            "$schema": SCHEMA_URL,
            "provider": provider_block(
                provider_id="wildawm-openai", model=model_name,
                model_name=f"{model_name} (via proxy)",
                base_url=proxy_base, api_key=api_key,
                name="WildAWM OpenAI-compatible (via proxy)",
                limit={
                    "context": int(model["context_window"]),
                    "output": int(model.get("max_output_tokens") or 8192),
                } if model.get("context_window") else None,
            ),
            "model": model_ref("wildawm-openai", model_name),
            "instructions": instructions,
            "tools": tools,
            # The container is isolated, so skip per-action approval prompts.
            "permission": "allow",
            "mcp": mcp,
            # Disable the hidden auto-title agent: an extra, unused policy call in headless runs.
            "agent": {"title": {"disable": True}},
        }
        (config_dir / "opencode.json").write_text(json.dumps(cfg, indent=2))

        data_dir = state_dir / "agent-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(data_dir, 0o777)
        except OSError:
            pass
