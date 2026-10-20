"""Shared opencode (1.17.7) `provider` block construction."""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA_URL = "https://opencode.ai/config.json"

ANTHROPIC_NPM = "@ai-sdk/anthropic"
OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"

#: Text-only input makes opencode route images through the describe_image MCP.
TEXT_ONLY_MODALITIES = {"input": ["text"], "output": ["text"]}


def model_ref(provider_id: str, model: str) -> str:
    """The `provider/model` reference opencode takes in `model` / `-m`."""
    return f"{provider_id}/{model}"


def provider_block(
    *,
    provider_id: str,
    model: str,
    base_url: str,
    api_key: str,
    api: str = "openai",
    timeout_ms: int | None = None,
    name: str | None = None,
    model_name: str | None = None,
    model_fields: Mapping[str, Any] | None = None,
    limit: Mapping[str, Any] | None = None,
    model_extra: Mapping[str, Any] | None = None,
) -> dict:
    """The `{provider_id: {...}}` opencode `provider` block for one endpoint (diff-stable key order)."""
    entry: dict[str, Any] = {"name": model_name or model}
    entry.update(model_fields or {})
    entry["modalities"] = dict(TEXT_ONLY_MODALITIES)
    if limit is not None:
        entry["limit"] = dict(limit)
    entry.update(model_extra or {})

    options: dict[str, Any] = {"baseURL": base_url}
    options["authToken" if api == "anthropic" else "apiKey"] = api_key
    if timeout_ms is not None:
        options["timeout"] = timeout_ms

    provider = {
        "npm": ANTHROPIC_NPM if api == "anthropic" else OPENAI_COMPATIBLE_NPM,
        "name": name or (
            "Anthropic Messages endpoint" if api == "anthropic"
            else "OpenAI-compatible endpoint"),
        "options": options,
        "models": {model: entry},
    }
    return {provider_id: provider}
