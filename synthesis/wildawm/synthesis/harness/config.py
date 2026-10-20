#!/usr/bin/env python3
"""Build the per-turn ``opencode.json`` for a headless synthesis turn."""
import json

from wildawm_common.opencode_config import SCHEMA_URL, model_ref, provider_block

# opencode tool ids, used to translate Claude's tool allow/deny lists.
OPENCODE_TOOL_IDS = (
    "bash", "edit", "write", "read", "glob", "grep", "list", "patch",
    "apply_patch", "webfetch", "websearch", "task", "todowrite", "todoread",
    "lsp", "skill",
)

# Claude tool name -> opencode tool id; mcp__* tools are gated separately.
_CLAUDE_TO_OPENCODE_TOOL = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Task": "task",
    "TodoWrite": "todowrite",
}


def _split_tools(spec: str | None) -> list[str]:
    if not spec:
        return []
    return [t.strip() for t in spec.split(",") if t.strip()]


def translate_tool_gate(allowed: str | None,
                        disallowed: str | None) -> dict[str, bool]:
    """Translate Claude allowed/disallowed tools to an opencode ``tools`` map; "" disables all."""
    if allowed is not None and allowed.strip() == "":
        return {t: False for t in OPENCODE_TOOL_IDS}
    gate: dict[str, bool] = {}
    for name in _split_tools(disallowed):
        oc = _CLAUDE_TO_OPENCODE_TOOL.get(name)
        if oc:
            gate[oc] = False
    return gate


def vision_mcp_block(*, python_cmd: str, script_path: str,
                     base_url: str, model: str, api_key: str,
                     describe_max_tokens: int = 2000,
                     describe_timeout_s: int = 180,
                     extra_env: dict | None = None) -> dict:
    """The describe_image stdio MCP in opencode ``mcp`` shape (tool ``vision_describe_image``)."""
    env = {
        "AWM_VISION_BASE_URL": base_url,
        "AWM_VISION_MODEL": model,
        "AWM_VISION_API_KEY": api_key,
        "AWM_DESCRIBE_IMAGE_MAX_TOKENS": str(describe_max_tokens),
        "AWM_DESCRIBE_IMAGE_TIMEOUT": str(describe_timeout_s),
    }
    env.update(extra_env or {})
    return {
        "vision": {
            "type": "local",
            "command": [python_cmd, script_path],
            "environment": env,
            "enabled": True,
            # Reasoning vision models with base64 uploads are slow.
            "timeout": (describe_timeout_s + 60) * 1000,
        }
    }


def translate_claude_mcp(mcp_config_json: str | None) -> dict:
    """Translate a Claude ``--mcp-config`` payload into opencode ``mcp`` entries."""
    if not mcp_config_json:
        return {}
    try:
        data = json.loads(mcp_config_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    out: dict = {}
    for name, srv in (data.get("mcpServers") or {}).items():
        if not isinstance(srv, dict):
            continue
        kind = srv.get("type", "stdio")
        if kind in ("stdio", "local"):
            command = [srv["command"], *srv.get("args", [])] if srv.get("command") else srv.get("command")
            entry = {"type": "local", "command": command, "enabled": True}
            if srv.get("env"):
                entry["environment"] = dict(srv["env"])
            out[name] = entry
        elif kind in ("http", "sse", "remote", "streamable-http"):
            entry = {"type": "remote", "url": srv.get("url"), "enabled": True}
            if srv.get("headers"):
                entry["headers"] = dict(srv["headers"])
            out[name] = entry
    return out


def build_opencode_config(*, provider_id: str, model: str, base_url: str,
                          api_key: str,
                          instructions: list[str] | None = None,
                          tools: dict[str, bool] | None = None,
                          mcp: dict | None = None,
                          max_steps: int | None = None,
                          request_timeout_ms: int = 1_800_000,
                          max_output_tokens: int | None = None,
                          context_tokens: int | None = None) -> dict:
    """Assemble the opencode config for one turn, with network conveniences disabled."""
    cfg: dict = {
        "$schema": SCHEMA_URL,
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "model": model_ref(provider_id, model),
        # Text-only: images go via the describe_image MCP; `limit` lifts opencode's small output cap.
        "provider": provider_block(
            provider_id=provider_id, model=model, base_url=base_url,
            api_key=api_key, timeout_ms=request_timeout_ms,
            name=f"WildAWM {provider_id}",
            model_fields={"tool_call": True},
            limit={
                "context": int(context_tokens or max_output_tokens),
                "output": int(max_output_tokens),
            } if max_output_tokens else None,
        ),
        # Explicit headless auto-approval for paths that check config before asking.
        "permission": {
            "edit": "allow",
            "bash": "allow",
            "webfetch": "allow",
            "external_directory": "allow",
        },
    }
    if instructions:
        cfg["instructions"] = list(instructions)
    if tools:
        cfg["tools"] = dict(tools)
    if mcp:
        cfg["mcp"] = dict(mcp)
    if max_steps and max_steps > 0:
        cfg["agent"] = {"build": {"maxSteps": int(max_steps)}}
    return cfg
