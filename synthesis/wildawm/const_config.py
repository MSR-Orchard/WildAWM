"""Central constants; env overrides use AWM_* names (WILDAWM_* aliases win where accepted)."""

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from wildawm.json_utils import load_jsonc

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# Zone for human-facing timestamps only; stored data stays in epoch seconds.
REPORT_TZ = ZoneInfo(os.environ.get("AWM_REPORT_TZ", "America/Los_Angeles"))


def report_time(ts: float | None = None) -> datetime.datetime:
    """A timezone-aware datetime in the project's reporting zone; `ts` is epoch seconds, defaulting to now."""
    if ts is None:
        return datetime.datetime.now(tz=REPORT_TZ)
    return datetime.datetime.fromtimestamp(ts, tz=REPORT_TZ)


def report_stamp(ts: float | None = None) -> str:
    """ISO-8601 timestamp with an explicit UTC offset."""
    return report_time(ts).isoformat(timespec="seconds")


def parse_report_time(text: str) -> datetime.datetime:
    """Parse an ISO timestamp, attaching the reporting zone when the text carries no offset."""
    parsed = datetime.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=REPORT_TZ)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_any(names: tuple[str, ...], default: str) -> str:
    for name in names:
        if name in os.environ:
            return os.environ[name]
    return default


def _env_w(name: str, default: str, alias: str = "") -> str:
    """Env lookup honoring the WILDAWM_* contract with an optional AWM_* alias."""
    names = (name,) + ((alias,) if alias and alias != name else ())
    return _env_any(names, default)


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _env_path_opt(name: str) -> Path | None:
    """Optional absolute path override; None when unset/empty."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _env_csv_set(name: str, default: str = "") -> set[str]:
    return {item for item in _env(name, default).split(",") if item}


def _env_csv_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


def _env_csv_list_any(names: tuple[str, ...], default: str = "") -> list[str]:
    """CSV list lookup across several env names (first set name wins)."""
    for name in names:
        if os.environ.get(name, "").strip():
            return [item.strip() for item in os.environ[name].split(",")
                    if item.strip()]
    return [item.strip() for item in default.split(",") if item.strip()]


# Optional JSONC override file for synthesis knobs; loaded lazily, a missing path is not an error.
SYNTHESIS_CONFIG_PATH = _env_path_opt("WILDAWM_SYNTHESIS_CONFIG")
_SYNTHESIS_CONFIG: dict | None = None


def synthesis_config() -> dict:
    """Parsed WILDAWM_SYNTHESIS_CONFIG (or {}); an unreadable or non-object file is a hard error."""
    global _SYNTHESIS_CONFIG
    if _SYNTHESIS_CONFIG is None:
        if SYNTHESIS_CONFIG_PATH is None:
            _SYNTHESIS_CONFIG = {}
        else:
            try:
                loaded = load_jsonc(SYNTHESIS_CONFIG_PATH)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"WILDAWM_SYNTHESIS_CONFIG={SYNTHESIS_CONFIG_PATH} "
                    f"could not be parsed: {exc}") from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(
                    f"WILDAWM_SYNTHESIS_CONFIG={SYNTHESIS_CONFIG_PATH} "
                    "must be a JSON object")
            _SYNTHESIS_CONFIG = loaded
    return _SYNTHESIS_CONFIG


def _cfg_file_int(key: str) -> int | None:
    raw = synthesis_config().get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"WILDAWM_SYNTHESIS_CONFIG: {key!r} must be an integer, "
            f"got {raw!r}") from exc


def _cfg_file_bool(key: str) -> bool | None:
    raw = synthesis_config().get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {
            "1", "true", "yes", "on", "0", "false", "no", "off"}:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    raise RuntimeError(
        f"WILDAWM_SYNTHESIS_CONFIG: {key!r} must be a boolean, got {raw!r}")


def _cfg_file_float(key: str) -> float | None:
    raw = synthesis_config().get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"WILDAWM_SYNTHESIS_CONFIG: {key!r} must be a number, got {raw!r}") from exc


def _cfg_file_list(key: str) -> list[str] | None:
    raw = synthesis_config().get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, list):
        raise RuntimeError(
            f"WILDAWM_SYNTHESIS_CONFIG: {key!r} must be a list of strings")
    return [str(item).strip() for item in raw if str(item).strip()]


def _env_or_cfg_int(env_name: str, cfg_key: str, default: int) -> int:
    """Env override > config-file override > code default (for limits)."""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip() != "":
        return int(raw)
    value = _cfg_file_int(cfg_key)
    return value if value is not None else default


def _env_or_cfg_bool(env_name: str, cfg_key: str, default: bool) -> bool:
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    value = _cfg_file_bool(cfg_key)
    return value if value is not None else default


def _env_or_cfg_float(env_name: str, cfg_key: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip() != "":
        return float(raw)
    value = _cfg_file_float(cfg_key)
    return value if value is not None else default


DASHBOARD_HOST = _env("AWM_DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _env_int("AWM_DASHBOARD_PORT", 8624)
DASHBOARD = _env("AWM_DASHBOARD", f"http://localhost:{DASHBOARD_PORT}")
SYNTHESIS_DASHBOARD = _env_any(
    ("AWM_SYNTHESIS_DASHBOARD", "AWM_SYNTH_DASHBOARD"),
    DASHBOARD,
)
MCP_INTERNAL_PORT = _env_int("AWM_MCP_INTERNAL_PORT", 8080)
MCP_HOST_PORT = _env_int("AWM_MCP_PORT", MCP_INTERNAL_PORT)
MCP_PORT_BASE = _env_int("AWM_MCP_PORT_BASE", 8180)
MCP_GATEWAY_URL = _env("AWM_MCP_GATEWAY_URL", f"http://mcp:{MCP_INTERNAL_PORT}")
AGENT_INTERNAL_PORT = _env_int("AWM_AGENT_INTERNAL_PORT", 8900)
OPENCODE_INTERNAL_PORT = _env_int("AWM_OPENCODE_INTERNAL_PORT", 4096)
WORLDCTL_PORT = _env_int("AWM_WORLDCTL_PORT", 8300)
WORLDCTL_PORT_BASE = _env_int("AWM_WORLDCTL_PORT_BASE", WORLDCTL_PORT)
AGENT_ADAPTER_PORT = _env_int("AWM_AGENT_ADAPTER_PORT", 8999)
LLM_PROXY_PORT = _env_int("AWM_LLM_PROXY_PORT", 3456)
BROWSER_MCP_PORT = _env_int("AWM_BROWSER_MCP_PORT", 8931)
WEBHARBOR_CTRL_PORT = _env_int("AWM_WEBHARBOR_CTRL_PORT", 8101)
WEBHARBOR_CTRL_HOST_PORT = _env_int("AWM_WEBHARBOR_CTRL_HOST_PORT", WEBHARBOR_CTRL_PORT)
WEBHARBOR_BASE_PORT = _env_int("AWM_WEBHARBOR_BASE_PORT", 40000)
WEBHARBOR_PORT_BASE = _env_int("AWM_WEBHARBOR_PORT_BASE", 8200)
# Docker default-bridge gateway, used by containers to reach host-loopback services.
DOCKER_HOST_GATEWAY = _env_w("WILDAWM_DOCKER_HOST_GATEWAY", "172.17.0.1",
                            alias="AWM_DOCKER_HOST_GATEWAY")
OPENAI_HOST_PROXY_PORT = _env_int("AWM_OPENAI_HOST_PROXY_PORT", 8787)
# No baked endpoint: provisioning fails loudly when the host proxy URL is unset.
OPENAI_HOST_PROXY_URL = _env_w("WILDAWM_OPENAI_HOST_PROXY_URL", "",
                               alias="AWM_OPENAI_HOST_PROXY_URL")
OPENAI_BASE_URL = _env("AWM_OPENAI_BASE_URL", "")
OPENAI_API_KEY = _env("AWM_OPENAI_API_KEY", "")
OPENAI_MODEL = _env("AWM_OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_REASONING_EFFORT = _env("AWM_REASONING_EFFORT", "").strip()
CLAUDE_CODE_AUTO_COMPACT_WINDOW = _env("AWM_CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")


class ReasoningEffortError(ValueError):
    """Invalid or unsupported reasoning-effort request."""


REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
REASONING_EFFORT_EXTRA = ("adaptive",)
REASONING_EFFORT_VALUES = set(REASONING_EFFORT_ORDER) | set(REASONING_EFFORT_EXTRA)
REASONING_EFFORT_ALIASES = {"off": "none"}
REASONING_HARNESS_VALUES = {
    # `none` skips the CLI flag while the proxy still injects the upstream value.
    "claude-code": {"none", "low", "medium", "high", "xhigh", "max"},
    # Codex's native flag lacks `max`, but the in-container proxy injects it upstream.
    "codex": {"none", "low", "medium", "high", "xhigh", "max"},
    # OpenClaw's native flag tops out at high; the proxy injects the actual upstream effort.
    "openclaw": {"none", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"},
}


def normalize_reasoning_effort(effort: str | None) -> str:
    """Canonical reasoning-effort string used across CLI, harnesses and proxy."""
    raw = str(effort or "").strip().lower()
    if not raw:
        return ""
    raw = REASONING_EFFORT_ALIASES.get(raw, raw)
    if raw not in REASONING_EFFORT_VALUES:
        raise ReasoningEffortError(
            f"reasoning_effort {effort!r} is not one of "
            f"{sorted(REASONING_EFFORT_VALUES | set(REASONING_EFFORT_ALIASES))}")
    return raw


def _reasoning_values_through(max_effort: str) -> list[str]:
    max_effort = normalize_reasoning_effort(max_effort)
    if max_effort in REASONING_EFFORT_EXTRA:
        return [max_effort]
    if max_effort not in REASONING_EFFORT_ORDER:
        raise ReasoningEffortError(f"cannot derive reasoning efforts through {max_effort!r}")
    return list(REASONING_EFFORT_ORDER[:REASONING_EFFORT_ORDER.index(max_effort) + 1])


def _reasoning_raw_quirk(spec: dict) -> dict | None:
    quirks = (spec or {}).get("quirks") or {}
    if "reasoning_effort" in quirks:
        raw = quirks.get("reasoning_effort")
    elif "reasoning_efforts" in quirks:
        raw = {"values": quirks.get("reasoning_efforts")}
    elif "reasoning_effort_max" in quirks:
        raw = {"max": quirks.get("reasoning_effort_max")}
    else:
        return None
    if raw is True:
        return {"max": "max"}
    if raw in (False, None):
        return None
    if isinstance(raw, str):
        return {"max": raw}
    if isinstance(raw, list):
        return {"values": raw}
    if isinstance(raw, dict):
        return dict(raw)
    raise ReasoningEffortError(f"invalid reasoning_effort quirk for {(spec or {}).get('ref')}: {raw!r}")


def _reasoning_supported_values(raw: dict) -> list[str]:
    raw_values = raw.get("values")
    if raw_values is None:
        return _reasoning_values_through(raw.get("max", "max"))
    return [normalize_reasoning_effort(v) for v in raw_values]


def reasoning_effort_default(spec: dict) -> str:
    """Registry-declared default reasoning effort for a model, or ""."""
    raw = _reasoning_raw_quirk(spec)
    if raw is None:
        return ""
    default = normalize_reasoning_effort(raw.get("default"))
    if not default:
        return ""
    values = _reasoning_supported_values(raw)
    ref = (spec or {}).get("ref") or (spec or {}).get("api_model") or "model"
    if default not in values:
        raise ReasoningEffortError(
            f"{ref} declares reasoning_effort default {default!r}, "
            f"but supported values are {values}")
    return default


def reasoning_effort_policy(spec: dict, effort: str | None,
                            harness: str | None = None) -> dict | None:
    """Validate a reasoning-effort request; a blank request falls back to the env/registry default."""
    raw = _reasoning_raw_quirk(spec)
    ref = (spec or {}).get("ref") or (spec or {}).get("api_model") or "model"
    eff = normalize_reasoning_effort(effort)
    if not eff:
        eff = normalize_reasoning_effort(DEFAULT_REASONING_EFFORT)
    if not eff and raw is not None:
        eff = reasoning_effort_default(spec)
    if not eff:
        return None
    if raw is None:
        raise ReasoningEffortError(
            f"{ref} does not declare reasoning-effort support in models.json; "
            "add quirks.reasoning_effort before using --reasoning_effort")
    values = _reasoning_supported_values(raw)
    if eff not in values:
        raise ReasoningEffortError(
            f"{ref} supports reasoning_effort {values}, not {eff!r}")
    harnesses = raw.get("harnesses")
    if harness and harnesses and harness not in set(harnesses):
        raise ReasoningEffortError(
            f"{ref} reasoning_effort is not enabled for harness {harness!r}; "
            f"allowed harnesses: {list(harnesses)}")
    harness_values = REASONING_HARNESS_VALUES.get(harness or "")
    if harness_values and eff not in harness_values:
        raise ReasoningEffortError(
            f"harness {harness!r} cannot pass reasoning_effort {eff!r}; "
            f"allowed for this harness: {sorted(harness_values)}")
    return {
        "effort": eff,
        "values": values,
        "chat_param": raw.get("chat_param", "reasoning_effort"),
        "responses_param": raw.get("responses_param", "reasoning"),
        "fail_if_stripped": bool(raw.get("fail_if_stripped", True)),
        "harnesses": list(harnesses or []),
    }


def reasoning_effort_env(policy: dict | None) -> dict[str, str]:
    """Environment variables consumed by the in-container harness proxy."""
    return prefixed_reasoning_effort_env(policy, "AWM_REASONING")


def prefixed_reasoning_effort_env(policy: dict | None,
                                  prefix: str) -> dict[str, str]:
    """Reasoning env with a configurable prefix (vision MCPs use AWM_VISION_REASONING)."""
    if not policy:
        return {}
    return {
        f"{prefix}_EFFORT": policy["effort"],
        f"{prefix}_CHAT_PARAM": policy.get("chat_param") or "reasoning_effort",
        f"{prefix}_RESPONSES_PARAM": policy.get("responses_param") or "reasoning",
        f"{prefix}_FAIL_IF_STRIPPED": (
            "1" if policy.get("fail_if_stripped", True) else "0"),
    }


def _set_reasoning_param(body: dict, param: str, effort: str) -> None:
    key = (param or "").strip()
    if not key:
        return
    body.pop("reasoning", None)
    body.pop("reasoning_effort", None)
    body.pop("reasoningEffort", None)
    if key in ("reasoning", "reasoning.effort"):
        body["reasoning"] = {"effort": effort}
    elif key == "reasoning_effort":
        body["reasoning_effort"] = effort
    elif key == "reasoningEffort":
        body["reasoningEffort"] = effort
    elif key in ("chat_template_kwargs.reasoning_effort",
                 "chat_template_kwargs.deepseek_reasoning_effort"):
        ctk = dict(body.get("chat_template_kwargs") or {})
        if effort == "none":
            ctk["thinking"] = False
            ctk.pop("reasoning_effort", None)
        else:
            ctk["thinking"] = True
            ctk["reasoning_effort"] = effort
        body["chat_template_kwargs"] = ctk
    elif key == "chat_template_kwargs.enable_thinking":
        ctk = dict(body.get("chat_template_kwargs") or {})
        ctk["enable_thinking"] = effort != "none"
        body["chat_template_kwargs"] = ctk
    else:
        raise ReasoningEffortError(f"unknown reasoning parameter shape {param!r}")


def apply_reasoning_to_body(body: dict, policy: dict | None, *,
                            api: str = "chat") -> dict:
    """Inject a validated reasoning policy into an OpenAI-compatible request."""
    if not policy:
        return body
    param = (policy.get("responses_param") if api == "responses"
             else policy.get("chat_param"))
    _set_reasoning_param(body, param or "reasoning_effort", policy["effort"])
    return body


def claude_code_auto_compact_window(
    model: str,
    context_window: int | str | None = None,
) -> str:
    """Window at which Claude Code auto-compacts, kept below the model's practical context."""
    override = (
        os.environ.get("AWM_CLAUDE_CODE_AUTO_COMPACT_WINDOW")
        or os.environ.get("AWM_MODEL_CONTEXT_WINDOW")
    )
    if override:
        return override
    if context_window is not None and str(context_window).strip():
        return str(int(context_window))
    # Registry-backed launches pass context_window; 128K is the fallback for direct callers.
    return "131072"


def claude_code_model_env(
    model: str | None = None,
    context_window: int | str | None = None,
) -> dict[str, str]:
    """Claude Code env pinning every model tier to the routed model."""
    env = {
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_INTERLEAVED_THINKING": "1",
    }
    if model:
        env.update({
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            # Subagents and auto-compaction follow the routed model too.
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": (
                os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
                or claude_code_auto_compact_window(model, context_window)
            ),
        })
    return env


def container_host_url(url: str) -> str:
    """Rewrite host-loopback URLs so Docker containers can reach the host."""
    for host in ("127.0.0.1", "localhost", "0.0.0.0"):
        url = url.replace(f"://{host}:", f"://{DOCKER_HOST_GATEWAY}:")
        url = url.replace(f"://{host}/", f"://{DOCKER_HOST_GATEWAY}/")
    return url

# Directory layout; WILDAWM_DATA_ROOT relocates the whole run-data tree at once.
DATA_WORK_ROOT = _env_path_opt("WILDAWM_DATA_ROOT")
_RUN_ROOT = DATA_WORK_ROOT or PROJECT_ROOT
DATA_ROOT = _env_path("AWM_DATA_DIR", _RUN_ROOT / "data")
WORLDS_DIR = _env_path("AWM_WORLDS_DIR", _RUN_ROOT / "worlds")
INSTANCES_DIR = _env_path("AWM_INSTANCES_DIR", WORLDS_DIR / "instances")
MASTER_DIR = _env_path("AWM_MASTER_DIR", WORLDS_DIR / "_master")
# World-synthesis pipeline anchors, all under WORLDS_DIR.
WS_PROFILES_DIR = _env_path("AWM_WS_PROFILES_DIR", WORLDS_DIR / "_profiles")
WS_PLAN_DIR = _env_path("AWM_WS_PLAN_DIR", WORLDS_DIR / "_plan")
WS_EVENTS_DIR = _env_path("AWM_WS_EVENTS_DIR", WORLDS_DIR / "_events")
WS_APPS_DIR = _env_path("AWM_WS_APPS_DIR", WORLDS_DIR / "_apps")
WS_SCRIPTS_DIR = _env_path("AWM_WS_SCRIPTS_DIR", WORLDS_DIR / "_scripts")
WS_MASTERS_DIR = _env_path("AWM_WS_MASTERS_DIR", WORLDS_DIR / "_masters")
OUTPUTS_DIR = _env_path("AWM_OUTPUTS_DIR", _RUN_ROOT / "outputs")
TASKS_XAI_DIR = _env_path("AWM_TASKS_XAI_DIR", _RUN_ROOT / "tasks_xai")
TASKS_OUT = TASKS_XAI_DIR
SCRATCH = _env_path("AWM_SCRATCH_DIR", WORLDS_DIR / "_xai_scratch")
XAI_SYNTHESIS_POOL_DB = _env_path(
    "AWM_XAI_SYNTHESIS_POOL_DB",
    _RUN_ROOT / "runtime" / "xai_synthesis_pool.sqlite",
)
XAI_DAY_PROPOSAL_ATTEMPTS = _env_int("AWM_XAI_DAY_PROPOSAL_ATTEMPTS", 3)
# Proposal budget: 900s exploration plus the default 420s finalization reserve.
XAI_DAY_PROPOSAL_TIMEOUT_S = _env_int("AWM_XAI_DAY_PROPOSAL_TIMEOUT", 1320)
XAI_REQUIRE_IMAGE_DECISIVE = _env_or_cfg_bool(
    "AWM_XAI_REQUIRE_IMAGE_DECISIVE", "xai_require_image_decisive", True)
XAI_SYNTHESIS_SKILLS_ENABLED = _env_bool(
    "AWM_XAI_SYNTHESIS_SKILLS_ENABLED", True)
XAI_SYNTHESIS_SKILL_DIRNAME = _env(
    "AWM_XAI_SYNTHESIS_SKILL_DIRNAME", "_synthesis_skills")
XAI_SYNTHESIS_SKILL_MAX_DOCS = _env_or_cfg_int(
    "AWM_XAI_MAX_SYNTHESIS_SKILLS", "xai_max_synthesis_skills", 50)
XAI_SYNTHESIS_SKILL_MAX_CHARS = _env_or_cfg_int(
    "AWM_XAI_SYNTHESIS_SKILL_MAX_CHARS", "xai_synthesis_skill_max_chars", 2000)
XAI_SYNTHESIS_SKILL_PROMPT_DOCS = _env_int(
    "AWM_XAI_SYNTHESIS_SKILL_PROMPT_DOCS", 8)
XAI_SYNTHESIS_SKILL_WRITER_TIMEOUT_S = _env_int(
    "AWM_XAI_SYNTHESIS_SKILL_WRITER_TIMEOUT", 300)
# Share of final binary rollouts given the counterfactual probe (stable per task/cell).
XAI_COUNTERFACTUAL_PROBE_RATE = _env_or_cfg_float(
    "AWM_COUNTERFACTUAL_PROBE_RATE", "counterfactual_probe_rate", 1.0)
if not 0.0 <= XAI_COUNTERFACTUAL_PROBE_RATE <= 1.0:
    raise ValueError("AWM_COUNTERFACTUAL_PROBE_RATE must be between 0 and 1")
WEBHARBOR_SEED_CACHE_DIR = _env_path("AWM_WEBHARBOR_SEED_CACHE_DIR", _RUN_ROOT / "webharbor_seeds")
# WebHarbor image (site source and static images baked at /opt/WebSyn).
WEBHARBOR_IMAGE = _env_any(("AWM_WEBHARBOR_IMAGE", "WEBHARBOR_IMAGE"),
                          "wildawm-webharbor:latest")
# Read-only WebHarbor site tree mounted into heavy-judge containers; populated once from the image.
WEBSYN_CACHE_DIR = _env_path("AWM_WEBSYN_CACHE_DIR",
                             _RUN_ROOT / "runtime" / "websyn_cache")
WEBSYN_CACHE_TIMEOUT_S = _env_int("AWM_WEBSYN_CACHE_TIMEOUT", 900)
LOG_DIR = _env_path("AWM_LOG_DIR", _RUN_ROOT / "logs")
MODELS_PATH = _env_path("AWM_MODELS_PATH", PROJECT_ROOT / "models.json")
ENV_PATH = _env_path("AWM_ENV_PATH", PROJECT_ROOT / ".env")
CLAUDE_CONFIG_DIR = _env_path("AWM_CLAUDE_CONFIG_DIR", Path.home() / ".claude")

# Runtime directories used by the dashboard and world runtime.
EVENTS_DIR = _env_path("AWM_EVENTS_DIR", WS_EVENTS_DIR)
PROFILES_DIR = _env_path("AWM_PROFILES_DIR", WS_PROFILES_DIR)
SEED_DIR = _env_path("AWM_SEED_DIR", _RUN_ROOT / "seed")
STORIES_DIR = _env_path("AWM_STORIES_DIR", WS_PLAN_DIR)
NANOBOT_DIR = _env_path("AWM_NANOBOT_DIR", _RUN_ROOT / "runtime" / "nanobot")
NANOBOT_STATE_DIR = _env_path("AWM_NANOBOT_STATE_DIR", NANOBOT_DIR / "state")

INSTANCE_PREFIX = _env("AWM_INSTANCE_PREFIX", "awm")

# Default synthesis model: a models.json ref with no baked default; model_spec() fails loudly if unset.
DEFAULT_GLM_LLM_REF = _env("WILDAWM_SYNTH_MODEL_REF", "")
DEFAULT_GLM_LLM_PROVIDER, _, DEFAULT_GLM_LLM_MODEL = DEFAULT_GLM_LLM_REF.partition("/")
if not DEFAULT_GLM_LLM_MODEL:
    DEFAULT_GLM_LLM_PROVIDER = DEFAULT_GLM_LLM_REF
    DEFAULT_GLM_LLM_MODEL = DEFAULT_GLM_LLM_REF
# Lightweight (single-call) judge; defaults to the synthesis model.
DEFAULT_LIGHTWEIGHT_JUDGE_REF = DEFAULT_GLM_LLM_REF
LIGHTWEIGHT_JUDGE_MODEL = _env(
    "AWM_LIGHTWEIGHT_JUDGE_REF", DEFAULT_LIGHTWEIGHT_JUDGE_REF)
# Empty uses the judge model's registry default effort.
LIGHTWEIGHT_JUDGE_REASONING_EFFORT = _env(
    "AWM_LIGHTWEIGHT_JUDGE_REASONING_EFFORT", "").strip()
# Dedicated output budget so hidden reasoning cannot crowd out the visible verdict.
LIGHTWEIGHT_JUDGE_MAX_OUTPUT_TOKENS = _env_int(
    "AWM_LIGHTWEIGHT_JUDGE_MAX_OUTPUT_TOKENS", 32768)
HEAVY_JUDGE_MODEL = _env("AWM_HEAVY_JUDGE_REF", DEFAULT_GLM_LLM_REF)
DIVERSITY_JUDGE_MODEL = _env("AWM_DIVERSITY_JUDGE_REF", DEFAULT_GLM_LLM_REF)
TASK_SKETCH_LLM_MODEL = _env("AWM_TASK_SKETCH_LLM_MODEL", DEFAULT_GLM_LLM_MODEL)
TASK_SKETCH_LLM_EFFORT = _env("AWM_TASK_SKETCH_LLM_EFFORT", "")
TASK_SKETCH_LLM_ROUTE = _env("AWM_TASK_SKETCH_LLM_ROUTE", DEFAULT_GLM_LLM_PROVIDER)
TASK_GENERATOR_LLM_MODEL = _env("AWM_TASK_GENERATOR_LLM_MODEL", DEFAULT_GLM_LLM_MODEL)
TASK_GENERATOR_LLM_EFFORT = _env("AWM_TASK_GENERATOR_LLM_EFFORT", "")
TASK_GENERATOR_LLM_ROUTE = _env("AWM_TASK_GENERATOR_LLM_ROUTE", DEFAULT_GLM_LLM_PROVIDER)
# Heavy-judge proxy: no baked default; the shim auto-starts unless AWM_HEAVY_SHIM_AUTO_START=0.
HEAVY_JUDGE_PROXY = _env_w("WILDAWM_HEAVY_JUDGE_PROXY", "",
                          alias="AWM_HEAVY_JUDGE_PROXY")
TASK_SKETCH_LLM_PROXY = _env("AWM_TASK_SKETCH_LLM_PROXY", HEAVY_JUDGE_PROXY)
TASK_GENERATOR_LLM_PROXY = _env("AWM_TASK_GENERATOR_LLM_PROXY", HEAVY_JUDGE_PROXY)
HEAVY_JUDGE_ANTHROPIC_PROXY = HEAVY_JUDGE_PROXY
HEAVY_SHIM_AUTO_START = _env_bool("AWM_HEAVY_SHIM_AUTO_START", True)
HEAVY_SHIM_PORT_BASE = _env_int("AWM_HEAVY_SHIM_PORT_BASE", 3465)
HEAVY_SHIM_START_TIMEOUT_S = float(_env("AWM_HEAVY_SHIM_START_TIMEOUT", "20"))
HEAVY_SHIM_LOG_DIR = _env_path("AWM_HEAVY_SHIM_LOG_DIR", LOG_DIR / "heavy-shims")

_HEAVY_SHIM_LOCK = threading.Lock()
_HEAVY_SHIM_PROCS: dict[str, subprocess.Popen] = {}
_HEAVY_SHIM_URLS: dict[str, str] = {}


def _proxy_health(url: str, spec: dict | None = None,
                  reasoning_policy: dict | None = None) -> bool:
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/health", timeout=HTTP_PROBE_TIMEOUT_S,
        ) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read() or b"{}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    if not spec:
        return True
    model = body.get("model")
    base_url = body.get("base_url")
    if model or base_url:
        ok = (
            model == spec.get("api_model")
            and str(base_url or "").rstrip("/") == str(spec.get("base_url") or "").rstrip("/")
        )
        if not ok:
            return False
        if reasoning_policy:
            return (
                body.get("reasoning_effort") == reasoning_policy.get("effort")
                and body.get("reasoning_chat_param")
                == (reasoning_policy.get("chat_param") or "reasoning_effort")
                and body.get("reasoning_responses_param")
                == (reasoning_policy.get("responses_param") or "reasoning")
            )
        return True
    return False


def _url_port(url: str) -> int | None:
    try:
        return urllib.parse.urlparse(url).port
    except ValueError:
        return None


def _url_with_port(url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    netloc = f"{host}:{port}"
    return urllib.parse.urlunparse((
        parsed.scheme or "http", netloc, parsed.path or "",
        parsed.params, parsed.query, parsed.fragment))


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _alloc_heavy_shim_url(preferred_url: str) -> str:
    preferred_port = _url_port(preferred_url)
    if preferred_port and _port_available(preferred_port):
        return preferred_url
    port = HEAVY_SHIM_PORT_BASE
    used = {_url_port(u) for u in _HEAVY_SHIM_URLS.values()}
    while port in used or not _port_available(port):
        port += 1
    return _url_with_port(preferred_url or "http://127.0.0.1", port)


def _start_heavy_shim(ref: str, spec: dict, preferred_url: str,
                      reasoning_policy: dict | None = None) -> str:
    url = _alloc_heavy_shim_url(preferred_url)
    port = _url_port(url)
    if not port:
        raise RuntimeError(f"cannot infer heavy shim port from {url!r}")
    HEAVY_SHIM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref).strip("_") or "model"
    log_path = HEAVY_SHIM_LOG_DIR / f"{port}_{safe_ref}.log"
    env = dict(os.environ)
    env.update({
        "AWM_LLM_PROXY_PORT": str(port),
        "AWM_OPENAI_BASE_URL": spec["base_url"],
        "AWM_OPENAI_API_KEY": spec.get("api_key", ""),
        "AWM_OPENAI_MODEL": spec["api_model"],
        "AWM_PROXY_PIN_MODEL": "1",
        "AWM_PROXY_MODEL_REF": ref,
        "AWM_PROXY_TRAJ_DIR": f"/tmp/xai_heavy_shim_{port}/traj",
        "AWM_PROXY_IMAGE_DIR": f"/tmp/xai_heavy_shim_{port}/img",
    })
    env.update(reasoning_effort_env(reasoning_policy))
    Path(env["AWM_PROXY_TRAJ_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["AWM_PROXY_IMAGE_DIR"]).mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "docker" / "anthropic-to-openai-proxy.py")],
            cwd=str(PROJECT_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.time() + HEAVY_SHIM_START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"heavy shim for {ref} exited with {proc.returncode}; see {log_path}")
        if _proxy_health(url, spec, reasoning_policy):
            _HEAVY_SHIM_PROCS[ref] = proc
            _HEAVY_SHIM_URLS[ref] = url
            return url
        time.sleep(0.5)
    raise RuntimeError(f"heavy shim for {ref} did not become healthy; see {log_path}")


def heavy_proxy_for(model_ref: str | None) -> tuple[str, str]:
    """Return (model ref, Anthropic proxy URL) for the heavy judge, starting a shim on demand."""
    ref = model_ref or HEAVY_JUDGE_MODEL
    preferred = HEAVY_JUDGE_PROXY
    if not HEAVY_SHIM_AUTO_START:
        if not preferred:
            raise RuntimeError(
                f"no Anthropic proxy endpoint for heavy-judge model {ref!r}: "
                "set WILDAWM_HEAVY_JUDGE_PROXY (or AWM_HEAVY_PROXY_<NAME> for a "
                "per-model URL), or enable the auto-started shim with "
                "AWM_HEAVY_SHIM_AUTO_START=1 (default)")
        return ref, preferred
    spec = model_spec(ref)
    reasoning_policy = reasoning_effort_policy(spec, None, harness="claude-code")
    with _HEAVY_SHIM_LOCK:
        proc = _HEAVY_SHIM_PROCS.get(ref)
        url = _HEAVY_SHIM_URLS.get(ref)
        if (url and proc and proc.poll() is None
                and _proxy_health(url, spec, reasoning_policy)):
            return ref, url
        if _proxy_health(preferred, spec, reasoning_policy):
            return ref, preferred
        return ref, _start_heavy_shim(ref, spec, preferred, reasoning_policy)


_DEFAULT_TEXTONLY_MODELS = ",".join(filter(None, (
    DEFAULT_GLM_LLM_REF,
    DEFAULT_GLM_LLM_MODEL,
)))
TEXTONLY_VISION_SHIM_MODELS = _env_csv_set(
    "AWM_TEXTONLY_VISION_SHIM_MODELS", _DEFAULT_TEXTONLY_MODELS)
TEXTONLY_JUDGE_MODELS = _env_csv_set(
    "AWM_TEXTONLY_JUDGES",
    ",".join(sorted(TEXTONLY_VISION_SHIM_MODELS)),
)
TEXTONLY_GENERATOR_MODELS = _env_csv_set(
    "AWM_TEXTONLY_GENERATOR_MODELS",
    ",".join(sorted(TEXTONLY_VISION_SHIM_MODELS)),
)
# Vision captioning endpoint for text-only models; no baked default.
VISION_MCP_BASE_URL = _env_w("WILDAWM_VISION_BASE_URL", "",
                            alias="AWM_VISION_BASE_URL")
VISION_MCP_MODEL = _env_w("WILDAWM_VISION_MODEL", "",
                         alias="AWM_VISION_MODEL")
VISION_MCP_API_KEY = _env_w("WILDAWM_VISION_API_KEY", "",
                            alias="AWM_VISION_API_KEY")


def model_needs_vision_shim(model_ref: str | None,
                            models: set[str] | None = None) -> bool:
    """True when a model ref or bare model id needs describe_image for images."""
    if not model_ref:
        return False
    candidates = {model_ref}
    if "/" in model_ref:
        candidates.add(model_ref.rsplit("/", 1)[-1])
    configured = models or TEXTONLY_VISION_SHIM_MODELS
    return bool({c.lower() for c in candidates}
                & {m.lower() for m in configured})


def judge_needs_vision_shim(model_ref: str | None) -> bool:
    """True when a heavy-judge model needs describe_image instead of Read."""
    return model_needs_vision_shim(model_ref or HEAVY_JUDGE_MODEL,
                                   TEXTONLY_JUDGE_MODELS)


def generator_needs_vision_shim(model_ref: str | None) -> bool:
    """True when a generator model needs describe_image instead of screenshots."""
    return model_needs_vision_shim(model_ref or TASK_GENERATOR_LLM_MODEL,
                                   TEXTONLY_GENERATOR_MODELS)


# Default harness for synthesis roles; per-role overrides are resolved in synthesis/harness/model.py.
SYNTH_HARNESS = _env("AWM_SYNTH_HARNESS", "opencode")
# Default OpenCode synthesis model (a models.json ref).
OPENCODE_LLM_REF = _env("AWM_OPENCODE_LLM_REF", DEFAULT_GLM_LLM_REF)
# Image whose bundled opencode drives host-side roles.
OPENCODE_AGENT_IMAGE = _env("AWM_OPENCODE_AGENT_IMAGE", "wildawm-agent:latest")
# Vision model (registry ref) behind the describe_image MCP for text-only models.
OPENCODE_VISION_REF = _env("AWM_OPENCODE_VISION_REF", "")

# Ensemble solver pool: user-configured via env or WILDAWM_SYNTHESIS_CONFIG; empty means no ensemble.
DEFAULT_ENSEMBLE_MODELS = ""
DEFAULT_ENSEMBLE_HARNESSES = ""
_models_cfg = _cfg_file_list("ensemble_models")
_harness_cfg = _cfg_file_list("ensemble_harnesses")
ENSEMBLE_MODELS = _env_csv_list_any(
    ("WILDAWM_ENSEMBLE_MODELS", "AWM_ENSEMBLE_MODELS"),
    ",".join(_models_cfg) if _models_cfg is not None else DEFAULT_ENSEMBLE_MODELS)
ENSEMBLE_HARNESSES = _env_csv_list_any(
    ("WILDAWM_ENSEMBLE_HARNESSES", "AWM_ENSEMBLE_HARNESSES"),
    ",".join(_harness_cfg) if _harness_cfg is not None else DEFAULT_ENSEMBLE_HARNESSES)
ENSEMBLE_N = _env_or_cfg_int("AWM_ENSEMBLE_N", "ensemble_n", 3)
LIVE_TEST_N = _env_or_cfg_int("AWM_LIVE_TEST_N", "live_test_n", 3)
_DEFAULT_DIFFICULTY_GATE_ENSEMBLE_MODELS = ",".join(ENSEMBLE_MODELS)
DIFFICULTY_GATE_ENSEMBLE_MODELS = _env_csv_list(
    "AWM_DIFFICULTY_GATE_ENSEMBLE_MODELS",
    _DEFAULT_DIFFICULTY_GATE_ENSEMBLE_MODELS,
)
BARE_HARD_MAX_COMPLETED = _env_or_cfg_int(
    "AWM_BARE_HARD_MAX_COMPLETED", "bare_hard_max_completed", 1)
BARE_MEDIUM_MIN_COMPLETED = _env_or_cfg_int(
    "AWM_BARE_MEDIUM_MIN_COMPLETED", "bare_medium_min_completed", 2)
BARE_MEDIUM_MAX_COMPLETED = _env_or_cfg_int(
    "AWM_BARE_MEDIUM_MAX_COMPLETED", "bare_medium_max_completed", 2)
DEAD_HARNESSES = _env_csv_set("AWM_DEAD_HARNESSES")
CELL_RETRY = _env_int("AWM_CELL_RETRY", 1)
MAX_INFRA_RESAMPLE = _env_int("AWM_MAX_INFRA_RESAMPLE", 4)
MAX_JUDGE_INFRA_RESAMPLE = _env_int(
    "AWM_MAX_JUDGE_INFRA_RESAMPLE", MAX_INFRA_RESAMPLE)
ENSEMBLE_MODEL_PREFLIGHT = _env_bool("AWM_ENSEMBLE_MODEL_PREFLIGHT", True)
HOST_LLM_MAX_CONCURRENCY = _env_int("AWM_HOST_LLM_MAX_CONCURRENCY", 10)

# Shared timeout budgets.
LLM_SHORT_TIMEOUT_S = _env_int("AWM_LLM_SHORT_TIMEOUT", 300)
LLM_MEDIUM_TIMEOUT_S = _env_int("AWM_LLM_MEDIUM_TIMEOUT", 900)
LLM_HEAVY_TIMEOUT_S = _env_int("AWM_LLM_HEAVY_TIMEOUT", 1800)
LLM_AUTHOR_TIMEOUT_S = _env_int("AWM_LLM_AUTHOR_TIMEOUT", 5400)
LLM_ARTIFACT_TIMEOUT_S = _env_int("AWM_LLM_ARTIFACT_TIMEOUT", 3600)
LOCAL_SCRIPT_TIMEOUT_S = _env_int("AWM_LOCAL_SCRIPT_TIMEOUT", 180)
RUNTIME_SHORT_TIMEOUT_S = _env_int("AWM_RUNTIME_SHORT_TIMEOUT", 300)
RUNTIME_PROVISION_TIMEOUT_S = _env_int("AWM_RUNTIME_PROVISION_TIMEOUT", 900)
RUNTIME_HEAVY_TIMEOUT_S = _env_int(
    "AWM_RUNTIME_HEAVY_TIMEOUT",
    max(RUNTIME_PROVISION_TIMEOUT_S * 2,
        LLM_HEAVY_TIMEOUT_S + RUNTIME_SHORT_TIMEOUT_S))
ENV_PORT_BIND_ATTEMPTS = _env_int("AWM_ENV_PORT_BIND_ATTEMPTS", 10)
HTTP_PROBE_TIMEOUT_S = _env_int("AWM_HTTP_PROBE_TIMEOUT", 5)
HTTP_SHORT_TIMEOUT_S = _env_int("AWM_HTTP_SHORT_TIMEOUT", 30)
HTTP_MEDIUM_TIMEOUT_S = _env_int("AWM_HTTP_MEDIUM_TIMEOUT", 60)
DB_CONNECT_TIMEOUT_S = _env_int("AWM_DB_CONNECT_TIMEOUT", 5)
DB_BUSY_TIMEOUT_MS = _env_int("AWM_DB_BUSY_TIMEOUT_MS", DB_CONNECT_TIMEOUT_S * 1000)
DB_LOCK_RETRIES = _env_int("AWM_DB_LOCK_RETRIES", 8)
DB_LOCK_RETRY_BASE_MS = _env_int("AWM_DB_LOCK_RETRY_BASE_MS", 100)
SUBPROCESS_FAST_TIMEOUT_S = _env_int("AWM_SUBPROCESS_FAST_TIMEOUT", 15)
DOCKER_COMMAND_TIMEOUT_S = _env_int("AWM_DOCKER_COMMAND_TIMEOUT", 120)
WEBHARBOR_NETWORK_CONNECT_TIMEOUT_S = _env_int(
    "AWM_WEBHARBOR_NETWORK_CONNECT_TIMEOUT", 60)
WEBHARBOR_NETWORK_CONNECT_RETRIES = _env_int(
    "AWM_WEBHARBOR_NETWORK_CONNECT_RETRIES", 4)
LLM_PROXY_UPSTREAM_TIMEOUT_S = _env_int(
    "AWM_LLM_PROXY_UPSTREAM_TIMEOUT", LLM_MEDIUM_TIMEOUT_S)

# Harbor package emission budgets.
HARBOR_BLOB_TIMEOUT_S = _env_int("AWM_HARBOR_BLOB_TIMEOUT", 600)
HARBOR_DIGEST_TIMEOUT_S = _env_int("AWM_HARBOR_DIGEST_TIMEOUT", 600)

SOLVE_TIMEOUT_S = _env_int("AWM_SOLVE_TIMEOUT", 1800)
SYNTHESIS_MAX_LLM_TURNS = _env_int("AWM_SYNTHESIS_MAX_LLM_TURNS", 99999)
TASK_SYNTHESIS_MAX_LLM_TURNS = _env_int(
    "AWM_TASK_SYNTHESIS_MAX_LLM_TURNS", SYNTHESIS_MAX_LLM_TURNS)
SKETCH_MAX_LLM_TURNS = _env_int(
    "AWM_SKETCH_MAX_LLM_TURNS", TASK_SYNTHESIS_MAX_LLM_TURNS)
SKETCH_OPENCODE_EXPLORE_MAX_LLM_TURNS = _env_int(
    "AWM_SKETCH_OPENCODE_EXPLORE_MAX_LLM_TURNS", 0)
SKETCH_OPENCODE_FINALIZE_RESERVE_S = _env_int(
    "AWM_SKETCH_OPENCODE_FINALIZE_RESERVE", 420)
SKETCH_OPENCODE_FRESH_FINALIZE_TIMEOUT_S = _env_int(
    "AWM_SKETCH_OPENCODE_FRESH_FINALIZE_TIMEOUT", 420)
SKETCH_OPENCODE_FRESH_FINALIZER_REFS = _env_csv_list(
    "AWM_SKETCH_OPENCODE_FRESH_FINALIZER_REFS", "")
SKETCH_OPENCODE_FRESH_FINALIZER_TRANSCRIPT_CHARS = _env_int(
    "AWM_SKETCH_OPENCODE_FRESH_FINALIZER_TRANSCRIPT_CHARS", 12000)
SKETCH_OPENCODE_FRESH_FINALIZER_MAX_TOKENS = _env_int(
    "AWM_SKETCH_OPENCODE_FRESH_FINALIZER_MAX_TOKENS", 8192)
SKETCH_CONTINUATION_MAX_LLM_TURNS = _env_int(
    "AWM_SKETCH_CONTINUATION_MAX_LLM_TURNS", 10)
# opencode output ceiling: sketch/generator return results as final text, which can hit the limit.
OPENCODE_MODEL_OUTPUT_TOKENS = _env_int("AWM_OPENCODE_MODEL_OUTPUT_TOKENS", 32768)
OPENCODE_MODEL_CONTEXT_TOKENS = _env_int(
    "AWM_OPENCODE_MODEL_CONTEXT_TOKENS", 131072)
SKETCH_CONTINUATION_TIMEOUT_S = _env_int(
    "AWM_SKETCH_CONTINUATION_TIMEOUT", LLM_SHORT_TIMEOUT_S)
SKETCH_FINALIZE_MAX_LLM_TURNS = _env_int(
    "AWM_SKETCH_FINALIZE_MAX_LLM_TURNS", 2)
SKETCH_LLM_TURN_POLL_S = float(_env("AWM_SKETCH_LLM_TURN_POLL", "1.0"))
SOLVE_MAX_TURNS = _env_int("AWM_SOLVE_MAX_TURNS", TASK_SYNTHESIS_MAX_LLM_TURNS)
# World-synthesis budgets.
WORLD_SYN_MAX_LLM_TURNS = _env_int("AWM_WORLD_SYN_MAX_LLM_TURNS", 999)
WORLD_SYN_PROFILE_TIMEOUT_S = _env_int("AWM_WORLD_SYN_PROFILE_TIMEOUT", 1800)
# Later days plan slower as world state accumulates, so this bound covers the tail.
WORLD_SYN_PLAN_TIMEOUT_S = _env_int("AWM_WORLD_SYN_PLAN_TIMEOUT", 3600)
WORLD_SYN_IMPLEMENT_TIMEOUT_S = _env_int("AWM_WORLD_SYN_IMPLEMENT_TIMEOUT", 7200)
WORLD_SYN_MAX_IMPLEMENT_ITERS = _env_int("AWM_WORLD_SYN_MAX_IMPLEMENT_ITERS", 3)
WORLD_SYN_SKILL_TIMEOUT_S = _env_or_cfg_int(
    "AWM_WORLD_SYN_SKILL_TIMEOUT", "world_syn_skill_timeout", 1800)
WORLD_SYN_SKILL_MAX_TURNS = _env_or_cfg_int(
    "AWM_WORLD_SYN_SKILL_MAX_TURNS", "world_syn_skill_max_turns", 40)
WORLD_SYN_SKILL_INDEX_CHARS = _env_or_cfg_int(
    "AWM_WORLD_SYN_SKILL_INDEX_CHARS", "world_syn_skill_index_chars", 24000)
# Fixed bcrypt salt so password hashes stay deterministic; 22 chars, last char one of '.Oeu'.
WORLD_SYN_BCRYPT_SALT = _env("AWM_WORLD_SYN_BCRYPT_SALT", "wildawm/world/syn/sal.")
WORLD_SYN_BCRYPT_ROUNDS = _env_int("AWM_WORLD_SYN_BCRYPT_ROUNDS", 12)
SKETCH_TOOL_LIMIT = _env_int("AWM_SKETCH_TOOL_LIMIT", 0)

# Gates: each limit is overridable by env (AWM_*) or the synthesis config file.
STEP_GATE = _env_or_cfg_int("AWM_STEP_GATE", "step_gate", 30)
MAX_DEEPEN = _env_or_cfg_int("AWM_MAX_DEEPEN", "max_deepen", 2)
MAX_EASE = _env_or_cfg_int("AWM_MAX_EASE", "max_ease", 2)
MAX_AGREE_FIX = _env_or_cfg_int("AWM_MAX_AGREE_FIX", "max_agree_fix", 2)
MAX_CAVE_FIX = _env_or_cfg_int("AWM_MAX_CAVE_FIX", "max_cave_fix", 2)
MAX_POST_FIX_VALIDATE_FIXES = _env_or_cfg_int(
    "AWM_MAX_POST_FIX_VALIDATE_FIXES", "max_post_fix_validate_fixes", 2)
MAX_INTERFACE_REAUTHOR = _env_or_cfg_int(
    "AWM_MAX_INTERFACE_REAUTHOR", "max_interface_reauthor", 2)
MAX_GEN_ITERS = _env_or_cfg_int(
    "AWM_MAX_GENERATION_ITERS", "max_gen_iters", 2)
MAX_FIX_ITERS = _env_or_cfg_int("AWM_MAX_FIX_ITERS", "max_fix_iters", 2)
MAX_SKETCH_ROUNDS = _env_or_cfg_int(
    "AWM_MAX_SKETCH_ROUNDS", "max_sketch_rounds", 2)
MAX_WORLD_PATCH_ITERS = _env_or_cfg_int(
    "AWM_MAX_WORLD_PATCH_ITERS", "max_world_patch_iters", 2)
# Transport waits add grace over the turn timeout so lower layers report their own timeouts.
DEFAULT_AGENT_TURN_TIMEOUT_S = _env_int("AWM_AGENT_TURN_TIMEOUT", 1800)
DEFAULT_AGENT_MAX_TURNS = _env_int("AWM_AGENT_MAX_TURNS", 999)
TEST_NUDGE_MAX = _env_int("AWM_TEST_NUDGE_MAX", 5)
TEST_NUDGE_TURN_TIMEOUT_S = _env_int("AWM_TEST_NUDGE_TURN_TIMEOUT", 300)
AGENT_TRANSPORT_GRACE_S = _env_int("AWM_AGENT_TRANSPORT_GRACE", 240)
AGENT_BUDGET_POLL_S = float(_env("AWM_AGENT_ADAPTER_BUDGET_POLL", "1.5"))
AGENT_READY_TIMEOUT_S = RUNTIME_SHORT_TIMEOUT_S
OPENCLAW_PROVIDER_TIMEOUT_S = _env_int(
    "AWM_OPENCLAW_PROVIDER_TIMEOUT",
    max(LLM_MEDIUM_TIMEOUT_S, DEFAULT_AGENT_TURN_TIMEOUT_S - AGENT_TRANSPORT_GRACE_S),
)


def agent_chat_timeout(timeout_s: int | float | None = None, *, hops: int = 1) -> float:
    """HTTP timeout for an agent turn across layered transports."""
    base = float(timeout_s or DEFAULT_AGENT_TURN_TIMEOUT_S)
    return base + float(AGENT_TRANSPORT_GRACE_S * max(1, hops))


AGENT_ADAPTER_CHAT_TIMEOUT_S = agent_chat_timeout(hops=1)
WORLDCTL_CHAT_TIMEOUT_S = agent_chat_timeout(hops=2)
DASHBOARD_AGENT_CHAT_TIMEOUT_S = agent_chat_timeout(hops=3)
CLI_AGENT_CHAT_TIMEOUT_S = agent_chat_timeout(hops=4)
XAI_AGENT_STATUS_TIMEOUT_S = int(CLI_AGENT_CHAT_TIMEOUT_S)


def model_spec(ref: str) -> dict:
    """Resolve a provider/model ref from models.json."""
    if not str(ref or "").strip():
        raise ValueError(
            "empty model ref: set the default synthesis model "
            "(WILDAWM_SYNTH_MODEL_REF) or the per-role "
            "AWM_*_REF variable to a 'provider/model' entry from models.json "
            "(template: models.example.json)")
    reg = load_jsonc(MODELS_PATH)
    prov_name, _, model = ref.partition("/")
    prov = (reg.get("providers") or {})[prov_name]
    models = prov.get("models")
    model_quirks = prov.get("model_quirks") or {}
    model_info = prov.get("model_info") or {}
    api_model = model
    quirks = dict(model_quirks.get(model, {}) or {})
    default_info = model_info.get("*", {}) or {}
    if not isinstance(default_info, dict):
        raise TypeError(f"{prov_name}: model_info['*'] must be an object")
    info = {**default_info, **dict(model_info.get(model, {}) or {})}
    if isinstance(models, dict):
        if model not in models:
            raise KeyError(f"{ref}: model not in provider registry")
        entry = models[model] or {}
        api_model = entry.get("model_id", model)
        quirks.update(entry.get("quirks") or {})
        info.update(entry.get("info") or {})
    elif isinstance(models, list) and model not in models:
        raise KeyError(f"{ref}: model not in provider registry")
    spec = {"ref": ref, "provider": prov_name, "name": model,
            "api_model": api_model, "base_url": prov["base_url"],
            "api_key": prov.get("api_key", ""),
            "provider_type": prov.get("provider_type", "openai_generic"),
            "quirks": quirks, "info": info}
    if info.get("context_window") is not None:
        spec["context_window"] = int(info["context_window"])
    return spec


def model_spec_for_api(base_url: str | None, api_model: str | None) -> dict | None:
    """Best-effort reverse lookup of a registry spec from endpoint + api model."""
    if not api_model:
        return None
    try:
        reg = load_jsonc(MODELS_PATH)
    except Exception:
        return None
    base_norm = str(base_url or "").rstrip("/")
    fallback: dict | None = None
    for prov_name, prov in (reg.get("providers") or {}).items():
        models = prov.get("models")
        names = list(models.keys()) if isinstance(models, dict) else list(models or [])
        for name in names:
            try:
                spec = model_spec(f"{prov_name}/{name}")
            except Exception:
                continue
            if spec.get("api_model") != api_model:
                continue
            if base_norm and str(spec.get("base_url") or "").rstrip("/") == base_norm:
                return spec
            fallback = fallback or spec
    return fallback


def chat_token_limit_param(spec: dict) -> str:
    """Output-token parameter name (Gemini returns empty completions with max_completion_tokens)."""
    base = str((spec or {}).get("base_url", "")).lower()
    ptype = str((spec or {}).get("provider_type", "")).lower()
    if ptype == "gemini" or "generativelanguage.googleapis.com" in base:
        return "max_tokens"
    return "max_completion_tokens"


def opencode_vision_spec() -> dict:
    """Resolve the vision backend from a registry ref or the WILDAWM_VISION_* environment."""
    if OPENCODE_VISION_REF:
        return model_spec(OPENCODE_VISION_REF)
    if not VISION_MCP_BASE_URL or not VISION_MCP_MODEL:
        raise RuntimeError(
            "vision backend is not configured: set AWM_OPENCODE_VISION_REF or WILDAWM_VISION_BASE_URL and WILDAWM_VISION_MODEL")
    return {
        "ref": "vision/environment",
        "provider": "vision",
        "provider_type": "openai_generic",
        "base_url": VISION_MCP_BASE_URL,
        "api_key": VISION_MCP_API_KEY,
        "api_model": VISION_MCP_MODEL,
    }


def vision_reasoning_env() -> dict[str, str]:
    """Reasoning env for the image-description MCP (defaults to `none`)."""
    effort = os.environ.get("AWM_VISION_REASONING_EFFORT")
    if not str(effort or "").strip():
        effort = "none"
    try:
        policy = reasoning_effort_policy(opencode_vision_spec(), effort)
    except ReasoningEffortError:
        # Keep vision captioning non-reasoning even if the registry is incomplete.
        policy = {
            "effort": "none",
            "chat_param": "reasoning_effort",
            "responses_param": "reasoning",
            "fail_if_stripped": True,
        }
    return prefixed_reasoning_effort_env(policy, "AWM_VISION_REASONING")
